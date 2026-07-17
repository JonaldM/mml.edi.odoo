"""Integration tests for the post-confirm reservation verify (go-live FIX 6 / SS-3).

After every action_confirm the processor performs, verify_order_reservation
must call action_assign() on the outgoing picking(s) (idempotent) and write an
edi.log warning listing any move that failed to reserve. It also raises a
config sanity warning when the DC outgoing picking type's reservation_method
is not 'at_confirm'. Requires a live Odoo DB (odoo-bin --test-enable)."""
import hashlib
import unittest

from odoo.tests.common import TransactionCase, tagged

from .common import EDITestSetup, make_clean_parsed_order

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")
class TestReservationVerify(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.test_product.is_storable = True
        self.wh = self.env["stock.warehouse"].create(
            {"name": "Reserve DC", "code": "RVDC", "roq_island": "north"})
        # backorder + auto-confirm: accept-in-full so a shortfall leaves a
        # genuinely under-reserved move to detect.
        self.trading_partner.write({
            "oos_policy": "backorder",
            "warehouse_id": self.wh.id,
            "auto_confirm_clean": True,
        })

    def _process(self, po_number, qty):
        order = make_clean_parsed_order(po_number=po_number, qty=qty)
        order.raw_data = "RESERVE_RAW_%s" % po_number
        file_hash = hashlib.sha256(order.raw_data.encode()).hexdigest()
        self.processor.process_parsed_order(
            order, self.trading_partner, "reserve.edi", file_hash)
        return self.env["edi.order.review"].search(
            [("customer_po_number", "=", po_number)], limit=1)

    def _unreserved_logs(self):
        return self.env["edi.log"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("event_type", "=", "info"),
            ("status", "=", "warning"),
            ("message", "ilike", "%failed to reserve%"),
        ])

    def test_full_stock_reserves_and_no_warning(self):
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, 100)
        review = self._process("RV-FULL-1", qty=10.0)
        so = review.sale_order_id
        self.assertEqual(so.state, "sale")
        outgoing = so.picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing")
        self.assertTrue(outgoing, "Confirm must generate an outgoing picking")
        moves = outgoing.move_ids.filtered(lambda m: m.product_uom_qty > 0)
        self.assertTrue(all(m.state == "assigned" for m in moves),
                        "All moves must be reserved after the verify")
        self.assertFalse(self._unreserved_logs())

    def test_shortfall_logs_unreserved_warning(self):
        # 4 on hand, 10 accepted in full (backorder policy) -> move can only
        # partially reserve; the verify must surface it.
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, 4)
        review = self._process("RV-SHORT-1", qty=10.0)
        self.assertEqual(review.sale_order_id.state, "sale")
        logs = self._unreserved_logs()
        self.assertEqual(len(logs), 1,
                         "Under-reservation must write ONE edi.log warning")
        self.assertIn(self.test_product.display_name, logs.message,
                      "The warning must list the offending move's product")

    def test_reservation_method_config_warning(self):
        if "reservation_method" not in self.env["stock.picking.type"]._fields:
            self.skipTest("reservation_method not present on this Odoo version")
        self.wh.out_type_id.reservation_method = "manual"
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, 100)
        self._process("RV-CONF-1", qty=10.0)
        logs = self.env["edi.log"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("event_type", "=", "info"),
            ("status", "=", "warning"),
            ("message", "ilike", "%reservation_method%"),
        ])
        self.assertTrue(logs, "Non-at_confirm reservation_method must warn")

    def test_verify_is_idempotent_and_never_raises(self):
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, 100)
        review = self._process("RV-IDEM-1", qty=10.0)
        so = review.sale_order_id
        # Second explicit call: action_assign on already-assigned pickings is
        # a no-op and the helper must not raise.
        unreserved = self.env["edi.processor"].verify_order_reservation(
            so, self.trading_partner)
        self.assertFalse(unreserved)
