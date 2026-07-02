"""Integration tests for reset/re-approve ACK re-send (go-live FIX 3 / IDEM-4).

action_reset_to_review used to leave the old ack_sent/success log matching the
identical exchange filename, so a corrected re-approve silently never sent —
Briscoes kept the stale confirmed quantities. Now a reset AFTER a sent ACK
bumps ack_attempt on every sibling store-review of the exchange; the filename
gains an '_aN' suffix, so the corrected ORDRSP goes out as a fresh exchange,
_compute_ack_status reads the LATEST attempt, and retry_pending_acks targets
the attempt filename. Requires a live Odoo DB (odoo-bin --test-enable)."""
import unittest
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

from .common import EDITestSetup, RecordingFTPHandler, make_idoc_raw

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")
class TestAckResetReapprove(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.trading_partner.parser_class = (
            "mml_edi.parsers.briscoes_idoc.BriscoesIDOCParser")
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
        # Reset requires the edi_manager group (test_review_workflow pattern).
        self.admin = self.env.ref("base.user_admin")
        self.admin.write({"group_ids": [
            (4, self.env.ref("mml_edi.group_edi_manager").id)]})
        self.review = self._make_review("PO-RESET-1", "resethash0000001")

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

    def _reset(self, review):
        review.with_user(self.admin).action_reset_to_review()

    def _ack_status(self, review):
        review.invalidate_recordset(["ack_status", "ack_date"])
        return review.ack_status

    # ── the IDEM-4 scenario: approve -> sent -> reset -> correct -> re-approve ──

    def test_reset_correct_reapprove_sends_exactly_one_fresh_attempt(self):
        review = self.review
        review.action_approve()
        fn1 = review._ack_exchange_filename()
        self.assertEqual(RecordingFTPHandler.uploads, [fn1])
        self.assertEqual(self._ack_status(review), "sent")

        self._reset(review)
        self.assertEqual(review.state, "pending_review")
        self.assertEqual(review.ack_attempt, 2,
                         "Reset after a SENT ACK must move to attempt 2")

        # Operator corrects a quantity, then re-approves.
        review.sale_order_id.order_line[0].product_uom_qty = 8.0
        review.action_approve_corrected()

        fn2 = review._ack_exchange_filename()
        self.assertNotEqual(fn1, fn2)
        self.assertTrue(fn2.endswith("_a2.edi"),
                        "Attempt 2 gets its own exchange filename")
        self.assertEqual(RecordingFTPHandler.uploads, [fn1, fn2],
                         "Exactly one NEW ACK for attempt 2")
        self.assertEqual(self._ack_status(review), "sent")

    def test_reset_before_anything_sent_keeps_the_same_exchange(self):
        RecordingFTPHandler.upload_error = self.EDIFTPError("FTP down")
        self.review.action_approve()  # errored — nothing delivered
        fn1 = self.review._ack_exchange_filename()

        self._reset(self.review)
        self.assertEqual(self.review.ack_attempt, 1,
                         "No bump when the ACK was never sent")
        self.assertEqual(self.review._ack_exchange_filename(), fn1)

    def test_siblings_share_the_attempt_bump(self):
        # Two store-reviews of the same PO/file: they share the exchange
        # filename, so a reset of ONE must move BOTH to the new attempt.
        rev_a = self.review
        rev_b = self._make_review("PO-RESET-1", "resethash0000001",
                                  ref_suffix="B")
        rev_a.action_approve()
        self.assertEqual(RecordingFTPHandler.uploads, [],
                         "ACK deferred while a sibling is still pending")
        rev_b.action_approve()
        self.assertEqual(len(RecordingFTPHandler.uploads), 1,
                         "One ACK for the whole exchange")

        self._reset(rev_a)
        self.assertEqual(rev_a.ack_attempt, 2)
        self.assertEqual(rev_b.ack_attempt, 2,
                         "Siblings share the exchange -> share the attempt")

    def test_ack_status_reads_the_latest_attempt(self):
        review = self.review
        review.action_approve()
        self.assertEqual(self._ack_status(review), "sent")

        self._reset(review)
        RecordingFTPHandler.upload_error = self.EDIFTPError("FTP down")
        review.action_approve_corrected()

        self.assertEqual(
            self._ack_status(review), "failed",
            "Status must reflect attempt 2's failure, not attempt 1's success")

    def test_supersede_writes_an_audit_log_row(self):
        self.review.action_approve()
        fn1 = self.review._ack_exchange_filename()
        self._reset(self.review)
        logs = self.env["edi.log"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("event_type", "=", "info"),
            ("status", "=", "warning"),
            ("message", "ilike", "superseded by reset%"),
        ])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs.filename, fn1)

    def test_retry_cron_targets_the_attempt_filename(self):
        review = self.review
        review.action_approve()  # attempt 1 sent
        self._reset(review)
        RecordingFTPHandler.upload_error = self.EDIFTPError("FTP down")
        review.action_approve_corrected()  # attempt 2 errored (claim written)

        RecordingFTPHandler.upload_error = None
        RecordingFTPHandler.listing = []  # attempt-2 file provably absent
        self.env["edi.processor"].retry_pending_acks()

        fn2 = review._ack_exchange_filename()
        self.assertTrue(fn2.endswith("_a2.edi"))
        # The fake records the errored first try too, so fn2 appears twice:
        # the failed upload + the cron's verified re-send. The point is the
        # cron acted on attempt 2 AT ALL despite attempt 1's success log.
        self.assertEqual(RecordingFTPHandler.uploads.count(fn2), 2)
        self.assertEqual(self._ack_status(review), "sent")
