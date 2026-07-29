# mml_edi/tests/test_edi_dashboard.py
"""Integration tests for the edi.dashboard computation service.

Exercises the batched get_dashboard_summary payload against real
edi.order.review / edi.log / edi.trading.partner records: the fresh-install
guard, partner scoping, needs-attention triage severity/ordering, the inbound
pipeline, the four KPI cards, the live feed's typed tags, the partner-health
rail and the honesty-footer copy.

Tagged post_install (the shared EDITestSetup fixture creates products, which at
at_install time hits registry-order NOT NULLs).
"""
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged

from .common import EDITestSetup


@tagged("post_install", "-at_install")
class TestEdiDashboard(TransactionCase, EDITestSetup):
    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.dash = self.env["edi.dashboard"]
        # A second partner so partner-scoping is actually exercised.
        self.partner_b = self.env["edi.trading.partner"].create({
            "name": "Second EDI Partner",
            "code": "PARTNERB",
            "partner_id": self.trading_partner.partner_id.id,
            "edi_format": "edifact_d01b",
            "parser_class": "mml_edi.parsers.kestrelby.KestrelbyParser",
            "ftp_protocol": "ftp",
            "ftp_host": "ftp.b.local",
            "environment": "test",
            "pricelist_id": self.trading_partner.pricelist_id.id,
        })

    # ---- helpers ----

    def _review(self, partner=None, po="PO1", store=None, state="pending_review",
                received=None, reviewed=None, file_hash=None):
        partner = partner or self.trading_partner
        vals = {
            "trading_partner_id": partner.id,
            "customer_po_number": po,
            "store_code": store,
            "state": state,
        }
        if file_hash:
            vals["edi_file_hash"] = file_hash
        rec = self.env["edi.order.review"].create(vals)
        # received_date / reviewed_date are ORM-writable datetimes (not fiscal-
        # locked pickings), so a plain write is safe for backdating here.
        write = {}
        if received is not None:
            write["received_date"] = received
        if reviewed is not None:
            write["reviewed_date"] = reviewed
        if write:
            rec.write(write)
        return rec

    def _issue(self, review, issue_type, severity, desc="issue"):
        return self.env["edi.order.issue"].create({
            "review_id": review.id,
            "issue_type": issue_type,
            "severity": severity,
            "description": desc,
        })

    def _log(self, partner=None, direction="inbound", event_type="file_download",
             status="success", message="msg", filename=None, review=None):
        partner = partner or self.trading_partner
        return self.env["edi.log"].log(
            partner, direction, event_type, status, message,
            filename=filename, review=review)

    # ---- fresh-install guard ----

    def test_data_available_false_before_any_inbound(self):
        # Scoped to the fresh fixture partner: deterministic on a prod clone
        # (data_available is partner-scoped, TESTPARTNER has no inbound logs).
        summary = self.dash.get_dashboard_summary(partner_code="TESTPARTNER")
        self.assertFalse(summary["data_available"])

    def test_data_available_true_after_inbound_log(self):
        self._log(event_type="file_download", direction="inbound")
        self.assertTrue(self.dash.get_dashboard_summary(
            partner_code="TESTPARTNER")["data_available"])

    # ---- partner options for the segmented filter ----

    def test_partner_options_lead_with_all(self):
        opts = self.dash.get_dashboard_summary()["partners"]
        self.assertEqual(opts[0]["code"], "all")
        codes = [o["code"] for o in opts]
        self.assertIn("TESTPARTNER", codes)
        self.assertIn("PARTNERB", codes)

    # ---- needs-attention triage ----

    def test_attention_blocking_is_red_and_first(self):
        r = self._review(po="POBLOCK")
        self._issue(r, "product_not_found", "blocking", "GTIN not in Odoo")
        r._compute_issue_counts()
        items = self.dash.get_dashboard_summary(
            partner_code="TESTPARTNER")["attention_items"]
        blocking = [i for i in items if i["kind"] == "blocking"]
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0]["severity"], "red")
        self.assertIn("POBLOCK", blocking[0]["title"])
        # blocking issue description surfaces as the summary
        self.assertIn("GTIN", blocking[0]["summary"])

    def test_attention_unknown_store_is_amber_with_map_action(self):
        r = self._review(po="POSTORE", store="1099")
        self._issue(r, "unknown_store", "warning", "store not mapped")
        r._compute_issue_counts()
        items = self.dash.get_dashboard_summary(
            partner_code="TESTPARTNER")["attention_items"]
        warn = [i for i in items if i["kind"] == "warning"]
        self.assertEqual(len(warn), 1)
        self.assertEqual(warn[0]["severity"], "amber")
        self.assertIn("map_store", warn[0]["actions"])
        self.assertIn("unknown store", warn[0]["title"])

    def test_attention_red_sorted_before_amber(self):
        rb = self._review(po="POBLOCK")
        self._issue(rb, "product_not_found", "blocking")
        rb._compute_issue_counts()
        rw = self._review(po="POWARN")
        self._issue(rw, "qty_shortfall", "warning")
        rw._compute_issue_counts()
        items = self.dash.get_dashboard_summary(
            partner_code="TESTPARTNER")["attention_items"]
        severities = [i["severity"] for i in items]
        # every red precedes every amber
        self.assertEqual(severities, sorted(severities, key=lambda s: 0 if s == "red" else 1))

    def test_attention_ack_failed_row(self):
        # An approved review whose per-PO ACK upload only ever errored reads
        # ack_status='failed' and surfaces as a red triage row.
        # reviewed_date within the 30d window the failed-ORDRSP source scans
        # (a real approved review always carries one).
        r = self._review(po="POACK", state="approved", file_hash="abcdef1234",
                         reviewed=fields.Datetime.now())
        exchange_key = "abcdef1234"[:8]
        filename = "ACK_%s_%s_%s.edi" % (self.trading_partner.code, "POACK", exchange_key)
        self._log(direction="outbound", event_type="ack_sent", status="error",
                  message="SFTP timeout", filename=filename)
        self.assertEqual(r.ack_status, "failed")
        items = self.dash.get_dashboard_summary(
            partner_code="TESTPARTNER")["attention_items"]
        ack = [i for i in items if i["kind"] == "ack_failed"]
        self.assertEqual(len(ack), 1)
        self.assertEqual(ack[0]["severity"], "red")
        self.assertIn("retry_ack", ack[0]["actions"])

    def test_attention_ack_failed_producer_shape(self):
        # Companion to the row test above, exercising the REAL producer shape:
        # edi_order_review._queue_ack's except branch logs an error 'ack_sent'
        # WITH filename (the root-cause fix) AND review_id — the row the
        # dashboard must find. A hand-crafted filename-only log (the old test)
        # is not what production emits.
        r = self._review(po="POACKP", state="approved", file_hash="feedface99",
                         reviewed=fields.Datetime.now())
        exchange_key = "feedface99"[:8]
        filename = "ACK_%s_%s_%s.edi" % (self.trading_partner.code, "POACKP", exchange_key)
        # Emitted exactly as _queue_ack does on upload failure.
        self._log(direction="outbound", event_type="ack_sent", status="error",
                  message="ACK generation/upload failed: SFTP timeout",
                  filename=filename, review=r)
        items = self.dash.get_dashboard_summary(
            partner_code="TESTPARTNER")["attention_items"]
        ack = [i for i in items if i["kind"] == "ack_failed"]
        self.assertEqual(len(ack), 1)
        self.assertEqual(ack[0]["res_id"], r.id)
        self.assertIn("retry_ack", ack[0]["actions"])

    def test_attention_ack_failed_auto_approved(self):
        # An auto-approved clean PO (the majority class for Kestrelby) carries NO
        # reviewed_date — retry_pending_acks still covers it, so a failed ACK on
        # one must surface. The failed-ORDRSP scan bounds it by received_date.
        r = self._review(po="POAUTO", state="auto_approved", file_hash="0ddba11abc",
                         received=fields.Datetime.now())
        exchange_key = "0ddba11abc"[:8]
        filename = "ACK_%s_%s_%s.edi" % (self.trading_partner.code, "POAUTO", exchange_key)
        self._log(direction="outbound", event_type="ack_sent", status="error",
                  message="ACK generation/upload failed: SFTP timeout",
                  filename=filename, review=r)
        items = self.dash.get_dashboard_summary(
            partner_code="TESTPARTNER")["attention_items"]
        ack = [i for i in items if i["kind"] == "ack_failed"]
        self.assertEqual(len(ack), 1)
        self.assertEqual(ack[0]["res_id"], r.id)

    def test_attention_ack_failed_dedupes_siblings(self):
        # Two store-review siblings of one inbound file share edi_file_hash ->
        # identical ACK filename -> one exchange. A single failed ACK must render
        # exactly ONE row, not one per sibling.
        common = fields.Datetime.now()
        r1 = self._review(po="POSIB", store="1", state="approved",
                          file_hash="5157e12345", reviewed=common)
        r2 = self._review(po="POSIB", store="2", state="approved",
                          file_hash="5157e12345", reviewed=common)
        exchange_key = "5157e12345"[:8]
        filename = "ACK_%s_%s_%s.edi" % (self.trading_partner.code, "POSIB", exchange_key)
        self._log(direction="outbound", event_type="ack_sent", status="error",
                  message="ACK generation/upload failed: SFTP timeout",
                  filename=filename, review=r1)
        items = self.dash.get_dashboard_summary(
            partner_code="TESTPARTNER")["attention_items"]
        ack = [i for i in items if i["kind"] == "ack_failed"]
        self.assertEqual(len(ack), 1)
        self.assertIn(ack[0]["res_id"], (r1.id, r2.id))

    def test_all_clear_when_no_attention(self):
        self._log(event_type="file_download")  # data available, nothing pending
        items = self.dash.get_dashboard_summary(
            partner_code="TESTPARTNER")["attention_items"]
        self.assertEqual(items, [])

    # ---- partner scoping ----

    def test_partner_scope_narrows_attention(self):
        ra = self._review(partner=self.trading_partner, po="POA")
        self._issue(ra, "product_not_found", "blocking")
        ra._compute_issue_counts()
        rb = self._review(partner=self.partner_b, po="POB")
        self._issue(rb, "product_not_found", "blocking")
        rb._compute_issue_counts()

        # Each fresh fixture partner sees only its own row — clone-safe (an
        # unscoped payload would be polluted by real pending reviews).
        scoped_a = self.dash.get_dashboard_summary(
            partner_code="TESTPARTNER")["attention_items"]
        self.assertEqual(len(scoped_a), 1)
        self.assertIn("POA", scoped_a[0]["title"])

        scoped_b = self.dash.get_dashboard_summary(
            partner_code="PARTNERB")["attention_items"]
        self.assertEqual(len(scoped_b), 1)
        self.assertIn("POB", scoped_b[0]["title"])

    def test_unknown_partner_code_scopes_to_nothing(self):
        r = self._review(po="POX")
        self._issue(r, "product_not_found", "blocking")
        r._compute_issue_counts()
        scoped = self.dash.get_dashboard_summary(partner_code="NOPE")["attention_items"]
        self.assertEqual(scoped, [])

    # ---- inbound pipeline ----

    def test_pipeline_counts_today(self):
        self._log(event_type="file_download")
        self._log(event_type="file_parse")
        self._log(event_type="order_created")
        self._log(event_type="ack_sent", status="success",
                  direction="outbound", filename="ACK_x.edi")
        self._review(po="PENDING1")  # standing in-review depth
        pipe = self.dash.get_dashboard_summary(partner_code="TESTPARTNER")["pipeline"]
        self.assertEqual(pipe["received"], 1)
        self.assertEqual(pipe["parsed"], 1)
        self.assertEqual(pipe["orders_created"], 1)
        self.assertEqual(pipe["acknowledged"], 1)
        self.assertEqual(pipe["in_review"], 1)

    def test_today_totals_distinct_pos(self):
        self._review(po="PO100", store="1")
        self._review(po="PO100", store="2")  # same PO, two stores
        self._review(po="PO200", store="1")
        today = self.dash.get_dashboard_summary(partner_code="TESTPARTNER")["today"]
        self.assertEqual(today["pos"], 2)          # two distinct POs
        self.assertEqual(today["store_orders"], 3)  # three store-orders

    # ---- KPI cards ----

    def test_auto_approval_and_exception_rate(self):
        now = fields.Datetime.now()
        # 3 auto-approved, 1 manually approved -> auto 75%, exception 25%.
        for i in range(3):
            self._review(po="AUTO%d" % i, state="auto_approved")
        self._review(po="MANUAL", state="approved",
                     reviewed=now, received=now - timedelta(hours=2))
        kpis = self.dash.get_dashboard_summary(partner_code="TESTPARTNER")["kpis"]
        self.assertEqual(kpis["auto_approval"]["value"], 75.0)
        self.assertEqual(kpis["exception_rate"]["value"], 25.0)
        # 75% auto-approval meets the default target -> green
        self.assertEqual(kpis["auto_approval"]["rag"], "green")

    def test_ordrsp_on_time_from_ack_logs(self):
        # 3 successful ACKs, 1 failed -> 75% on-time.
        for i in range(3):
            self._log(direction="outbound", event_type="ack_sent", status="success",
                      filename="ACK_ok_%d.edi" % i)
        self._log(direction="outbound", event_type="ack_sent", status="error",
                  filename="ACK_bad.edi")
        kpis = self.dash.get_dashboard_summary(partner_code="TESTPARTNER")["kpis"]
        self.assertEqual(kpis["ordrsp_on_time"]["value"], 75.0)

    def test_review_turnaround_median(self):
        now = fields.Datetime.now()
        self._review(po="T1", state="approved",
                     received=now - timedelta(hours=2), reviewed=now)
        self._review(po="T2", state="approved",
                     received=now - timedelta(hours=6), reviewed=now)
        kpis = self.dash.get_dashboard_summary(partner_code="TESTPARTNER")["kpis"]
        # median of [2h, 6h] = 4h
        self.assertEqual(kpis["review_turnaround"]["value"], 4.0)
        self.assertEqual(kpis["review_turnaround"].get("unit"), "h")

    def test_kpi_values_none_when_no_data(self):
        kpis = self.dash.get_dashboard_summary(partner_code="TESTPARTNER")["kpis"]
        # No resolved reviews / acks -> honest None, not a false 0.
        self.assertIsNone(kpis["auto_approval"]["value"])
        self.assertIsNone(kpis["ordrsp_on_time"]["value"])
        self.assertIsNone(kpis["review_turnaround"]["value"])

    # ---- live feed ----

    def test_live_feed_maps_event_types_to_tags(self):
        self._log(event_type="file_download")
        self._log(event_type="file_parse")
        self._log(event_type="order_created")
        self._log(event_type="duplicate_skipped")
        feed = self.dash.get_dashboard_summary(partner_code="TESTPARTNER")["live_feed"]
        tags = {row["tag"] for row in feed}
        self.assertEqual(tags, {"POLL", "PARSE", "ORDER", "DUP"})

    def test_live_feed_errored_ack_reads_exception(self):
        self._log(direction="outbound", event_type="ack_sent", status="error",
                  message="upload failed")
        feed = self.dash.get_dashboard_summary(partner_code="TESTPARTNER")["live_feed"]
        self.assertEqual(feed[0]["tag"], "EXCEPTION")

    # ---- partner-health rail ----

    def test_partner_health_rows(self):
        # An inbound poll must have happened for health to be anything but
        # "never" — set last_poll_date, then the pending review drives
        # health_state="review" ("Awaiting review").
        self._log(event_type="file_download")
        self._review(po="PH1")  # pending -> partner health = review
        rows = self.dash.get_dashboard_summary(
            partner_code="TESTPARTNER")["partner_health"]
        by_code = {r["code"]: r for r in rows}
        self.assertIn("TESTPARTNER", by_code)
        self.assertEqual(by_code["TESTPARTNER"]["health"], "Awaiting review")
        self.assertIn("in review", by_code["TESTPARTNER"]["detail"])

    # ---- honesty-footer copy ----

    def test_pending_banner_present_and_empty(self):
        self.assertEqual(self.dash.get_dashboard_summary(
            partner_code="TESTPARTNER")["pending_banner"], "")
        self._review(po="PB1")
        banner = self.dash.get_dashboard_summary(
            partner_code="TESTPARTNER")["pending_banner"]
        self.assertIn("pending review", banner)
        self.assertIn("open the queue", banner)

    def test_auto_line_counts_todays_auto_approved(self):
        self._review(po="A1", state="auto_approved")
        self._review(po="A2", state="auto_approved")
        line = self.dash.get_dashboard_summary(partner_code="TESTPARTNER")["auto_line"]
        self.assertIn("2 orders auto-approved", line)

    # ---- payload shape ----

    def test_payload_has_all_top_level_keys(self):
        summary = self.dash.get_dashboard_summary()
        for key in ("data_available", "partners", "attention_items", "pipeline",
                    "today", "pending_review", "kpis", "live_feed",
                    "partner_health", "feed", "auto_line", "pending_banner",
                    "targets"):
            self.assertIn(key, summary)
