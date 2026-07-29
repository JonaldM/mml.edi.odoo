"""Integration tests for the committed ACK send-claim (go-live FIX 2 / IDEM-3).

The old send-once guard was a racy filename search_count: an FTP ghost-success
(upload stored on the server but the final 226 reply lost) was logged
ack_sent/error, so the 30-min retry cron blindly re-uploaded a file the VAN
may have already swept — SAP got two ORDRSPs. Now _queue_ack writes an
'ack_sending' claim row BEFORE the upload (committed outside test mode), and
any retry that sees a claim must VERIFY via the outbound FTP listing: mark
sent without re-uploading when the file is present, re-upload only when it is
provably absent, and fail closed (no upload) when the listing itself fails.
Requires a live Odoo DB (odoo-bin --test-enable)."""
import unittest
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

from .common import EDITestSetup, RecordingFTPHandler, make_idoc_raw

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")
class TestAckSendClaim(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        # Real production ACK path: the iDOC parser (allow-listed) working off
        # the review's stored raw iDOC. FTP is a recording fake — no network.
        self.trading_partner.parser_class = (
            "mml_edi.parsers.kestrelby_idoc.KestrelbyIDOCParser")
        try:
            from odoo.addons.mml_edi.models import edi_ftp as edi_ftp_mod
        except ImportError:
            from mml_edi.models import edi_ftp as edi_ftp_mod
        RecordingFTPHandler.reset()
        patcher = mock.patch.object(
            edi_ftp_mod, "EDIFTPHandler", RecordingFTPHandler)
        patcher.start()
        self.addCleanup(patcher.stop)
        try:
            from odoo.addons.mml_edi.parsers.base_parser import EDIFTPError
        except ImportError:
            from mml_edi.parsers.base_parser import EDIFTPError
        self.EDIFTPError = EDIFTPError
        self.review = self._make_review("PO-CLAIM-1", "claimhash0000001")

    def _make_review(self, po, file_hash, ref_suffix="A"):
        so = self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "edi_trading_partner_id": self.trading_partner.id,
            "client_order_ref": "%s_%s" % (po, ref_suffix),
        })
        self.env["sale.order.line"].create({
            "order_id": so.id,
            "product_id": self.test_product.id,
            "product_uom_qty": 10.0,
            "price_unit": 9.99,
            "edi_line_number": 1,
            "edi_ordered_qty": 10.0,
        })
        return self.env["edi.order.review"].create({
            "trading_partner_id": self.trading_partner.id,
            "customer_po_number": po,
            "sale_order_id": so.id,
            "edi_file_hash": file_hash,
            "edi_raw_data": make_idoc_raw(po),
            "document_type": "new_order",
            "state": "pending_review",
        })

    def _logs(self, event_type, status=None):
        domain = [
            ("trading_partner_id", "=", self.trading_partner.id),
            ("event_type", "=", event_type),
        ]
        if status:
            domain.append(("status", "=", status))
        return self.env["edi.log"].search(domain)

    def _ack_status(self, review):
        review.invalidate_recordset(["ack_status", "ack_date"])
        return review.ack_status

    # ── happy path ─────────────────────────────────────────────────────────

    def test_clean_send_writes_claim_before_marking_sent(self):
        self.review.action_approve()
        fn = self.review._ack_exchange_filename()
        self.assertEqual(RecordingFTPHandler.uploads, [fn])
        self.assertEqual(len(self._logs("ack_sending")), 1,
                         "Every send is preceded by exactly one claim row")
        self.assertEqual(self._logs("ack_sending").filename, fn)
        self.assertEqual(len(self._logs("ack_sent", "success")), 1)
        self.assertEqual(self._ack_status(self.review), "sent")

    # ── ghost-success recovery ─────────────────────────────────────────────

    def test_ghost_success_retry_verifies_and_never_resends(self):
        RecordingFTPHandler.upload_error = self.EDIFTPError("226 lost mid-air")
        self.review.action_approve()
        fn = self.review._ack_exchange_filename()
        # Upload was STORED (recorded) but reported as an error.
        self.assertEqual(RecordingFTPHandler.uploads, [fn])
        self.assertTrue(self._logs("ack_sent", "error"))
        self.assertFalse(self._logs("ack_sent", "success"))
        self.assertEqual(self._ack_status(self.review), "failed")

        # Retry (as the cron would) with the file PRESENT on the server:
        # must mark sent WITHOUT a second upload.
        RecordingFTPHandler.upload_error = None
        RecordingFTPHandler.listing = [fn]
        self.review._queue_ack()

        self.assertEqual(RecordingFTPHandler.uploads, [fn],
                         "A delivered ACK must NEVER be re-uploaded")
        sent = self._logs("ack_sent", "success")
        self.assertEqual(len(sent), 1)
        self.assertIn("NOT re-uploaded", sent.message)
        self.assertEqual(self._ack_status(self.review), "sent")

    def test_retry_reuploads_when_provably_absent(self):
        RecordingFTPHandler.upload_error = self.EDIFTPError("connection reset")
        self.review.action_approve()
        fn = self.review._ack_exchange_filename()

        RecordingFTPHandler.upload_error = None
        RecordingFTPHandler.listing = []  # server-side listing: file absent
        self.review._queue_ack()

        self.assertEqual(RecordingFTPHandler.uploads, [fn, fn],
                         "Provably-absent file is re-uploaded exactly once")
        self.assertEqual(len(self._logs("ack_sent", "success")), 1)
        self.assertEqual(len(self._logs("ack_sending")), 1,
                         "The original claim keeps guarding later retries")
        self.assertEqual(self._ack_status(self.review), "sent")

    def test_listing_failure_fails_closed_without_uploading(self):
        RecordingFTPHandler.upload_error = self.EDIFTPError("226 lost mid-air")
        self.review.action_approve()
        fn = self.review._ack_exchange_filename()

        RecordingFTPHandler.upload_error = None
        RecordingFTPHandler.listing_error = self.EDIFTPError("LIST refused")
        self.review._queue_ack()

        self.assertEqual(RecordingFTPHandler.uploads, [fn],
                         "Cannot verify -> must NOT re-upload (fail closed)")
        self.assertFalse(self._logs("ack_sent", "success"))
        self.assertEqual(self._ack_status(self.review), "failed",
                         "Left failed so the retry cron tries again later")

    def test_retry_cron_uses_the_verify_path(self):
        RecordingFTPHandler.upload_error = self.EDIFTPError("226 lost mid-air")
        self.review.action_approve()
        fn = self.review._ack_exchange_filename()

        RecordingFTPHandler.upload_error = None
        RecordingFTPHandler.listing = [fn]
        self.env["edi.processor"].retry_pending_acks()

        self.assertEqual(RecordingFTPHandler.uploads, [fn],
                         "The 30-min retry cron must verify, not blind-resend")
        self.assertEqual(self._ack_status(self.review), "sent")

    def test_first_send_does_not_consult_the_listing(self):
        # A fresh exchange has no claim — its filename cannot pre-exist, so
        # the listing (even a poisoned one) must not be consulted.
        RecordingFTPHandler.listing_error = self.EDIFTPError("LIST refused")
        self.review.action_approve()
        self.assertEqual(len(RecordingFTPHandler.uploads), 1)
        self.assertEqual(self._ack_status(self.review), "sent")
