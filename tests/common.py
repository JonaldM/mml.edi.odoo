# mml.edi/tests/common.py
"""
Shared test fixtures for mml_edi Odoo TransactionCase tests.

Usage:
    class MyTest(TransactionCase, EDITestSetup):
        def setUp(self):
            super().setUp()
            self.setup_edi_test_data()
"""
from contextlib import contextmanager
from datetime import date, timedelta

# Import dataclasses for use in tests — these don't need Odoo
try:
    from odoo.addons.mml_edi.parsers.base_parser import ParsedOrder, ParsedOrderLine
except ImportError:
    from mml_edi.parsers.base_parser import ParsedOrder, ParsedOrderLine


def make_idoc_raw(po_number, ordered_each=10.0, posex="00001"):
    """Minimal single-PO Briscoes ORDERS iDOC (one line) for ACK-flow tests.

    POSEX '00001' parses to line 1 — matching make_parsed_line's default
    line_number — so a review processed from make_clean_parsed_order can have
    its edi_raw_data swapped for this and BriscoesIDOCParser.generate_ack will
    map the live SO line's confirmed qty onto the echoed line's WMENG.
    EA order unit throughout, so the ORDRSP's order-unit quantities (MENGE /
    WMENG) read directly in eaches.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ORDERSEXT><IDOC BEGIN="1">'
        '<EDI_DC40 SEGMENT="1"><DOCNUM>0000000000000001</DOCNUM>'
        '<SNDPOR>SAPBRP_TEST</SNDPOR><SNDPRN>BRISCOES</SNDPRN>'
        '<CREDAT>20260701</CREDAT><CRETIM>120000</CRETIM></EDI_DC40>'
        '<E1EDK01 SEGMENT="1"><CURCY>NZD</CURCY><BELNR>%s</BELNR></E1EDK01>'
        '<E1EDP01 SEGMENT="1"><POSEX>%s</POSEX>'
        '<MENGE>%.3f</MENGE><MENEE>EA</MENEE>'
        '<BMNG2>%.3f</BMNG2><PMENE>EA</PMENE>'
        '<VPREI>9.99</VPREI><PEINH>1</PEINH>'
        '</E1EDP01>'
        '</IDOC></ORDERSEXT>'
    ) % (po_number, posex, ordered_each, ordered_each)


def make_island_warehouse(env, name, code):
    """Create a test fulfilment DC, island-tagged when mml_roq_forecast is
    installed.

    ``roq_island`` is owned by mml_roq_forecast, which mml_edi soft-depends on
    (see EDIProcessor._dc_available_qty). On prod-like DBs the tag routes
    availability through the real _fulfilment_free_qty island fence; on a bare
    DB (mml_edi installed alone) the field does not exist and the processor's
    warehouse-scoped free_qty fallback is exercised instead.
    """
    vals = {"name": name, "code": code}
    if "roq_island" in env["stock.warehouse"]._fields:
        vals["roq_island"] = "north"
    return env["stock.warehouse"].create(vals)


class RecordingFTPHandler:
    """Network-free stand-in for EDIFTPHandler (ACK send-path tests).

    Class-level state so tests can inspect uploads and stage the outbound
    listing; call reset() in setUp. ``upload_error`` is raised AFTER the
    upload is recorded — simulating an FTP ghost-success (data stored on the
    server, but the final 226 reply lost).
    """
    uploads = []
    listing = []
    upload_error = None
    listing_error = None

    def __init__(self, partner):
        self.partner = partner

    @contextmanager
    def connection(self):
        yield self

    def upload_file(self, filename, content):
        type(self).uploads.append(filename)
        if type(self).upload_error:
            raise type(self).upload_error

    def list_outbox_files(self):
        if type(self).listing_error:
            raise type(self).listing_error
        return list(type(self).listing)

    @classmethod
    def reset(cls):
        cls.uploads = []
        cls.listing = []
        cls.upload_error = None
        cls.listing_error = None


def make_parsed_line(
    product_code="9780000000002",
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
    - A test pricelist with one item for the test product at 9.99
    - A test product with barcode=9780000000002 (valid EAN-13) and list_price=9.99
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
            # Valid EAN-13 with correct check digit (978-0-000000-0-2).
            # "TEST001" was not a valid EAN-13, causing _validate_ean13_for_ordrsp
            # to raise UserError whenever an ORDRSP was generated for an SO that
            # included this product (e.g. auto-approved new-order reviews and
            # TestBriscoesOrdrspIntegration happy-path tests).
            "barcode": "9780000000002",
            "list_price": 9.99,
            "type": "consu",
            # EDI pricelists must resolve ex-GST (edi.trading.partner._validate_pricelist_gst).
            # Clear sale taxes so the company's default GST-inclusive tax isn't applied —
            # otherwise assigning this product's pricelist to the test trading partner trips
            # that constraint and every TransactionCase using this fixture errors in setUp.
            "taxes_id": [(6, 0, [])],
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
