"""Integration tests for the ORDCHG oversell gate (go-live FIX 5 / SS-5).

apply_change_order used to write product_uom_qty straight from the ORDCHG
with no availability check — a qty increase beyond stock was confirmed and
ACKed in full. Now short_ship partners re-run reclamp_order_lines after the
changes are applied, edi_ordered_qty tracks the customer's NEW ordered qty,
and the clamp is reflected in the audit trail. Requires a live Odoo DB."""
import hashlib
import unittest

from odoo.tests.common import TransactionCase, tagged

from .common import (
    EDITestSetup,
    make_change_order_parsed_order,
    make_clean_parsed_order,
    make_island_warehouse,
)

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")
class TestOrdchgAvailabilityGate(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.test_product.is_storable = True
        self.wh = make_island_warehouse(self.env, "ORDCHG DC", "OCDC")
        self.trading_partner.write({
            "oos_policy": "short_ship",
            "warehouse_id": self.wh.id,
            "auto_confirm_clean": False,
        })
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, 4)

    def _process(self, order, filename):
        file_hash = hashlib.sha256((order.raw_data or filename).encode()).hexdigest()
        self.processor.process_parsed_order(
            order, self.trading_partner, filename, file_hash)

    def _setup_order_with_change(self, po_number, change_qty):
        """Create the original order (10 ordered, clamped to 4) then route an
        ORDCHG raising the line to change_qty. Returns the change review."""
        original = make_clean_parsed_order(po_number=po_number, qty=10.0)
        original.raw_data = "OC_ORIG_%s" % po_number
        self._process(original, "oc_orig.edi")

        change = make_change_order_parsed_order(po_number=po_number, qty=change_qty)
        change.raw_data = "OC_CHG_%s" % po_number
        self._process(change, "oc_chg.edi")

        return self.env["edi.order.review"].search([
            ("customer_po_number", "=", po_number),
            ("document_type", "=", "change_order"),
        ], limit=1)

    def _line(self, review):
        return review.sale_order_id.order_line.filtered(
            lambda sol: sol.product_id == self.test_product)

    def test_ordchg_increase_is_reclamped_to_availability(self):
        review = self._setup_order_with_change("OC-GATE-1", change_qty=50.0)
        self.assertTrue(review, "ORDCHG must create a change_order review")
        review.action_approve()
        line = self._line(review)
        self.assertEqual(line.edi_ordered_qty, 50.0,
                         "ORDCHG redefines the customer's ordered qty")
        self.assertEqual(line.product_uom_qty, 4.0,
                         "Raised qty must be clamped to DC availability")
        self.assertEqual(line.edi_qty_shortfall, 46.0,
                         "Shortfall drives the ORDRSP — must reflect the clamp")

    def test_ordchg_increase_with_stock_applies_in_full(self):
        review = self._setup_order_with_change("OC-GATE-2", change_qty=50.0)
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, 100)  # 104 on hand
        review.action_approve()
        line = self._line(review)
        self.assertEqual(line.product_uom_qty, 50.0)
        self.assertEqual(line.edi_qty_shortfall, 0.0)

    def test_ordchg_clamp_is_recorded_in_audit_trail(self):
        review = self._setup_order_with_change("OC-GATE-3", change_qty=50.0)
        review.action_approve()
        # ONE edi.log warning row from the reclamp
        logs = self.env["edi.log"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("event_type", "=", "info"),
            ("status", "=", "warning"),
            ("message", "ilike", "Availability re-clamped%"),
        ])
        self.assertEqual(len(logs), 1)
        # and the clamp is posted on the review's chatter
        messages = review.message_ids.mapped("body")
        self.assertTrue(
            any("re-clamped" in (body or "") for body in messages),
            "Reclamp must be reflected in the review audit trail",
        )

    def test_ordchg_backorder_partner_applies_raw_qty(self):
        self.trading_partner.oos_policy = "backorder"
        review = self._setup_order_with_change("OC-GATE-4", change_qty=50.0)
        review.action_approve()
        line = self._line(review)
        self.assertEqual(line.product_uom_qty, 50.0,
                         "Backorder partners keep the legacy accept-in-full path")

    def test_ordchg_on_confirmed_so_skips_reclamp(self):
        """Must-fix 1 (review 2026-07-02): a CONFIRMED SO has reserved its
        own stock, which free_qty nets OUT — reclamping it would count the
        order against itself and clamp healthy, fully-reserved lines toward
        zero (then ACK 'cannot supply' stock reserved for this very PO). On
        a confirmed SO the ORDCHG applies the raw qty and the reclamp is
        SKIPPED — the human change-order review is the gate."""
        review = self._setup_order_with_change("OC-GATE-5", change_qty=50.0)
        so = review.sale_order_id
        so.action_confirm()  # reserves the parse-clamped 4 units
        self.assertEqual(so.state, "sale")

        review.action_approve()
        line = self._line(review)
        self.assertEqual(line.edi_ordered_qty, 50.0,
                         "ORDCHG still redefines the ordered basis")
        self.assertEqual(
            line.product_uom_qty, 50.0,
            "Confirmed SO: raw ORDCHG qty applies — NO reclamp against the "
            "order's own reservation (it would clamp reserved stock to 0)")
