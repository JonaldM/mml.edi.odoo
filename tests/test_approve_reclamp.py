"""Integration tests for the approve-time availability re-check (go-live FIX 1 / SS-1).

pending_review SOs hold no reservation, so stock can move between parse and
approve. EVERY approve path — action_approve, action_approve_corrected and the
bulk wizard — must re-clamp lines to live DC availability immediately before
action_confirm, record the clamp on the review (chatter + a qty_shortfall
issue when a line clamps to zero), and the subsequently generated ORDRSP must
carry the POST-reclamp quantities (the iDOC ACK reads the live SO line qty).
Requires a live Odoo DB (odoo-bin --test-enable)."""
import hashlib
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

from .common import (
    EDITestSetup,
    RecordingFTPHandler,
    make_clean_parsed_order,
    make_idoc_raw,
    make_island_warehouse,
)

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")
class TestApproveReclamp(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.test_product.is_storable = True
        self.wh = make_island_warehouse(self.env, "Approve Reclamp DC", "ARDC")
        self.trading_partner.write({
            "oos_policy": "short_ship",
            "warehouse_id": self.wh.id,
            "auto_confirm_clean": False,
        })
        # 10 on hand at parse time — orders of 10 pass the parse gate in full;
        # the tests then drain stock BEFORE approving.
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, 10)

    def _process(self, po_number, qty=10.0):
        order = make_clean_parsed_order(po_number=po_number, qty=qty)
        order.raw_data = "APPROVE_RECLAMP_RAW_%s" % po_number
        file_hash = hashlib.sha256(order.raw_data.encode()).hexdigest()
        self.processor.process_parsed_order(
            order, self.trading_partner, "approve-reclamp.edi", file_hash)
        return self.env["edi.order.review"].search(
            [("customer_po_number", "=", po_number)], limit=1)

    def _line(self, review):
        return review.sale_order_id.order_line.filtered(
            lambda sol: sol.product_id == self.test_product)

    def _drain_stock(self, qty):
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, -qty)

    # ── action_approve (covers the bulk wizard path too) ──────────────────

    def test_action_approve_reclamps_to_live_availability(self):
        review = self._process("AR-APPROVE-1")
        line = self._line(review)
        self.assertEqual(line.product_uom_qty, 10.0, "Parse gate passed in full")
        self.assertEqual(review.state, "pending_review")

        self._drain_stock(6)  # 4 left by approve time
        review.action_approve()

        self.assertEqual(review.state, "approved")
        self.assertEqual(review.sale_order_id.state, "sale")
        self.assertEqual(line.product_uom_qty, 4.0,
                         "Approve must re-clamp to stock at APPROVE time")
        self.assertEqual(line.edi_qty_shortfall, 6.0)
        self.assertEqual(line.edi_ordered_qty, 10.0, "Ordered basis untouched")
        messages = review.message_ids.mapped("body")
        self.assertTrue(
            any("re-clamped" in (body or "") for body in messages),
            "Reclamp must be recorded on the review chatter",
        )

    def test_action_approve_zero_stock_clamps_to_zero_and_raises_issue(self):
        review = self._process("AR-ZERO-1")
        line = self._line(review)
        self._drain_stock(10)  # nothing left

        review.action_approve()

        self.assertEqual(line.product_uom_qty, 0.0)
        self.assertEqual(line.edi_qty_shortfall, 10.0)
        self.assertEqual(review.sale_order_id.state, "sale",
                         "Zero-qty line must not break confirm (decision 5.3)")
        zero_issues = review.issue_ids.filtered(
            lambda i: i.issue_type == "qty_shortfall"
            and "ZERO" in (i.description or ""))
        self.assertEqual(len(zero_issues), 1,
                         "Clamp-to-zero must raise the standard qty_shortfall issue")
        self.assertEqual(zero_issues.resolution, "accepted",
                         "The approve flow resolves the issue it just raised")

    def test_action_approve_unchanged_stock_posts_no_reclamp_chatter(self):
        review = self._process("AR-NOOP-1")
        review.action_approve()
        self.assertEqual(self._line(review).product_uom_qty, 10.0)
        messages = review.message_ids.mapped("body")
        self.assertFalse(
            any("re-clamped" in (body or "") for body in messages),
            "No chatter noise when nothing changed",
        )

    def test_backorder_partner_approve_leaves_lines_untouched(self):
        review = self._process("AR-BO-1")
        self.trading_partner.oos_policy = "backorder"
        self._drain_stock(10)
        review.action_approve()
        self.assertEqual(self._line(review).product_uom_qty, 10.0,
                         "Backorder partners keep the legacy accept-in-full path")

    # ── action_approve_corrected ───────────────────────────────────────────

    def test_action_approve_corrected_reclamps_to_live_availability(self):
        review = self._process("AR-CORR-1")
        line = self._line(review)
        # Operator corrects the line, then stock drains before they approve.
        line.product_uom_qty = 8.0
        self._drain_stock(6)  # 4 left

        review.action_approve_corrected()

        self.assertEqual(review.state, "approved")
        self.assertEqual(line.product_uom_qty, 4.0,
                         "Corrected approvals must not promise stock that moved")
        self.assertEqual(line.edi_qty_shortfall, 6.0)

    def test_action_approve_corrected_preserves_operator_reduction(self):
        """Must-fix 2 (review 2026-07-02): an operator's manual reduction is
        a deliberate business decision. With plentiful stock, the corrected
        approve must NOT restore the line up to the customer's ordered qty —
        that would ship the exact quantity the human just rejected."""
        review = self._process("AR-CORR-2")  # ordered 10, 10 in stock
        line = self._line(review)
        line.product_uom_qty = 4.0  # deliberate operator cut, stock untouched

        review.action_approve_corrected()

        self.assertEqual(review.state, "approved")
        self.assertEqual(line.product_uom_qty, 4.0,
                         "Operator reduction must survive corrected approve")
        self.assertEqual(line.edi_qty_shortfall, 6.0,
                         "Shortfall reports against the customer's ordered 10")

    # ── bulk wizard ────────────────────────────────────────────────────────

    def test_bulk_approve_reclamps_to_live_availability(self):
        review = self._process("AR-BULK-1")
        self._drain_stock(6)  # 4 left

        wiz = self.env["edi.bulk.action"].with_context(
            active_ids=review.ids, active_model="edi.order.review",
        ).create({})
        wiz.action_approve_all()

        self.assertEqual(review.state, "approved")
        self.assertEqual(self._line(review).product_uom_qty, 4.0,
                         "Bulk approval must run the same SS-1 gate")

    # ── ORDRSP reflects the POST-reclamp quantity ──────────────────────────

    def test_ordrsp_wmeng_reflects_reclamped_qty(self):
        """The iDOC ACK reads the LIVE sol qty (kestrelby_idoc reads
        product_uom_qty per POSEX), so after an approve-time re-clamp the
        ORDRSP must carry the re-clamped DELIVERABLE qty in E1EDP20/WMENG
        (per the iDOC ORDRSP IG v1.7 — BMNG2 always echoes the ORDERED qty,
        and a partial line is ACTION=001 with NO ABGRU)."""
        try:
            from odoo.addons.mml_edi.models import edi_ftp as edi_ftp_mod
        except ImportError:
            from mml_edi.models import edi_ftp as edi_ftp_mod

        review = self._process("AR-ACK-1")
        line = self._line(review)
        # Swap in an iDOC raw file + the iDOC parser so generate_ack runs the
        # production Kestrelby path (POSEX 00001 == the line's edi_line_number 1).
        review.edi_raw_data = make_idoc_raw("AR-ACK-1", ordered_each=10.0)
        self.trading_partner.parser_class = (
            "mml_edi.parsers.kestrelby_idoc.KestrelbyIDOCParser")
        self._drain_stock(6)  # 4 left at approve time

        RecordingFTPHandler.reset()
        with mock.patch.object(edi_ftp_mod, "EDIFTPHandler", RecordingFTPHandler):
            review.action_approve()

        self.assertEqual(line.product_uom_qty, 4.0)
        parser = self.trading_partner.get_parser_instance()
        root = ET.fromstring(parser.generate_ack(review).decode("utf-8"))
        lines = root.findall("IDOC/E1EDP01")
        self.assertEqual(
            [float(p.find("E1EDP20/WMENG").text) for p in lines], [4.0],
            "ORDRSP WMENG must carry the RE-CLAMPED deliverable qty")
        self.assertEqual(
            [float(p.find("BMNG2").text) for p in lines], [10.0],
            "BMNG2 always echoes the customer's ORDERED qty (IG v1.7)")
        self.assertEqual(lines[0].find("ACTION").text, "001")
        self.assertIsNone(lines[0].find("ABGRU"),
                          "partial supply must NOT carry ABGRU (only ACTION=003 may)")
