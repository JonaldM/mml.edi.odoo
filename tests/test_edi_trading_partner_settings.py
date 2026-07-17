# mml_edi/tests/test_edi_trading_partner_settings.py
"""Integration tests for the trading-partner Settings-form backing fields.

Covers the EDI-Identity config fields added for the design-handoff Settings
screen (sender id/qualifier, supplier GLN, vendor code) and the read-only
store-map relation surfaced on the Stores tab.

Tagged post_install (the shared EDITestSetup fixture creates products, which at
at_install time hits registry-order NOT NULLs).
"""
from odoo.tests import TransactionCase, tagged

from .common import EDITestSetup


@tagged("post_install", "-at_install")
class TestEdiTradingPartnerSettings(TransactionCase, EDITestSetup):
    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()

    def test_sender_qualifier_defaults_to_gln(self):
        # Fresh partner (fixture didn't set it) -> qualifier default "14" (GLN).
        p = self.env["edi.trading.partner"].create({
            "name": "Identity Partner",
            "code": "IDENTITY",
            "partner_id": self.trading_partner.partner_id.id,
            "edi_format": "edifact_d96a",
            "parser_class": "mml_edi.parsers.briscoes.BriscoesParser",
            "ftp_protocol": "ftp",
            "environment": "test",
        })
        self.assertEqual(p.edi_sender_qualifier, "14")

    def test_identity_fields_writable(self):
        self.trading_partner.write({
            "edi_sender_id": "9469313000007",
            "supplier_gln": "9469313000007",
            "vendor_code": "300024",
        })
        self.assertEqual(self.trading_partner.edi_sender_id, "9469313000007")
        self.assertEqual(self.trading_partner.supplier_gln, "9469313000007")
        self.assertEqual(self.trading_partner.vendor_code, "300024")

    def test_store_partner_ids_relates_to_customer_children(self):
        customer = self.trading_partner.partner_id
        store = self.env["res.partner"].create({
            "name": "Test Store 1042",
            "ref": "1042",
            "parent_id": customer.id,
            "type": "delivery",
        })
        self.trading_partner.invalidate_recordset(["store_partner_ids"])
        self.assertIn(store, self.trading_partner.store_partner_ids)
