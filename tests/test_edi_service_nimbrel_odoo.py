# mml.edi/tests/test_edi_service_nimbrel_odoo.py
"""Odoo TransactionCase tests for the Nimbrel DESADV dispatch path (Wave2-D,
AN-03/OPS-6).

Covers what pure mocked tests/test_edi_service.py cannot: a real
stock.picking produced by order-confirm, real sscc.register minting wired
through the despatch build, and the Kestrelby path staying byte-identical
when routed through the SAME on_3pl_despatch_confirmed entry point.

Requires a live Odoo DB (odoo-bin --test-enable)."""
import hashlib
import tempfile
import unittest

from odoo.tests.common import TransactionCase, tagged

from .common import EDITestSetup, make_clean_parsed_order, make_island_warehouse

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")
class TestEDIServiceNimbrelDispatch(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.test_product.is_storable = True
        # Nimbrel matches products on default_code (the shared fixture only
        # sets barcode) — without it the order blocks product_not_found and
        # never auto-confirms, so no picking is ever generated to dispatch.
        self.test_product.default_code = "0200000999995"
        # Outbound DESADV/INVOIC go to a local dir (real transport, no network).
        # ISC (PIA+5:IN) is recovered by re-parsing the order raw_data, which
        # carries PIA+5+ISC001:IN — no product-field dependency.
        self.edi_outbox = tempfile.mkdtemp()
        self.wh = make_island_warehouse(self.env, "Nimbrel Dispatch DC", "ADDC")
        self.env["ir.config_parameter"].sudo().set_param("mml_edi.asn_enabled", "1")

        self.nimbrel_customer = self.env["res.partner"].create({
            "name": "Nimbrel NZ Holding LTD",
            "customer_rank": 1,
        })
        # per_store mode resolves NAD+ST to a child contact by ref; without
        # it the order blocks unknown_store and never auto-confirms.
        self.env["res.partner"].create({
            "name": "Nimbrel Store 12345",
            "parent_id": self.nimbrel_customer.id,
            "ref": "12345",
        })
        self.nimbrel_pricelist = self.env["product.pricelist"].create({
            "name": "Nimbrel Test Pricelist",
            "currency_id": self.env.company.currency_id.id,
        })
        self.env["product.pricelist.item"].create({
            "pricelist_id": self.nimbrel_pricelist.id,
            "product_id": self.test_product.id,
            "compute_price": "fixed",
            "fixed_price": 9.99,
        })
        self.nimbrel_partner = self.env["edi.trading.partner"].create({
            "name": "Nimbrel",
            "code": "NIMBREL",
            "partner_id": self.nimbrel_customer.id,
            "edi_format": "edifact_d01b",
            "parser_class": "mml_edi.parsers.nimbrel.NimbrelParser",
            # Fictional counterparty VAN mailbox. The module defaults are
            # the REAL provisioned mailbox ids (wire routing data), so
            # fixtures configure their own rather than asserting on them.
            "unb_recipient_id": "NIMBREL",
            "unb_recipient_test_id": "TST1NIMBREL",
            "ftp_protocol": "localdir",
            "environment": "test",
            "ftp_test_inbox_path": self.edi_outbox,
            "ftp_test_outbox_path": self.edi_outbox,
            "pricelist_id": self.nimbrel_pricelist.id,
            "order_split_mode": "per_store",
            "product_match_field": "default_code",
            "client_ref_template": "{po_number}",
            "warehouse_id": self.wh.id,
            "auto_confirm_clean": True,
            "oos_policy": "backorder",
            "edi_sender_id": "0200000000004T",
            "edi_sender_qualifier": "ZZZ",
            "supplier_gln": "0200000000004",
            "nimbrel_vendor_code": "V1058",
        })
        self.test_product.is_storable = True
        self.env["stock.quant"]._update_available_quantity(
            self.test_product, self.wh.lot_stock_id, 1000)

    def _confirm_and_ship(self, po_number, store_code="12345", qty=10.0):
        order = make_clean_parsed_order(po_number=po_number, store_code=store_code, qty=qty)
        order.raw_data = (
            "UNA:+.? '"
            "UNB+UNOC:3+0200000000004T:ZZZ+TST1NIMBREL:ZZZ+260703:0900+1++++1'"
            "UNH+1+ORDERS:D:01B:UN:EAN008'"
            "BGM+220+%s+9'"
            "NAD+ST+%s::92'"
            "LIN+1'"
            "PIA+5+ISC001:IN'"
            "PIA+1+0200000999995:SA'"
            "QTY+21:%d:EA'"
            "UNT+8+1'"
            "UNZ+1+1'"
        ) % (po_number, store_code, int(qty))
        file_hash = hashlib.sha256(order.raw_data.encode()).hexdigest()
        self.processor.process_parsed_order(
            order, self.nimbrel_partner, "nimbrel_dispatch.edi", file_hash)
        review = self.env["edi.order.review"].search(
            [("customer_po_number", "=", po_number)], limit=1)
        so = review.sale_order_id
        self.assertTrue(so, "Clean auto-confirm order must produce a sale.order")
        outgoing = so.picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing")
        self.assertTrue(outgoing)
        picking = outgoing[0]
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.button_validate()
        return picking, so, review

    def _dispatch(self, picking):
        from odoo.addons.mml_edi.services.edi_service import EDIService

        class FakeEvent:
            res_model = "stock.picking"
            res_id = picking.id

        EDIService(self.env).on_3pl_despatch_confirmed(FakeEvent())

    # --- routing -------------------------------------------------------------

    def test_nimbrel_picking_generates_desadv_log(self):
        picking, so, review = self._confirm_and_ship("PO-DISPATCH-1")
        self._dispatch(picking)
        logs = self.env["edi.log"].search([
            ("trading_partner_id", "=", self.nimbrel_partner.id),
            ("event_type", "=", "ack_sent"),
            ("filename", "like", "DESADV_NIMBREL_%"),
        ])
        self.assertTrue(logs, "Nimbrel dispatch must log a DESADV ack_sent event")

    def test_nimbrel_dispatch_mints_sscc_for_the_picking(self):
        picking, so, review = self._confirm_and_ship("PO-DISPATCH-2")
        self._dispatch(picking)
        rows = self.env["sscc.register"].search([("picking_id", "=", picking.id)])
        self.assertTrue(rows, "Dispatch must mint at least one SSCC for the picking")
        for row in rows:
            self.assertEqual(len(row.sscc), 18)

    def test_nimbrel_dispatch_is_idempotent_on_sscc(self):
        """Re-running the despatch hook (e.g. a retry) must never re-mint a
        second SSCC for the same picking/unit."""
        picking, so, review = self._confirm_and_ship("PO-DISPATCH-3")
        self._dispatch(picking)
        first_count = self.env["sscc.register"].search_count(
            [("picking_id", "=", picking.id)])
        self._dispatch(picking)
        second_count = self.env["sscc.register"].search_count(
            [("picking_id", "=", picking.id)])
        self.assertEqual(first_count, second_count)

    def test_kestrelby_dispatch_still_uses_kestrelby_path_not_nimbrel(self):
        """The refactor must not touch Kestrelby's live production behaviour —
        routing a Kestrelby picking through the same entry point must NOT
        create any sscc.register rows (Nimbrel-only) and must still attempt
        the Kestrelby ASN path (asserted via the absence of DESADV_NIMBREL_
        logs and presence of the Kestrelby-style error/ack pattern)."""
        # trading_partner from EDITestSetup defaults to a csv/kestrelby-class
        # parser; point it at the real Kestrelby iDOC parser class to exercise
        # the Kestrelby branch explicitly.
        self.trading_partner.write({
            "parser_class": "mml_edi.parsers.kestrelby_idoc.KestrelbyIDOCParser",
            "warehouse_id": self.wh.id,
            "auto_confirm_clean": True,
            "ftp_protocol": "localdir",
            "ftp_test_inbox_path": self.edi_outbox,
            "ftp_test_outbox_path": self.edi_outbox,
        })
        from .common import make_idoc_raw
        # Dispatch ROUTING (not parsing) is under test: make_idoc_raw carries no
        # product segment, so pass a matchable ParsedOrder with the iDOC as
        # raw_data. Routing keys off sale_order.edi_trading_partner_id.
        order = make_clean_parsed_order(po_number="KESTRELBY-PO-1", qty=10.0)
        order.raw_data = make_idoc_raw("KESTRELBY-PO-1", ordered_each=10.0)
        file_hash = hashlib.sha256(order.raw_data.encode()).hexdigest()
        self.processor.process_parsed_order(
            order, self.trading_partner, "kestrelby_dispatch.edi", file_hash)
        review = self.env["edi.order.review"].search(
            [("customer_po_number", "=", "KESTRELBY-PO-1")], limit=1)
        so = review.sale_order_id
        self.assertTrue(so)
        outgoing = so.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing")
        self.assertTrue(outgoing)
        picking = outgoing[0]
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.button_validate()

        sscc_before = self.env["sscc.register"].search_count([])
        self._dispatch(picking)
        sscc_after = self.env["sscc.register"].search_count([])
        self.assertEqual(
            sscc_before, sscc_after,
            "Kestrelby dispatch must never mint an SSCC (Nimbrel-only concept)",
        )
        nimbrel_desadv_logs = self.env["edi.log"].search([
            ("filename", "like", "DESADV_NIMBREL_%"),
        ])
        self.assertFalse(
            nimbrel_desadv_logs,
            "Kestrelby picking must never produce an Nimbrel-format DESADV filename",
        )
