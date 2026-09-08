"""retry_pending_acks must bound its candidate scan.

The cron ships active at a 30-minute interval and used to search EVERY
resolved review ever created, then issue two search_counts per exchange. With
one review per store per PO (Briscoes splits a PO across up to 47 stores) the
cost grows monotonically forever, even though rows resolved long ago can no
longer produce work. Requires a live Odoo DB (odoo-bin --test-enable).
"""
import unittest
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

from .common import EDITestSetup, RecordingFTPHandler, make_idoc_raw

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime - run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")
class TestRetryPendingAcksWindow(EDITestSetup, TransactionCase):

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

    def _make_resolved_review(self, po, file_hash):
        so = self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "edi_trading_partner_id": self.trading_partner.id,
            "client_order_ref": "%s_A" % po,
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
            "state": "approved",
        })

    def _age(self, review, days):
        """Backdate write_date past the retry window (it is ORM-managed)."""
        self.env.cr.execute(
            "UPDATE edi_order_review "
            "SET write_date = (now() at time zone 'UTC') - (%s * interval '1 day') "
            "WHERE id = %s",
            (days, review.id),
        )
        review.invalidate_recordset(["write_date"])

    def _was_acked(self, review):
        """Whether THIS review's exchange was uploaded. Asserted by filename
        rather than by upload count: the gate runs on a prod clone, where other
        reviews may legitimately be re-queued in the same pass."""
        return review._ack_exchange_filename() in RecordingFTPHandler.uploads

    def test_recent_unacked_review_is_still_retried(self):
        review = self._make_resolved_review("PO-WINDOW-NEW", "windowhash000001")
        self.env["edi.processor"].retry_pending_acks()
        self.assertTrue(
            self._was_acked(review),
            "A resolved review inside the window must still be re-queued")

    def test_review_older_than_the_window_is_not_scanned(self):
        review = self._make_resolved_review("PO-WINDOW-OLD", "windowhash000002")
        self._age(review, 60)
        self.env["edi.processor"].retry_pending_acks()
        self.assertFalse(
            self._was_acked(review),
            "A review resolved long ago must fall outside the retry window")

    def test_window_can_be_disabled_with_the_config_parameter(self):
        review = self._make_resolved_review("PO-WINDOW-OFF", "windowhash000003")
        self._age(review, 60)
        self.env["ir.config_parameter"].sudo().set_param(
            "mml_edi.ack_retry_window_days", "0")
        self.env["edi.processor"].retry_pending_acks()
        self.assertTrue(
            self._was_acked(review),
            "Setting the window to 0 must restore the unbounded scan")
