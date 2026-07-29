# mml_edi/tests/test_edi_wall.py
"""Integration tests for the edi.wall computation service.

Exercises the batched get_wall_summary payload against real edi.order.review /
edi.log / edi.trading.partner records: the fresh-install guard, the four alarm
tiles (count + tone), the three service-level bars, the partner strip, the live
feed and partner scoping.

Tagged post_install (the shared EDITestSetup fixture creates products, which at
at_install time hits registry-order NOT NULLs).
"""
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged

from .common import EDITestSetup


@tagged("post_install", "-at_install")
class TestEdiWall(TransactionCase, EDITestSetup):
    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.wall = self.env["edi.wall"]
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
        write = {}
        if received is not None:
            write["received_date"] = received
        if reviewed is not None:
            write["reviewed_date"] = reviewed
        if write:
            rec.write(write)
        return rec

    def _issue(self, review, issue_type, severity, desc="issue"):
        issue = self.env["edi.order.issue"].create({
            "review_id": review.id,
            "issue_type": issue_type,
            "severity": severity,
            "description": desc,
        })
        review._compute_issue_counts()
        return issue

    def _log(self, partner=None, direction="inbound", event_type="file_download",
             status="success", message="msg", filename=None, review=None):
        partner = partner or self.trading_partner
        return self.env["edi.log"].log(
            partner, direction, event_type, status, message,
            filename=filename, review=review)

    def _alarm(self, summary, key):
        return next(a for a in summary["alarms"] if a["key"] == key)

    def _bar(self, summary, label):
        return next(b for b in summary["service_levels"] if b["label"] == label)

    # ---- payload shape ----

    def test_payload_has_all_top_level_keys(self):
        summary = self.wall.get_wall_summary()
        for key in ("data_available", "alarms", "pipeline", "today",
                    "service_levels", "partners", "feed", "poll"):
            self.assertIn(key, summary)

    def test_four_alarms_in_order(self):
        keys = [a["key"] for a in self.wall.get_wall_summary()["alarms"]]
        self.assertEqual(keys, ["blocking", "failed_ack", "unknown", "poll"])

    def test_three_service_bars(self):
        labels = [b["label"] for b in self.wall.get_wall_summary()["service_levels"]]
        self.assertEqual(labels,
                         ["Auto-approval rate", "ORDRSP on-time", "Clean-parse rate"])

    # ---- fresh-install guard ----

    def test_data_available_false_before_inbound(self):
        self.assertFalse(self.wall.get_wall_summary(
            partner_code="TESTPARTNER")["data_available"])

    def test_data_available_true_after_inbound(self):
        self._log(event_type="file_download", direction="inbound")
        self.assertTrue(self.wall.get_wall_summary(
            partner_code="TESTPARTNER")["data_available"])

    # ---- alarm tiles ----

    def test_blocking_alarm_count_and_red_tone(self):
        for i in range(3):
            r = self._review(po="POB%d" % i)
            self._issue(r, "product_not_found", "blocking")
        alarm = self._alarm(self.wall.get_wall_summary(partner_code="TESTPARTNER"), "blocking")
        self.assertEqual(alarm["value"], "3")
        self.assertEqual(alarm["tone"], "red")

    def test_blocking_alarm_single_is_amber(self):
        r = self._review(po="POB1")
        self._issue(r, "product_not_found", "blocking")
        alarm = self._alarm(self.wall.get_wall_summary(partner_code="TESTPARTNER"), "blocking")
        self.assertEqual(alarm["value"], "1")
        self.assertEqual(alarm["tone"], "amber")

    def test_blocking_alarm_zero_is_calm(self):
        alarm = self._alarm(self.wall.get_wall_summary(partner_code="TESTPARTNER"), "blocking")
        self.assertEqual(alarm["value"], "0")
        self.assertEqual(alarm["tone"], "calm")

    def test_unknown_store_alarm(self):
        r = self._review(po="POS", store="1099")
        self._issue(r, "unknown_store", "warning", "store not mapped")
        alarm = self._alarm(self.wall.get_wall_summary(partner_code="TESTPARTNER"), "unknown")
        self.assertEqual(alarm["value"], "1")

    def test_failed_ordrsp_alarm(self):
        r = self._review(po="POACK", state="approved", file_hash="abcdef1234",
                         reviewed=fields.Datetime.now())
        filename = "ACK_%s_%s_%s.edi" % (self.trading_partner.code, "POACK", "abcdef12")
        self._log(direction="outbound", event_type="ack_sent", status="error",
                  message="SFTP timeout", filename=filename, review=r)
        alarm = self._alarm(self.wall.get_wall_summary(partner_code="TESTPARTNER"), "failed_ack")
        self.assertEqual(alarm["value"], "1")
        self.assertEqual(alarm["tone"], "amber")

    def test_poll_freshness_alarm_never_polled(self):
        alarm = self._alarm(self.wall.get_wall_summary(partner_code="TESTPARTNER"), "poll")
        self.assertEqual(alarm["value"], "—")
        self.assertEqual(alarm["tone"], "red")

    def test_poll_freshness_alarm_recent_is_calm(self):
        self._log(event_type="file_download", direction="inbound")
        alarm = self._alarm(self.wall.get_wall_summary(partner_code="TESTPARTNER"), "poll")
        # A just-now poll reads a small minute count and a calm tone.
        self.assertNotEqual(alarm["value"], "—")
        self.assertEqual(alarm["tone"], "calm")

    # ---- service-level bars ----

    def test_auto_approval_bar_value(self):
        for i in range(3):
            self._review(po="AUTO%d" % i, state="auto_approved")
        self._review(po="MANUAL", state="approved",
                     reviewed=fields.Datetime.now(),
                     received=fields.Datetime.now() - timedelta(hours=2))
        bar = self._bar(self.wall.get_wall_summary(partner_code="TESTPARTNER"),
                        "Auto-approval rate")
        self.assertEqual(bar["value"], "75.0%")
        self.assertEqual(bar["tone"], "green")  # meets default 75 target

    def test_clean_parse_bar_from_parse_logs(self):
        for i in range(3):
            self._log(event_type="file_parse", status="success", filename="f%d" % i)
        self._log(event_type="file_parse", status="error", filename="bad")
        bar = self._bar(self.wall.get_wall_summary(partner_code="TESTPARTNER"),
                        "Clean-parse rate")
        self.assertEqual(bar["value"], "75.0%")

    def test_ordrsp_on_time_bar_none_when_no_acks(self):
        bar = self._bar(self.wall.get_wall_summary(partner_code="TESTPARTNER"),
                        "ORDRSP on-time")
        self.assertEqual(bar["value"], "—")
        self.assertEqual(bar["tone"], "none")

    # ---- partner strip ----

    def test_partner_strip_shows_format_and_review_count(self):
        self._review(po="PS1")  # one pending review for TESTPARTNER
        strip = self.wall.get_wall_summary(partner_code="TESTPARTNER")["partners"]
        row = next(p for p in strip if p["name"] == self.trading_partner.name)
        self.assertIn("in review", row["sub"])
        self.assertTrue(row["dot"].startswith("#"))

    # ---- live feed ----

    def test_feed_rows_carry_tags(self):
        self._log(event_type="file_download")
        self._log(event_type="order_created")
        feed = self.wall.get_wall_summary(partner_code="TESTPARTNER")["feed"]
        tags = {row["tag"] for row in feed}
        self.assertEqual(tags, {"POLL", "ORDER"})

    # ---- partner scoping ----

    def test_partner_scope_narrows_blocking_alarm(self):
        ra = self._review(partner=self.trading_partner, po="POA")
        self._issue(ra, "product_not_found", "blocking")
        rb = self._review(partner=self.partner_b, po="POB")
        self._issue(rb, "product_not_found", "blocking")
        # Each partner sees only its own blocking count (clone-safe).
        a = self._alarm(self.wall.get_wall_summary(partner_code="TESTPARTNER"), "blocking")
        b = self._alarm(self.wall.get_wall_summary(partner_code="PARTNERB"), "blocking")
        self.assertEqual(a["value"], "1")
        self.assertEqual(b["value"], "1")

    def test_today_line_present(self):
        self._log(event_type="file_download")
        today = self.wall.get_wall_summary(partner_code="TESTPARTNER")["today"]
        self.assertEqual(today["files"], 1)
