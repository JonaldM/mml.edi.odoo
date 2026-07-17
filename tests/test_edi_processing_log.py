# mml_edi/tests/test_edi_processing_log.py
"""Integration tests for the edi.processing.log computation service.

Exercises the two batched read-mostly methods against real edi.log rows: the
day-grouped/filtered row list (direction + status filters, free-text search,
selectable flags, day labels) and the per-file exchange chain (sibling grouping
by SHA-256, dedup status, hash descriptor, chain ordering, and the manager-only
technical-detail gate).

Tagged post_install (the shared EDITestSetup fixture creates products, which at
at_install time hits registry-order NOT NULLs).
"""
from odoo.tests import TransactionCase, tagged

from .common import EDITestSetup


@tagged("post_install", "-at_install")
class TestEdiProcessingLog(TransactionCase, EDITestSetup):
    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.svc = self.env["edi.processing.log"]

    # ---- helpers ----

    def _log(self, direction="inbound", event_type="file_download", status="success",
             message="msg", filename=None, file_hash=None, detail=None):
        return self.env["edi.log"].log(
            self.trading_partner, direction, event_type, status, message,
            filename=filename, file_hash=file_hash, detail=detail)

    # ---- row list ----

    def test_page_returns_rows_newest_first(self):
        self._log(message="first", filename="A.xml")
        self._log(event_type="file_parse", message="second", filename="A.xml")
        page = self.svc.get_log_page()
        self.assertEqual(page["count"], 2)
        # Newest first: the parse event was logged last.
        self.assertEqual(page["rows"][0]["message"], "second")

    def test_direction_filter(self):
        self._log(direction="inbound", filename="in.xml")
        self._log(direction="outbound", event_type="ack_sent", filename="out.edi")
        inbound = self.svc.get_log_page(direction="inbound")
        self.assertEqual(inbound["count"], 1)
        self.assertEqual(inbound["rows"][0]["filename"], "in.xml")

    def test_status_filter(self):
        self._log(status="success", filename="ok.xml")
        self._log(event_type="error", status="error", message="boom", filename="bad.xml")
        errors = self.svc.get_log_page(status="error")
        self.assertEqual(errors["count"], 1)
        self.assertEqual(errors["rows"][0]["status"], "Error")

    def test_query_matches_filename_and_message(self):
        self._log(message="Downloaded from VAN", filename="ORDERS_4500.xml")
        self._log(message="unrelated", filename="OTHER.xml")
        by_file = self.svc.get_log_page(query="4500")
        self.assertEqual(by_file["count"], 1)
        by_msg = self.svc.get_log_page(query="Downloaded")
        self.assertEqual(by_msg["count"], 1)

    def test_row_tag_and_status_presentation(self):
        self._log(event_type="file_download", status="success", filename="A.xml")
        row = self.svc.get_log_page()["rows"][0]
        self.assertEqual(row["tag"], "POLL")
        self.assertEqual(row["status"], "OK")
        self.assertTrue(row["selectable"])  # has a filename
        self.assertTrue(row["day"].startswith("Today"))

    def test_row_without_file_not_selectable(self):
        self._log(direction="internal", event_type="review_approved",
                  message="Review approved", filename=None)
        row = self.svc.get_log_page()["rows"][0]
        self.assertFalse(row["selectable"])
        self.assertEqual(row["tag"], "APPROVED")

    def test_first_selectable_reported(self):
        self._log(direction="internal", event_type="review_approved",
                  message="no file", filename=None)
        target = self._log(event_type="file_download", filename="A.xml")
        page = self.svc.get_log_page()
        self.assertEqual(page["first_selectable"], target.id)

    def test_limit_caps_rows(self):
        for i in range(5):
            self._log(filename="F%d.xml" % i)
        page = self.svc.get_log_page(limit=3)
        self.assertEqual(page["count"], 3)

    # ---- exchange chain ----

    def test_exchange_chain_groups_siblings_by_hash(self):
        h = "9a4f21c8e0b7d34a1f9a4f21c8e0b7d3"
        dl = self._log(event_type="file_download", message="Downloaded",
                       filename="ORDERS.xml", file_hash=h)
        self._log(event_type="file_parse", message="Parsed", filename="ORDERS.xml", file_hash=h)
        self._log(direction="outbound", event_type="ack_sent", status="success",
                  message="ORDRSP sent", filename="ACK.edi", file_hash=h)
        ex = self.svc.get_exchange_chain(dl.id)
        self.assertEqual(len(ex["chain"]), 3)
        # Chain is timestamp-ordered: download first, ACK last.
        self.assertEqual(ex["chain"][0]["title"], "File downloaded")
        self.assertEqual(ex["chain"][-1]["title"], "ORDRSP sent")
        # Every node but the last draws a connector line.
        self.assertTrue(ex["chain"][0]["line"])
        self.assertFalse(ex["chain"][-1]["line"])
        # A successful ACK -> green idempotent dedup chip.
        self.assertTrue(ex["dedup"]["ok"])
        self.assertIn("idempotent", ex["dedup"]["label"])
        self.assertIn("9a4f21c8", ex["hash"])

    def test_exchange_chain_duplicate_status(self):
        h = "c71e0f92aa045512bbc71e0f92aa0455"
        self._log(event_type="file_download", filename="ORDERS.xml", file_hash=h)
        dup = self._log(event_type="duplicate_skipped", status="warning",
                        message="Duplicate skipped", filename="ORDERS.xml", file_hash=h)
        ex = self.svc.get_exchange_chain(dup.id)
        self.assertFalse(ex["dedup"]["ok"])
        self.assertIn("duplicate", ex["dedup"]["label"])
        self.assertIn("matched a processed file", ex["hash"])

    def test_exchange_chain_failed_ack_retry_armed(self):
        h = "3d1180aa17f29be0043d1180aa17f29b"
        claim = self._log(direction="outbound", event_type="ack_sent", status="warning",
                          message="Upload claimed", filename="ACK.edi", file_hash=h)
        self._log(direction="outbound", event_type="ack_sent", status="error",
                  message="SFTP timeout", filename="ACK.edi", file_hash=h,
                  detail="paramiko.ssh_exception.SSHException: connect timeout")
        ex = self.svc.get_exchange_chain(claim.id)
        self.assertFalse(ex["dedup"]["ok"])
        self.assertIn("retry armed", ex["dedup"]["label"])
        # A manager sees the technical detail of the errored node.
        self.assertTrue(ex["has_detail"])
        self.assertIn("SSHException", ex["detail"])

    def test_exchange_chain_falls_back_to_filename(self):
        # No hash -> siblings grouped by filename + partner.
        a = self._log(event_type="file_download", message="dl", filename="NOHASH.xml")
        self._log(event_type="file_parse", message="parse", filename="NOHASH.xml")
        ex = self.svc.get_exchange_chain(a.id)
        self.assertEqual(len(ex["chain"]), 2)
        self.assertIn("no hash recorded", ex["hash"])

    def test_exchange_chain_missing_log_returns_none(self):
        self.assertIsNone(self.svc.get_exchange_chain(999999999))

    def test_technical_detail_hidden_from_non_manager(self):
        h = "aa11bb22cc33dd44aa11bb22cc33dd44"
        err = self._log(direction="outbound", event_type="ack_sent", status="error",
                        message="fail", filename="ACK.edi", file_hash=h,
                        detail="secret stack trace")
        user = self.env["res.users"].create({
            "name": "EDI Operator",
            "login": "edi_operator_test",
            "groups_id": [(6, 0, [self.env.ref("mml_edi.group_edi_user").id])],
        })
        ex = self.svc.with_user(user).get_exchange_chain(err.id)
        self.assertFalse(ex["has_detail"])
        self.assertEqual(ex["detail"], "")
