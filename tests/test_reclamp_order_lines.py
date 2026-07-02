"""Integration tests for edi.processor.reclamp_order_lines (go-live FIX 4 / SS-1).

The shared approve-time availability gate: for short_ship partners every SO
line with edi_ordered_qty > 0 is re-clamped to min(edi_ordered_qty, DC-avail)
(floor 0), edi_qty_shortfall updated, changed lines returned, and ONE edi.log
warning written. Backorder partners are untouched. Requires a live Odoo DB
(odoo-bin --test-enable)."""
import hashlib
import unittest

from odoo.tests.common import TransactionCase, tagged

from .common import EDITestSetup, make_clean_parsed_order

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")
class TestReclampOrderLines(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.test_product.is_storable = True
        self.wh = self.env["stock.warehouse"].create(
            {"name": "Reclamp DC", "code": "RCDC", "roq_island": "north"})
        self.trading_partner.write({
            "oos_policy": "short_ship",
            "warehouse_id": self.wh.id,
            "auto_confirm_clean": False,
        })
        # 4 on hand; orders below request 10 -> parse-time clamp to 4, short 6.
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, 4)

    def _process(self, po_number, qty=10.0):
        order = make_clean_parsed_order(po_number=po_number, qty=qty)
        order.raw_data = "RECLAMP_RAW_%s" % po_number
        file_hash = hashlib.sha256(order.raw_data.encode()).hexdigest()
        self.processor.process_parsed_order(
            order, self.trading_partner, "reclamp.edi", file_hash)
        return self.env["edi.order.review"].search(
            [("customer_po_number", "=", po_number)], limit=1)

    def _line(self, review):
        return review.sale_order_id.order_line.filtered(
            lambda sol: sol.product_id == self.test_product)

    def _reclamp_logs(self):
        return self.env["edi.log"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("event_type", "=", "info"),
            ("status", "=", "warning"),
            ("message", "ilike", "Availability re-clamped%"),
        ])

    def test_reclamp_reduces_line_when_stock_dropped(self):
        review = self._process("RC-DROP-1")
        line = self._line(review)
        self.assertEqual(line.product_uom_qty, 4.0)
        # Stock drops to 1 between parse and approve.
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, -3)
        changes = self.env["edi.processor"].reclamp_order_lines(
            review.sale_order_id, self.trading_partner)
        self.assertEqual(line.product_uom_qty, 1.0)
        self.assertEqual(line.edi_qty_shortfall, 9.0)
        self.assertEqual(line.edi_ordered_qty, 10.0, "Ordered basis untouched")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["line"], line)
        self.assertEqual(changes[0]["old_qty"], 4.0)
        self.assertEqual(changes[0]["new_qty"], 1.0)
        self.assertEqual(changes[0]["shortfall"], 9.0)
        self.assertEqual(len(self._reclamp_logs()), 1,
                         "Exactly one edi.log warning row per reclamp")

    def test_reclamp_raises_line_when_stock_arrived(self):
        review = self._process("RC-UP-1")
        line = self._line(review)
        self.assertEqual(line.product_uom_qty, 4.0)
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, 100)
        changes = self.env["edi.processor"].reclamp_order_lines(
            review.sale_order_id, self.trading_partner)
        self.assertEqual(line.product_uom_qty, 10.0,
                         "Line restored toward ordered qty when stock arrives")
        self.assertEqual(line.edi_qty_shortfall, 0.0)
        self.assertEqual(len(changes), 1)

    def test_reclamp_noop_returns_empty_and_writes_no_log(self):
        review = self._process("RC-NOOP-1")
        changes = self.env["edi.processor"].reclamp_order_lines(
            review.sale_order_id, self.trading_partner)
        self.assertEqual(changes, [])
        self.assertFalse(self._reclamp_logs(),
                         "No log row when nothing changed")

    def test_backorder_partner_lines_untouched(self):
        review = self._process("RC-BO-1")
        line = self._line(review)
        self.trading_partner.oos_policy = "backorder"
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, -3)
        changes = self.env["edi.processor"].reclamp_order_lines(
            review.sale_order_id, self.trading_partner)
        self.assertEqual(changes, [])
        self.assertEqual(line.product_uom_qty, 4.0, "Backorder lines untouched")

    def test_missing_warehouse_fails_toward_zero(self):
        """Contract: NEVER raises on missing warehouse — availability degrades
        to 0.0 (fail-closed) and the line clamps to zero."""
        review = self._process("RC-NOWH-1")
        line = self._line(review)
        review.sale_order_id.warehouse_id = False
        changes = self.env["edi.processor"].reclamp_order_lines(
            review.sale_order_id, self.trading_partner)
        self.assertEqual(line.product_uom_qty, 0.0)
        self.assertEqual(line.edi_qty_shortfall, 10.0)
        self.assertEqual(len(changes), 1)
