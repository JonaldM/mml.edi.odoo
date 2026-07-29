# mml_edi/tests/test_edi_mobile_triage.py
"""Integration tests for the edi.mobile.triage computation service.

Exercises the batched get_triage payload against real edi.order.review /
edi.order.issue records: the pending/blocking/warning header counts, the
per-card one-tap action pairs (blocking, unknown store, plain warning), the
red-before-amber ordering, partner scoping and the fresh-install guard.

Tagged post_install (the shared EDITestSetup fixture creates products, which at
at_install time hits registry-order NOT NULLs).
"""
from odoo.tests import TransactionCase, tagged

from .common import EDITestSetup


@tagged("post_install", "-at_install")
class TestEdiMobileTriage(TransactionCase, EDITestSetup):
    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.triage = self.env["edi.mobile.triage"]
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

    def _review(self, partner=None, po="PO1", store=None, state="pending_review"):
        partner = partner or self.trading_partner
        return self.env["edi.order.review"].create({
            "trading_partner_id": partner.id,
            "customer_po_number": po,
            "store_code": store,
            "state": state,
        })

    def _issue(self, review, issue_type, severity, desc="issue"):
        issue = self.env["edi.order.issue"].create({
            "review_id": review.id,
            "issue_type": issue_type,
            "severity": severity,
            "description": desc,
        })
        review._compute_issue_counts()
        return issue

    def _log(self, event_type="file_download", direction="inbound", status="success"):
        return self.env["edi.log"].log(
            self.trading_partner, direction, event_type, status, "msg")

    def _actions(self, card):
        return [(a["action"], a["variant"]) for a in card["actions"]]

    # ---- payload shape ----

    def test_payload_has_all_top_level_keys(self):
        payload = self.triage.get_triage()
        for key in ("data_available", "partners", "pending", "red_count",
                    "amber_count", "cards"):
            self.assertIn(key, payload)

    def test_partner_options_lead_with_all(self):
        opts = self.triage.get_triage()["partners"]
        self.assertEqual(opts[0]["code"], "all")

    # ---- fresh-install guard ----

    def test_data_available_false_before_inbound(self):
        self.assertFalse(self.triage.get_triage(
            partner_code="TESTPARTNER")["data_available"])

    def test_data_available_true_after_inbound(self):
        self._log()
        self.assertTrue(self.triage.get_triage(
            partner_code="TESTPARTNER")["data_available"])

    # ---- header counts ----

    def test_pending_and_severity_counts(self):
        rb = self._review(po="POBLOCK")
        self._issue(rb, "product_not_found", "blocking")
        rw = self._review(po="POWARN")
        self._issue(rw, "qty_shortfall", "warning")
        self._review(po="POCLEAN")  # pending, no issues -> counts to pending only
        payload = self.triage.get_triage(partner_code="TESTPARTNER")
        self.assertEqual(payload["pending"], 3)
        self.assertEqual(payload["red_count"], 1)
        self.assertEqual(payload["amber_count"], 1)

    # ---- per-card actions ----

    def test_blocking_card_open_then_reject(self):
        r = self._review(po="POBLOCK")
        self._issue(r, "product_not_found", "blocking", "GTIN missing")
        cards = self.triage.get_triage(partner_code="TESTPARTNER")["cards"]
        blocking = next(c for c in cards if c["kind"] == "blocking")
        self.assertEqual(blocking["id"], r.id)
        self.assertEqual(blocking["severity"], "red")
        self.assertEqual(self._actions(blocking),
                         [("open", "accent"), ("reject", "danger")])
        self.assertIn("POBLOCK", blocking["title"])

    def test_unknown_store_card_open_then_reject(self):
        r = self._review(po="POS", store="1099")
        self._issue(r, "unknown_store", "warning", "store not mapped")
        cards = self.triage.get_triage(partner_code="TESTPARTNER")["cards"]
        warn = next(c for c in cards if c["kind"] == "warning")
        self.assertEqual(warn["severity"], "amber")
        self.assertEqual(self._actions(warn),
                         [("open", "accent"), ("reject", "danger")])

    def test_plain_warning_card_approve_then_open(self):
        r = self._review(po="POWARN")
        self._issue(r, "qty_shortfall", "warning", "clamped to available")
        cards = self.triage.get_triage(partner_code="TESTPARTNER")["cards"]
        warn = next(c for c in cards if c["kind"] == "warning")
        self.assertEqual(self._actions(warn),
                         [("approve", "approve"), ("open", "neutral")])

    # ---- ordering ----

    def test_cards_red_before_amber(self):
        rw = self._review(po="POWARN")
        self._issue(rw, "qty_shortfall", "warning")
        rb = self._review(po="POBLOCK")
        self._issue(rb, "product_not_found", "blocking")
        cards = self.triage.get_triage(partner_code="TESTPARTNER")["cards"]
        severities = [c["severity"] for c in cards]
        self.assertEqual(severities,
                         sorted(severities, key=lambda s: 0 if s == "red" else 1))

    # ---- partner scoping ----

    def test_partner_scope_narrows_cards(self):
        ra = self._review(partner=self.trading_partner, po="POA")
        self._issue(ra, "product_not_found", "blocking")
        rb = self._review(partner=self.partner_b, po="POB")
        self._issue(rb, "product_not_found", "blocking")
        a = self.triage.get_triage(partner_code="TESTPARTNER")["cards"]
        b = self.triage.get_triage(partner_code="PARTNERB")["cards"]
        self.assertEqual(len(a), 1)
        self.assertIn("POA", a[0]["title"])
        self.assertEqual(len(b), 1)
        self.assertIn("POB", b[0]["title"])

    def test_all_clear_has_no_cards(self):
        self._log()  # data available, nothing pending
        payload = self.triage.get_triage(partner_code="TESTPARTNER")
        self.assertEqual(payload["cards"], [])
        self.assertEqual(payload["red_count"], 0)
        self.assertEqual(payload["amber_count"], 0)
