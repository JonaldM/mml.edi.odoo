# mml.edi/tests/common.py
"""
Shared test fixtures for mml_edi Odoo TransactionCase tests.

Usage:
    class MyTest(TransactionCase, EDITestSetup):
        def setUp(self):
            super().setUp()
            self.setup_edi_test_data()
"""
from datetime import date, timedelta

# Import dataclasses for use in tests — these don't need Odoo
from mml_edi.parsers.base_parser import ParsedOrder, ParsedOrderLine


def make_parsed_line(
    product_code="TEST001",
    description="Test Product",
    quantity=10.0,
    unit_price=9.99,
    line_number=1,
):
    return ParsedOrderLine(
        product_code=product_code,
        description=description,
        quantity=quantity,
        unit_price=unit_price,
        line_number=line_number,
    )


def make_clean_parsed_order(po_number="TESTPO001", store_code=None, qty=10.0):
    """ParsedOrder with one valid line — no issues expected."""
    return ParsedOrder(
        po_number=po_number,
        store_code=store_code,
        order_date=date.today(),
        requested_delivery_date=date.today() + timedelta(days=7),
        lines=[make_parsed_line(quantity=qty, unit_price=9.99)],
        document_type="new_order",
        raw_data="MOCK_EDI_CONTENT",
    )


def make_price_discrepancy_parsed_order(po_number="TESTPO_PRICE"):
    """ParsedOrder with a price that won't match any pricelist (999.99)."""
    return ParsedOrder(
        po_number=po_number,
        order_date=date.today(),
        lines=[make_parsed_line(unit_price=999.99)],
        document_type="new_order",
        raw_data="MOCK_PRICE_DISCREPANCY_EDI",
    )


def make_product_not_found_parsed_order(po_number="TESTPO_NOTFOUND"):
    """ParsedOrder with a product code that doesn't exist in Odoo."""
    return ParsedOrder(
        po_number=po_number,
        order_date=date.today(),
        lines=[make_parsed_line(product_code="NONEXISTENT_SKU_99999")],
        document_type="new_order",
        raw_data="MOCK_NOTFOUND_EDI",
    )


def make_change_order_parsed_order(po_number="TESTPO001", qty=20.0):
    """Change order for an existing PO — qty updated."""
    return ParsedOrder(
        po_number=po_number,
        order_date=date.today(),
        requested_delivery_date=date.today() + timedelta(days=14),
        lines=[make_parsed_line(quantity=qty, unit_price=9.99)],
        document_type="change_order",
        change_reason="Quantity update",
        raw_data="MOCK_CHANGE_ORDER_EDI",
    )


class EDITestSetup:
    """
    Mixin providing setup_edi_test_data() for Odoo TransactionCase tests.

    Creates:
    - A test customer partner
    - A test pricelist with one item for TEST001 product at 9.99
    - A test product with barcode=TEST001 and list_price=9.99
    - A test trading partner linked to the above

    All records are rolled back after each test method (TransactionCase behaviour).
    """

    def setup_edi_test_data(self):
        test_partner = self.env["res.partner"].create({
            "name": "EDI Test Customer",
            "customer_rank": 1,
        })

        pricelist = self.env["product.pricelist"].create({
            "name": "EDI Test Pricelist",
            "currency_id": self.env.company.currency_id.id,
        })

        self.test_product = self.env["product.product"].create({
            "name": "EDI Test Product",
            "barcode": "TEST001",
            "list_price": 9.99,
            "type": "product",
        })

        self.env["product.pricelist.item"].create({
            "pricelist_id": pricelist.id,
            "product_id": self.test_product.id,
            "compute_price": "fixed",
            "fixed_price": 9.99,
        })

        self.trading_partner = self.env["edi.trading.partner"].create({
            "name": "EDI Test Partner",
            "code": "TESTPARTNER",
            "partner_id": test_partner.id,
            "edi_format": "csv",
            "parser_class": "mml_edi.parsers.briscoes.BriscoesParser",
            "ftp_protocol": "ftp",
            "ftp_host": "ftp.test.local",
            "ftp_port": 21,
            "environment": "test",
            "pricelist_id": pricelist.id,
            "price_tolerance_pct": 0.0,
            "auto_confirm_clean": False,
            "order_split_mode": "single",
            "product_match_field": "barcode",
            "client_ref_template": "{po_number}",
        })

        self.processor = self.env["edi.processor"]


def make_fallback_lookup_order(
    primary_ean="NONEXISTENT_EAN_0000",
    vendor_code="MML-INTERNAL-001",
    buyer_article_no="BRISCOES-ART-001",
    po_number="TESTPO_CASCADE",
):
    """
    ParsedOrder where primary product_code (EAN) won't match anything,
    but vendor_code (MML internal ref) WILL match a product with default_code.
    Used for cascade lookup tests.
    """
    return ParsedOrder(
        po_number=po_number,
        order_date=date.today(),
        lines=[
            ParsedOrderLine(
                product_code=primary_ean,
                description="Cascade Test Product",
                quantity=5.0,
                unit_price=9.99,
                line_number=1,
                vendor_code=vendor_code,
                buyer_article_no=buyer_article_no,
            )
        ],
        document_type="new_order",
        raw_data="MOCK_CASCADE_EDI",
    )
