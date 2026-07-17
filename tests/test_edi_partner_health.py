# mml_edi/tests/test_edi_partner_health.py
"""Integration tests for the edi.partner.health computation service.

Exercises the batched get_health_summary payload against real
edi.trading.partner / edi.log / edi.order.review records: the 24h exchange-queue
strip, the partner rail health states (never / review / error / ok / circuit
open), the per-partner detail bundle (diagnosis hint, four stats, circuit
breaker, transport rows incl. the SFTP host-key badge, 7-day throughput buckets
and recent exchanges).

Tagged post_install (the shared EDITestSetup fixture creates products, which at
at_install time hits registry-order NOT NULLs).
"""
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged

from .common import EDITestSetup


@tagged("post_install", "-at_install")
class TestEdiPartnerHealth(TransactionCase, EDITestSetup):
    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.svc = self.env["edi.partner.health"]
        # A second, SFTP partner so transport-panel + multi-partner rail are
        # exercised. Dedicated config (never search([],limit=1)).
        self.partner_b = self.env["edi.trading.partner"].create({
            "name": "SFTP EDI Partner",
            "code": "SFTPB",
            "partner_id": self.trading_partner.partner_id.id,
            "edi_format": "idoc_xml",
            "parser_class": "mml_edi.parsers.briscoes.BriscoesParser",
            "ftp_protocol": "sftp",
            "ftp_host": "sftp.b.local",
            "ftp_port": 22,
            "ftp_inbox_path": "/in",
            "ftp_outbox_path": "/out",
            "environment": "test",
            "order_split_mode": "per_store",
            "pricelist_id": self.trading_partner.pricelist_id.id,
        })

    # ---- helpers ----

    def _log(self, partner=None, direction="inbound", event_type="file_download",
             status="success", message="msg", filename=None):
        partner = partner or self.trading_partner
        return self.env["edi.log"].log(
            partner, direction, event_type, status, message, filename=filename)

    def _review(self, partner=None, po="PO1", state="pending_review"):
        partner = partner or self.trading_partner
        return self.env["edi.order.review"].create({
            "trading_partner_id": partner.id,
            "customer_po_number": po,
            "state": state,
        })

    def _detail_for(self, summary, partner):
        return summary["details"][str(partner.id)]

    def _row_for(self, summary, partner):
        return next(r for r in summary["partners"] if r["id"] == partner.id)

    # ---- exchange-queue strip ----

    def test_exchange_queue_counts_last_24h(self):
        self._log(event_type="file_download")
        self._log(event_type="file_download")
        self._log(event_type="file_parse")
        self._log(event_type="order_created")
        self._log(direction="outbound", event_type="ack_sent", status="success")
        self._log(direction="outbound", event_type="ack_sent", status="error")
        self._log(direction="outbound", event_type="ack_sent", status="warning")
        queue = self.svc.get_health_summary()["queue"]
        by_label = {q["label"]: q["n"] for q in queue}
        self.assertEqual(by_label["Polled"], 2)
        self.assertEqual(by_label["Parsed"], 1)
        self.assertEqual(by_label["Orders"], 1)
        self.assertEqual(by_label["ACK sent"], 1)
        self.assertEqual(by_label["Failed"], 1)
        self.assertEqual(by_label["ACK queued"], 1)
        # Six lanes, arrows on all but the last.
        self.assertEqual(len(queue), 6)
        self.assertTrue(all(q["arrow"] for q in queue[:-1]))
        self.assertFalse(queue[-1]["arrow"])

    def test_exchange_queue_excludes_older_than_24h(self):
        old = self._log(event_type="file_download")
        # Backdate past the 24h window via raw SQL (edi.log has _log_access=False;
        # timestamp is a plain datetime, not a fiscal-locked picking).
        old.flush_recordset()
        self.env.cr.execute(
            "UPDATE edi_log SET timestamp = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(hours=30), old.id))
        old.invalidate_recordset()
        queue = {q["label"]: q["n"] for q in self.svc.get_health_summary()["queue"]}
        self.assertEqual(queue["Polled"], 0)

    # ---- partner rail health states ----

    def test_health_state_never_without_inbound(self):
        # partner_b has no logs -> never polled.
        row = self._row_for(self.svc.get_health_summary(), self.partner_b)
        self.assertEqual(row["health"], "Not connected")

    def test_health_state_review_with_pending(self):
        self._log(event_type="file_download")  # sets last_poll
        self._review(state="pending_review")
        row = self._row_for(self.svc.get_health_summary(), self.trading_partner)
        self.assertEqual(row["health"], "Awaiting review")

    def test_health_state_error_beats_review(self):
        self._log(event_type="file_download")
        self._log(event_type="error", status="error", message="boom")
        self._review(state="pending_review")
        row = self._row_for(self.svc.get_health_summary(), self.trading_partner)
        self.assertEqual(row["health"], "Recent errors")

    def test_health_state_ok_when_clean(self):
        self._log(event_type="file_download")
        row = self._row_for(self.svc.get_health_summary(), self.trading_partner)
        self.assertEqual(row["health"], "Healthy")

    def test_health_state_circuit_open_beats_all(self):
        self._log(event_type="file_download")
        self._log(event_type="error", status="error", message="boom")
        self.trading_partner.write({
            "circuit_failure_count": 5,
            "circuit_open_since": fields.Datetime.now(),
        })
        row = self._row_for(self.svc.get_health_summary(), self.trading_partner)
        self.assertEqual(row["health"], "Circuit open")

    # ---- detail bundle ----

    def test_detail_stats_reflect_signals(self):
        self._log(event_type="file_download")
        self._review(state="pending_review")
        self._log(direction="outbound", event_type="ack_sent", status="success")
        d = self._detail_for(self.svc.get_health_summary(), self.trading_partner)
        stats = {s["k"]: s["v"] for s in d["stats"]}
        self.assertEqual(stats["Pending review"], "1")
        self.assertEqual(stats["Errors (24h)"], "0")
        # One success, no failures -> 100% on-time.
        self.assertEqual(stats["ORDRSP on-time"], "100.0%")
        self.assertNotEqual(stats["Last poll"], "never")

    def test_on_time_pct_with_failures(self):
        for _ in range(3):
            self._log(direction="outbound", event_type="ack_sent", status="success")
        self._log(direction="outbound", event_type="ack_sent", status="error")
        d = self._detail_for(self.svc.get_health_summary(), self.trading_partner)
        stats = {s["k"]: s["v"] for s in d["stats"]}
        self.assertEqual(stats["ORDRSP on-time"], "75.0%")

    def test_circuit_panel_closed_by_default(self):
        d = self._detail_for(self.svc.get_health_summary(), self.trading_partner)
        self.assertEqual(d["circuit"]["label"], "CLOSED")
        self.assertEqual(len(d["circuit"]["dots"]),
                         self.trading_partner.circuit_failure_threshold)

    def test_circuit_panel_open_when_tripped(self):
        self.trading_partner.write({
            "circuit_failure_count": 5,
            "circuit_open_since": fields.Datetime.now(),
        })
        d = self._detail_for(self.svc.get_health_summary(), self.trading_partner)
        self.assertEqual(d["circuit"]["label"], "OPEN")
        # All five dots filled red.
        self.assertTrue(all(c == "#dc3545" for c in d["circuit"]["dots"]))

    def test_transport_sftp_host_key_badge(self):
        # No host key yet -> warn; set one -> ok.
        d = self._detail_for(self.svc.get_health_summary(), self.partner_b)
        host_key_row = next(r for r in d["transport"] if r["k"] == "Host key")
        self.assertEqual(host_key_row["badge"], "warn")
        self.partner_b.sudo().write({"sftp_host_key": "AAAAB3NzaC1yc2E="})
        d2 = self._detail_for(self.svc.get_health_summary(), self.partner_b)
        host_key_row2 = next(r for r in d2["transport"] if r["k"] == "Host key")
        self.assertEqual(host_key_row2["badge"], "ok")

    def test_transport_ftp_has_no_host_key_row(self):
        d = self._detail_for(self.svc.get_health_summary(), self.trading_partner)
        keys = [r["k"] for r in d["transport"]]
        self.assertNotIn("Host key", keys)  # trading_partner is FTP
        self.assertIn("Protocol", keys)
        self.assertIn("Host", keys)

    def test_throughput_seven_day_buckets(self):
        self._log(event_type="file_download")
        self._log(event_type="order_created")
        d = self._detail_for(self.svc.get_health_summary(), self.trading_partner)
        self.assertEqual(len(d["throughput"]), 7)
        today = d["throughput"][-1]  # last bucket = today
        self.assertEqual(today["files"], 1)
        self.assertEqual(today["orders"], 1)

    def test_recent_exchanges_present_and_tagged(self):
        self._log(event_type="file_download", message="Downloaded X")
        d = self._detail_for(self.svc.get_health_summary(), self.trading_partner)
        self.assertTrue(d["exchanges"])
        self.assertEqual(d["exchanges"][0]["tag"], "POLL")

    def test_subtitle_counts_active_partners(self):
        summary = self.svc.get_health_summary()
        # Two active partners in the fixture.
        self.assertIn("2 active", summary["subtitle"])
        self.assertEqual(summary["settings_action"], "mml_edi.action_edi_trading_partner")
