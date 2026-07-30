# mml.edi/tests/test_sscc_register_odoo.py
"""Odoo TransactionCase tests for sscc.register (AN-03/OPS-6, Wave2-D).

Covers what pure pytest (tests/test_gs1_sscc.py) cannot: real ORM
persistence, the mml_edi.sscc.serial sequence actually incrementing per
mint, the UNIQUE(picking_id, unit_key) idempotency guarantee under repeated
calls (re-print/re-generate never re-mints), and get_or_create() wired
against a real stock.picking produced by the order-confirm flow.

Requires a live Odoo DB (odoo-bin --test-enable)."""
import hashlib
import unittest

from odoo.tests.common import TransactionCase, tagged

from .common import EDITestSetup, make_clean_parsed_order, make_island_warehouse

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")
class TestSSCCRegister(EDITestSetup, TransactionCase):

    # TEST_GS1_PREFIX comes from EDITestSetup and is applied by
    # setup_edi_test_data(): sscc.register has no built-in prefix any more, it
    # is account-specific configuration (ir.config_parameter
    # mml_edi.gs1_company_prefix).

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.test_product.is_storable = True
        self.wh = make_island_warehouse(self.env, "SSCC DC", "SSDC")
        self.trading_partner.write({
            "oos_policy": "backorder",
            "warehouse_id": self.wh.id,
            "auto_confirm_clean": True,
        })
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, 1000)

    def _make_picking(self, po_number="SSCC-PO-1"):
        order = make_clean_parsed_order(po_number=po_number, qty=10.0)
        order.raw_data = "SSCC_RAW_%s" % po_number
        file_hash = hashlib.sha256(order.raw_data.encode()).hexdigest()
        self.processor.process_parsed_order(
            order, self.trading_partner, "sscc.edi", file_hash)
        review = self.env["edi.order.review"].search(
            [("customer_po_number", "=", po_number)], limit=1)
        so = review.sale_order_id
        outgoing = so.picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing")
        self.assertTrue(outgoing, "Order confirm must produce an outgoing picking")
        return outgoing[0]

    # --- minting -----------------------------------------------------------

    def test_get_or_create_mints_an_18_digit_sscc(self):
        picking = self._make_picking("SSCC-MINT-1")
        Register = self.env["sscc.register"]
        row = Register.get_or_create(picking, "carton-1")
        self.assertEqual(len(row.sscc), 18)
        self.assertTrue(row.sscc.isdigit())

    def test_get_or_create_uses_configured_gs1_prefix(self):
        picking = self._make_picking("SSCC-PREFIX-1")
        row = self.env["sscc.register"].get_or_create(picking, "carton-1")
        self.assertTrue(row.sscc.startswith("0" + self.TEST_GS1_PREFIX))
        self.assertEqual(row.gs1_prefix, self.TEST_GS1_PREFIX)

    def test_reconfigured_prefix_reaches_the_minted_sscc(self):
        """The configured value — not a code constant — is what ends up on the
        wire/label. Re-pointing the parameter re-points minting."""
        other_prefix = "0200008"
        self.env["ir.config_parameter"].sudo().set_param(
            "mml_edi.gs1_company_prefix", other_prefix)
        picking = self._make_picking("SSCC-PREFIX-2")
        row = self.env["sscc.register"].get_or_create(picking, "carton-1")
        self.assertEqual(row.gs1_prefix, other_prefix)
        self.assertTrue(row.sscc.startswith("0" + other_prefix))

    def test_unconfigured_prefix_fails_closed(self):
        """Fresh-install neutrality: with no configured prefix there is no code
        default to fall back on, so minting refuses rather than claiming another
        company's GS1 prefix."""
        from odoo.exceptions import UserError
        picking = self._make_picking("SSCC-PREFIX-3")
        self.env["ir.config_parameter"].sudo().set_param(
            "mml_edi.gs1_company_prefix", "")
        with self.assertRaises(UserError):
            self.env["sscc.register"].get_or_create(picking, "carton-1")

    def test_get_or_create_persists_picking_and_unit_key(self):
        picking = self._make_picking("SSCC-PERSIST-1")
        row = self.env["sscc.register"].get_or_create(
            picking, "pallet-1", unit_type="pallet")
        self.assertEqual(row.picking_id, picking)
        self.assertEqual(row.unit_key, "pallet-1")
        self.assertEqual(row.unit_type, "pallet")

    def test_get_or_create_requires_unit_key(self):
        from odoo.exceptions import UserError
        picking = self._make_picking("SSCC-NOKEY-1")
        with self.assertRaises(UserError):
            self.env["sscc.register"].get_or_create(picking, "")

    # --- idempotency (the whole point of this model) ------------------------

    def test_reprint_never_re_mints_same_unit(self):
        picking = self._make_picking("SSCC-IDEM-1")
        Register = self.env["sscc.register"]
        first = Register.get_or_create(picking, "carton-1")
        second = Register.get_or_create(picking, "carton-1")
        third = Register.get_or_create(picking, "carton-1")
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.id, third.id)
        self.assertEqual(first.sscc, second.sscc)

    def test_reprint_ten_times_creates_exactly_one_row(self):
        picking = self._make_picking("SSCC-IDEM-10X")
        Register = self.env["sscc.register"]
        for _ in range(10):
            Register.get_or_create(picking, "carton-A")
        count = Register.search_count([
            ("picking_id", "=", picking.id),
            ("unit_key", "=", "carton-A"),
        ])
        self.assertEqual(count, 1)

    def test_different_unit_keys_on_same_picking_mint_different_sscc(self):
        picking = self._make_picking("SSCC-MULTI-1")
        Register = self.env["sscc.register"]
        a = Register.get_or_create(picking, "carton-1")
        b = Register.get_or_create(picking, "carton-2")
        self.assertNotEqual(a.sscc, b.sscc)
        self.assertNotEqual(a.serial, b.serial)

    def test_same_unit_key_on_different_pickings_mint_different_sscc(self):
        picking_a = self._make_picking("SSCC-DIFFPICK-A")
        picking_b = self._make_picking("SSCC-DIFFPICK-B")
        Register = self.env["sscc.register"]
        a = Register.get_or_create(picking_a, "carton-1")
        b = Register.get_or_create(picking_b, "carton-1")
        self.assertNotEqual(a.sscc, b.sscc)

    def test_sscc_uniqueness_across_many_mints(self):
        """Simulates a 12-month uniqueness window in miniature: 50 mints
        across several pickings must never collide."""
        Register = self.env["sscc.register"]
        seen = set()
        for i in range(5):
            picking = self._make_picking("SSCC-BULK-%d" % i)
            for j in range(10):
                row = Register.get_or_create(picking, "unit-%d" % j)
                self.assertNotIn(row.sscc, seen)
                seen.add(row.sscc)
        self.assertEqual(len(seen), 50)

    def test_serial_sequence_advances_per_mint(self):
        picking = self._make_picking("SSCC-SEQ-1")
        Register = self.env["sscc.register"]
        a = Register.get_or_create(picking, "carton-1")
        b = Register.get_or_create(picking, "carton-2")
        self.assertGreater(b.serial, a.serial)

    def test_serial_sequence_not_advanced_by_idempotent_reuse(self):
        """Re-fetching the SAME unit_key must not burn a new serial."""
        picking = self._make_picking("SSCC-SEQ-REUSE-1")
        Register = self.env["sscc.register"]
        first = Register.get_or_create(picking, "carton-1")
        Register.get_or_create(picking, "carton-1")
        Register.get_or_create(picking, "carton-1")
        second_picking = self._make_picking("SSCC-SEQ-REUSE-2")
        next_mint = Register.get_or_create(second_picking, "carton-1")
        # Only ONE new serial consumed between first and next_mint (the
        # idempotent re-fetches above must not have advanced the sequence).
        self.assertEqual(next_mint.serial, first.serial + 1)

    def test_name_get_shows_sscc(self):
        picking = self._make_picking("SSCC-NAMEGET-1")
        row = self.env["sscc.register"].get_or_create(picking, "carton-1")
        self.assertEqual(row.display_name, row.sscc)

    # --- label data (report/sscc_label_report.xml consumer) ----------------

    def test_get_label_data_returns_expected_keys(self):
        picking = self._make_picking("SSCC-LABEL-1")
        row = self.env["sscc.register"].get_or_create(picking, "carton-1")
        data = row.get_label_data()
        for key in (
            "sscc", "unit_type", "ship_from_name", "ship_from_address",
            "ship_to_code", "ship_to_name", "ship_to_address",
            "ship_to_postal_code", "carrier_name", "connote", "po_number",
            "isc", "carton_qty", "vendor_part_no", "description",
        ):
            self.assertIn(key, data)
        self.assertEqual(data["sscc"], row.sscc)

    def test_get_label_data_does_not_raise_with_no_matching_move(self):
        """A defensive-only path: unit_key that doesn't match any move on the
        picking must degrade gracefully (blank product fields), not crash."""
        picking = self._make_picking("SSCC-LABEL-ORPHAN-1")
        row = self.env["sscc.register"].get_or_create(picking, "carton-does-not-exist")
        data = row.get_label_data()
        self.assertEqual(data["vendor_part_no"], "")
        self.assertEqual(data["description"], "")

    def test_report_action_renders_without_error(self):
        """End-to-end smoke test: the registered ir.actions.report can
        actually render the QWeb template to HTML for a real sscc.register
        row (does not require wkhtmltopdf — render_qweb_html is pure QWeb)."""
        picking = self._make_picking("SSCC-LABEL-RENDER-1")
        row = self.env["sscc.register"].get_or_create(picking, "carton-1")
        html, _report_type = self.env["ir.actions.report"]._render_qweb_html(
            "mml_edi.report_sscc_label", row.ids
        )
        self.assertIn(row.sscc.encode(), html)
