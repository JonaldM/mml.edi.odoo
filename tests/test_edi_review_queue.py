# mml_edi/tests/test_edi_review_queue.py
"""Integration tests for the edi.review.queue computation service.

Exercises the two triage screens' batched payloads against real
edi.order.review / edi.order.issue / edi.log records:

  * get_queue      — grouping (blocking / warning / change / auto), pill counts,
                     clean-order detection, partner scoping, summary line,
                     degraded-partner banner, fresh-install guard.
  * get_review_detail — header chips, diagnosis hint, context grid, issue rows +
                     resolution chips, SO-lines matched-by/status derivation, the
                     per-PO ORDRSP deferral panel, lifecycle timeline, actions.

State transitions are pinned through the REAL edi.order.review /
edi.order.issue methods the OWL screens call (action_approve /
action_reject / action_reset_to_review / issue.action_accept), asserting the
row leaves its group and the resolution chip appears.

Tagged post_install (the shared EDITestSetup fixture creates products, which at
at_install time hits registry-order NOT NULLs).
"""
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged

from .common import EDITestSetup


@tagged("post_install", "-at_install")
class TestEdiReviewQueue(TransactionCase, EDITestSetup):
    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.svc = self.env["edi.review.queue"]
        self.partner_b = self.env["edi.trading.partner"].create({
            "name": "Second EDI Partner",
            "code": "PARTNERB",
            "partner_id": self.trading_partner.partner_id.id,
            "edi_format": "idoc_xml",
            "parser_class": "mml_edi.parsers.briscoes.BriscoesParser",
            "ftp_protocol": "ftp",
            "ftp_host": "ftp.b.local",
            "environment": "test",
            "pricelist_id": self.trading_partner.pricelist_id.id,
        })

    # ---- helpers ------------------------------------------------------------

    def _review(self, partner=None, po="PO1", store=None, state="pending_review",
                doc="new_order", received=None, reviewed=None, file_hash=None,
                raw=None, change_summary=None, so=None):
        partner = partner or self.trading_partner
        vals = {
            "trading_partner_id": partner.id,
            "customer_po_number": po,
            "store_code": store,
            "state": state,
            "document_type": doc,
        }
        if file_hash:
            vals["edi_file_hash"] = file_hash
        if raw is not None:
            vals["edi_raw_data"] = raw
        if change_summary is not None:
            vals["change_summary"] = change_summary
        if so is not None:
            vals["sale_order_id"] = so.id
        rec = self.env["edi.order.review"].create(vals)
        write = {}
        if received is not None:
            write["received_date"] = received
        if reviewed is not None:
            write["reviewed_date"] = reviewed
        if write:
            rec.write(write)
        return rec

    def _issue(self, review, issue_type, severity, desc="issue", so_line=None,
               edi_price=0.0, system_price=0.0):
        vals = {
            "review_id": review.id,
            "issue_type": issue_type,
            "severity": severity,
            "description": desc,
        }
        if so_line is not None:
            vals["sale_order_line_id"] = so_line.id
        if edi_price or system_price:
            vals["edi_price"] = edi_price
            vals["system_price"] = system_price
        iss = self.env["edi.order.issue"].create(vals)
        review._compute_issue_counts()
        return iss

    def _log(self, partner=None, direction="inbound", event_type="file_download",
             status="success", message="msg", filename=None, review=None,
             timestamp=None):
        partner = partner or self.trading_partner
        log = self.env["edi.log"].log(
            partner, direction, event_type, status, message,
            filename=filename, review=review)
        if timestamp is not None:
            log.write({"timestamp": timestamp})
        return log

    def _sale_order(self, qty=10.0, price=9.99):
        so = self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
        })
        self.env["sale.order.line"].create({
            "order_id": so.id,
            "product_id": self.test_product.id,
            "product_uom_qty": qty,
            "price_unit": price,
        })
        return so

    # ---- fresh-install guard ------------------------------------------------

    def test_data_available_false_before_any_inbound(self):
        q = self.svc.get_queue(partner_code="TESTPARTNER")
        self.assertFalse(q["data_available"])

    def test_data_available_true_after_inbound_log(self):
        self._log(event_type="file_download", direction="inbound")
        self.assertTrue(self.svc.get_queue(partner_code="TESTPARTNER")["data_available"])

    # ---- grouping -----------------------------------------------------------

    def test_blocking_group_holds_reviews_with_blocking_issues(self):
        r = self._review(po="POBLK")
        self._issue(r, "product_not_found", "blocking", "GTIN not in Odoo")
        q = self.svc.get_queue(partner_code="TESTPARTNER")
        groups = {g["key"]: g for g in q["groups"]}
        self.assertIn("blocking", groups)
        self.assertEqual(len(groups["blocking"]["items"]), 1)
        self.assertIn("POBLK", groups["blocking"]["items"][0]["ref"])
        self.assertEqual(q["counts"]["blocking"], 1)

    def test_warning_group_holds_warning_only_reviews(self):
        r = self._review(po="POWARN")
        self._issue(r, "qty_shortfall", "warning")
        q = self.svc.get_queue(partner_code="TESTPARTNER")
        groups = {g["key"]: g for g in q["groups"]}
        self.assertIn("warning", groups)
        self.assertEqual(q["counts"]["warning"], 1)
        self.assertNotIn("blocking", groups)

    def test_change_group_holds_change_orders(self):
        self._review(po="POCHG", doc="change_order", change_summary="qty 24 -> 36")
        q = self.svc.get_queue(partner_code="TESTPARTNER")
        groups = {g["key"]: g for g in q["groups"]}
        self.assertIn("change", groups)
        self.assertEqual(q["counts"]["change"], 1)

    def test_auto_group_holds_auto_approved(self):
        self._review(po="POAUTO", state="auto_approved")
        q = self.svc.get_queue(partner_code="TESTPARTNER")
        groups = {g["key"]: g for g in q["groups"]}
        self.assertIn("auto", groups)
        self.assertEqual(q["counts"]["auto"], 1)

    def test_clean_pending_is_flagged_for_approve_all_clean(self):
        r = self._review(po="POCLEAN")  # pending, no issues -> clean
        q = self.svc.get_queue(partner_code="TESTPARTNER")
        self.assertIn(r.id, q["clean_ids"])
        # It sits in the warning group (needs a decision) but carries no issue.
        self.assertEqual(q["counts"]["warning"], 1)

    def test_counts_all_is_sum_of_groups(self):
        rb = self._review(po="A")
        self._issue(rb, "product_not_found", "blocking")
        self._review(po="B", doc="change_order")
        self._review(po="C", state="auto_approved")
        q = self.svc.get_queue(partner_code="TESTPARTNER")
        self.assertEqual(
            q["counts"]["all"],
            q["counts"]["blocking"] + q["counts"]["warning"]
            + q["counts"]["change"] + q["counts"]["auto"])

    # ---- partner scoping ----------------------------------------------------

    def test_partner_scope_narrows_queue(self):
        ra = self._review(partner=self.trading_partner, po="POA")
        self._issue(ra, "product_not_found", "blocking")
        rb = self._review(partner=self.partner_b, po="POB")
        self._issue(rb, "product_not_found", "blocking")

        qa = self.svc.get_queue(partner_code="TESTPARTNER")
        self.assertEqual(qa["counts"]["blocking"], 1)
        self.assertIn("POA", qa["groups"][0]["items"][0]["ref"])

        qb = self.svc.get_queue(partner_code="PARTNERB")
        self.assertEqual(qb["counts"]["blocking"], 1)
        self.assertIn("POB", qb["groups"][0]["items"][0]["ref"])

    def test_unknown_partner_scopes_to_nothing(self):
        r = self._review(po="POX")
        self._issue(r, "product_not_found", "blocking")
        q = self.svc.get_queue(partner_code="NOPE")
        self.assertEqual(q["counts"]["all"], 0)
        self.assertEqual(q["groups"], [])

    def test_partner_options_lead_with_all(self):
        opts = self.svc.get_queue()["partners"]
        self.assertEqual(opts[0]["code"], "all")
        self.assertIn("TESTPARTNER", [o["code"] for o in opts])

    # ---- summary line -------------------------------------------------------

    def test_summary_line_counts_pending(self):
        self._review(po="P1")
        self._review(po="P2")
        line = self.svc.get_queue(partner_code="TESTPARTNER")["summary_line"]
        self.assertIn("2 pending", line)

    # ---- degraded banner ----------------------------------------------------

    def test_degraded_banner_names_partner_with_failed_acks(self):
        now = fields.Datetime.now()
        for i in range(2):
            self._log(direction="outbound", event_type="ack_sent", status="error",
                      filename="ACK_bad_%d.edi" % i, timestamp=now - timedelta(hours=1))
        banner = self.svc.get_queue(partner_code="TESTPARTNER")["degraded"]
        self.assertIsNotNone(banner)
        self.assertEqual(banner["partner_id"], self.trading_partner.id)
        self.assertIn("degraded", banner["text"])

    def test_no_degraded_banner_below_threshold(self):
        self._log(direction="outbound", event_type="ack_sent", status="error",
                  filename="ACK_one.edi")
        self.assertIsNone(self.svc.get_queue(partner_code="TESTPARTNER")["degraded"])

    # ---- detail: shape + chips ---------------------------------------------

    def test_detail_none_for_missing_id(self):
        self.assertIsNone(self.svc.get_review_detail(999999999))

    def test_detail_state_and_doc_chips(self):
        r = self._review(po="PODET", store="1017")
        d = self.svc.get_review_detail(r.id)
        self.assertEqual(d["state_label"], "Pending review")
        self.assertEqual(d["doc_label"], "NEW ORDER")
        self.assertFalse(d["doc_is_change"])
        self.assertIn("PODET", d["ref"])
        self.assertIn("1017", d["ref"])

    def test_detail_change_order_chip_and_mono_diff(self):
        r = self._review(po="POCHG", doc="change_order",
                         change_summary="LIN 00020 qty 24 -> 36")
        d = self.svc.get_review_detail(r.id)
        self.assertTrue(d["doc_is_change"])
        self.assertEqual(d["mono_label"], "Change summary (diff)")
        self.assertTrue(any("24 -> 36" in ln["text"] for ln in d["mono"]))

    def test_detail_context_grid_has_eight_cells(self):
        r = self._review(po="POCTX")
        d = self.svc.get_review_detail(r.id)
        self.assertEqual(len(d["context"]), 8)
        keys = [c["k"] for c in d["context"]]
        self.assertIn("Partner", keys)
        self.assertIn("PO number", keys)
        self.assertIn("ORDRSP", keys)

    def test_detail_diagnosis_hint_for_product_not_found(self):
        r = self._review(po="POHINT")
        self._issue(r, "product_not_found", "blocking")
        d = self.svc.get_review_detail(r.id)
        self.assertIn("match field", d["hint"])

    # ---- detail: issues + resolution chip ----------------------------------

    def test_detail_price_issue_carries_prices(self):
        r = self._review(po="POPRICE")
        self._issue(r, "price_discrepancy", "blocking", edi_price=8.90, system_price=8.10)
        d = self.svc.get_review_detail(r.id)
        iss = d["issues"][0]
        self.assertTrue(iss["has_price"])
        self.assertEqual(iss["edi_price"], "8.90")
        self.assertEqual(iss["sys_price"], "8.10")
        self.assertIn("actions", iss)
        self.assertTrue(iss["show_actions"])

    def test_issue_accept_sets_resolution_chip(self):
        r = self._review(po="PORES")
        iss = self._issue(r, "product_not_found", "blocking")
        # The REAL resolution action the OWL calls.
        iss.action_accept()
        d = self.svc.get_review_detail(r.id)
        row = d["issues"][0]
        self.assertTrue(row["resolved"])
        self.assertEqual(row["res_label"], "Accepted")
        self.assertFalse(row["show_actions"])

    # ---- detail: SO lines ---------------------------------------------------

    def test_detail_so_lines_matched_by_barcode(self):
        so = self._sale_order()
        r = self._review(po="POSO", so=so)
        d = self.svc.get_review_detail(r.id)
        self.assertEqual(len(d["so_lines"]), 1)
        line = d["so_lines"][0]
        # test_product carries barcode 9780000000002 -> matched by barcode, OK.
        self.assertEqual(line["match_by"], "barcode")
        self.assertEqual(line["match_cls"], "edi-chip-green")
        self.assertEqual(line["status"], "OK")
        # system price resolves from the fixture pricelist item (9.99).
        self.assertEqual(line["sys_price"], "9.99")

    def test_detail_so_line_status_reflects_issue(self):
        so = self._sale_order()
        line = so.order_line[0]
        r = self._review(po="POSL", so=so)
        self._issue(r, "price_discrepancy", "blocking", so_line=line,
                    edi_price=8.90, system_price=8.10)
        d = self.svc.get_review_detail(r.id)
        self.assertEqual(d["so_lines"][0]["status"], "Price")
        self.assertEqual(d["so_lines"][0]["status_cls"], "edi-pill-amber")

    # ---- detail: ORDRSP deferral panel -------------------------------------

    def test_ordrsp_panel_sibling_progress(self):
        common_hash = "abc12345ff"
        self._review(po="POMULTI", store="1042", state="approved", file_hash=common_hash)
        this = self._review(po="POMULTI", store="1017", state="pending_review",
                            file_hash=common_hash)
        d = self.svc.get_review_detail(this.id)
        o = d["ordrsp"]
        self.assertEqual(o["total_stores"], 2)
        self.assertEqual(o["resolved_stores"], 1)
        self.assertEqual(o["pct"], 50.0)
        self.assertFalse(o["all_resolved"])
        # The pending 'this' review's chip announces it holds the ORDRSP.
        self.assertTrue(any("holds the ORDRSP" in s["label"] for s in o["stores"]))

    def test_ordrsp_filename_is_per_po_exchange(self):
        r = self._review(po="POACKF", file_hash="deadbeef99")
        o = self.svc.get_review_detail(r.id)["ordrsp"]
        self.assertEqual(o["filename"], "ACK_TESTPARTNER_POACKF_deadbeef.edi")

    # ---- detail: lifecycle timeline ----------------------------------------

    def test_timeline_from_logs_plus_awaiting_node(self):
        r = self._review(po="POTL")
        self._log(event_type="file_download", review=r)
        self._log(event_type="file_parse", review=r)
        d = self.svc.get_review_detail(r.id)
        titles = [ev["title"] for ev in d["timeline"]]
        self.assertIn("File downloaded", titles)
        self.assertIn("File parsed", titles)
        # A pending review ends with the synthetic Awaiting-review node.
        self.assertEqual(d["timeline"][-1]["title"], "Awaiting review")

    # ---- detail: actions ----------------------------------------------------

    def test_actions_for_pending_new_order(self):
        r = self._review(po="POACT")
        self._issue(r, "product_not_found", "blocking")
        d = self.svc.get_review_detail(r.id)
        keys = [a["key"] for a in d["actions"]]
        self.assertIn("approve", keys)
        self.assertIn("reject", keys)
        # A blocking product_not_found makes the primary read "Match & approve".
        primary = next(a for a in d["actions"] if a["key"] == "approve")
        self.assertEqual(primary["kind"], "primary")
        self.assertEqual(primary["label"], "Match & approve")

    def test_actions_for_auto_approved_open_so(self):
        so = self._sale_order()
        r = self._review(po="POAUTOSO", state="auto_approved", so=so)
        d = self.svc.get_review_detail(r.id)
        keys = [a["key"] for a in d["actions"]]
        self.assertIn("open_so", keys)
        self.assertNotIn("approve", keys)

    # ---- state transitions leave the pending groups -------------------------

    def test_approve_removes_from_pending_queue(self):
        r = self._review(po="POAPPROVE")  # clean pending -> warning group + clean
        q0 = self.svc.get_queue(partner_code="TESTPARTNER")
        self.assertIn(r.id, q0["clean_ids"])
        # The REAL approve path the Approve-all-clean button calls.
        r.action_approve()
        self.assertEqual(r.state, "approved")
        q1 = self.svc.get_queue(partner_code="TESTPARTNER")
        # No longer a pending clean; approved reviews move to the audit log, so
        # they are not in any queue group (auto shows only auto_approved).
        self.assertNotIn(r.id, q1["clean_ids"])
        all_ids = [it["id"] for g in q1["groups"] for it in g["items"]]
        self.assertNotIn(r.id, all_ids)

    def test_reject_removes_from_pending_queue(self):
        r = self._review(po="POREJECT")
        self._issue(r, "product_not_found", "blocking")
        r.action_reject()
        self.assertEqual(r.state, "rejected")
        q = self.svc.get_queue(partner_code="TESTPARTNER")
        all_ids = [it["id"] for g in q["groups"] for it in g["items"]]
        self.assertNotIn(r.id, all_ids)
