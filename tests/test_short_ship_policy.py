"""Integration tests for the OOS short-ship policy (Workstream A end-to-end).

A short_ship trading partner reduces each line to the qty available at the
fulfilment DC and records the shortfall (which drives the ORDRSP). A backorder
partner is unchanged. Requires a live Odoo DB (odoo-bin --test-enable)."""
import hashlib
import unittest

from odoo.tests.common import TransactionCase, tagged

from .common import EDITestSetup, make_clean_parsed_order, make_island_warehouse

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")
class TestShortShipPolicy(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        # EDITestSetup's product is a non-storable consu; make it storable so stock
        # can be seeded for the short-ship availability check.
        self.test_product.is_storable = True
        # Route EDI orders to a controlled, island-tagged warehouse so availability
        # flows through the real _fulfilment_free_qty path (not the untagged fallback).
        self.wh = make_island_warehouse(self.env, "Short Ship DC", "SSDC")
        self.trading_partner.write({
            "oos_policy": "short_ship",
            "warehouse_id": self.wh.id,
            "auto_confirm_clean": False,
        })
        # 4 on hand; the orders below request 10 -> ship 4, short 6.
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, 4)

    def _process(self, order):
        file_hash = hashlib.sha256((order.raw_data or "x").encode()).hexdigest()
        self.processor.process_parsed_order(
            order, self.trading_partner, "ss.edi", file_hash)
        return self.env["edi.order.review"].search(
            [("customer_po_number", "=", order.po_number)], limit=1)

    def _line(self, review):
        return review.sale_order_id.order_line.filtered(
            lambda sol: sol.product_id == self.test_product)

    def test_short_ship_reduces_line_and_records_shortfall(self):
        order = make_clean_parsed_order(po_number="SS-SHORT-001", qty=10.0)
        order.raw_data = "SS_SHORT_RAW"
        line = self._line(self._process(order))
        self.assertEqual(line.edi_ordered_qty, 10.0, "Original ordered qty preserved")
        self.assertEqual(line.product_uom_qty, 4.0, "Line short-shipped to DC-available")
        self.assertEqual(line.edi_qty_shortfall, 6.0)

    def test_full_availability_ships_in_full(self):
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, 100)  # 104 on hand now
        order = make_clean_parsed_order(po_number="SS-FULL-001", qty=10.0)
        order.raw_data = "SS_FULL_RAW"
        line = self._line(self._process(order))
        self.assertEqual(line.product_uom_qty, 10.0)
        self.assertEqual(line.edi_qty_shortfall, 0.0)

    def test_backorder_policy_accepts_in_full(self):
        self.trading_partner.oos_policy = "backorder"
        order = make_clean_parsed_order(po_number="SS-BO-001", qty=10.0)
        order.raw_data = "SS_BO_RAW"
        line = self._line(self._process(order))
        self.assertEqual(line.product_uom_qty, 10.0, "Backorder accepts in full")
        self.assertEqual(line.edi_ordered_qty, 10.0)

    def test_fully_oos_line_ships_zero_and_order_still_confirms(self):
        """Decision 5.3 (empirical): a fully-OOS line (qty 0) must not break
        auto-confirm. The pure ORDRSP test uses a mock SO; this exercises a real
        Odoo SO + action_confirm with a zero-qty line."""
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, -4)  # net 0 on hand
        self.trading_partner.auto_confirm_clean = True
        order = make_clean_parsed_order(po_number="SS-OOS-001", qty=10.0)
        order.raw_data = "SS_OOS_RAW"
        review = self._process(order)
        line = self._line(review)
        self.assertEqual(line.product_uom_qty, 0.0, "Fully OOS ships zero")
        self.assertEqual(line.edi_ordered_qty, 10.0)
        self.assertEqual(line.edi_qty_shortfall, 10.0)
        self.assertEqual(review.sale_order_id.state, "sale",
                         "Auto-confirm must tolerate a zero-qty line (decision 5.3)")
