# mml.edi/parsers/briscoes_idoc.py
"""
Briscoes iDOC XML Parser — Phase 1 Stub.

Returns mock ParsedOrder data that exercises all pipeline code paths.
Phase 2: Replace parse_file() and generate_ack() with real SAP ORDERSEXT XML
parsing when sample files are provided by Briscoes IT.

iDOC XML Structure (ORDERSEXT v1.6, MANDT=300):
  EDI_DC40          — interchange header (IDOCTYP=ORDERS05, CIMTYP=ORDERSEXT)
  E1EDK01           — PO header (BELNR=PO#, BSART=order type, KZABS=ack required flag)
  E1EDK03           — dates (012=PO date, 011=delivery date)
  E1EDKA1           — header partners (AG=buyer, WE=single ship-to, LF=vendor)
  E1EDP01           — line items (POSEX, ACTION 001/002/003, MENGE, MENEE, BMNG2, VPREI, NETWR)
  ZE1EDP01          — extended line data (ATTYP: skip if 01=generic article placeholder)
  E1EDPA1           — per-line ship-to for multi-store orders (PARVW=WE, LIFNR=store code)
  E1EDP19           — product IDs:
                        001 = Briscoes buyer article no  → buyer_article_no
                        002 = vendor/MML article no      → vendor_code
                        003 = GTIN/EAN-13                → product_code (primary)
  E1EDP20           — delivery schedule (WMENG, AMENG, EDATU)
  E1EDS01           — summary/control
"""

import logging
from datetime import date, timedelta

from .base_parser import BaseEDIParser, ParsedOrder, ParsedOrderLine

_logger = logging.getLogger(__name__)

_MOCK_STORE_A = "1017"
_MOCK_STORE_B = "1042"


class BriscoesIDOCParser(BaseEDIParser):
    """
    Parser for Briscoes SAP iDOC ORDERSEXT purchase orders.

    Phase 1: Returns mock data for end-to-end pipeline testing.
    Phase 2: Implement real ORDERSEXT XML parsing.
    """

    def parse_file(
        self, raw_content: bytes, trading_partner
    ) -> list[ParsedOrder]:
        """
        # PHASE 2: Replace this stub with real iDOC XML parsing.
        #
        # The real implementation should:
        # 1. Parse raw_content as XML (xml.etree.ElementTree or lxml)
        # 2. Validate EDI_DC40 header: MANDT=300, IDOCTYP=ORDERS05, CIMTYP=ORDERSEXT
        # 3. Identify order type from E1EDK01 BSART:
        #      ZNS/ZWB = new order; ZNC/ZNB/ZNR/ZNP = other new types; ZCH = change
        # 4. Extract dates from E1EDK03 (QUALF 012=PO date, 011=delivery date)
        # 5. Extract partners from E1EDKA1 (PARVW: AG=buyer, WE=ship-to, LF=vendor)
        # 6. For multi-store: E1EDPA1 at line level (PARVW=WE, LIFNR=store code)
        # 7. For each E1EDP01 line:
        #    a. Skip if ZE1EDP01 ATTYP == '01' (generic article — not a real product)
        #    b. Extract POSEX (line number), ACTION (001=add, 002=change, 003=delete)
        #    c. Extract MENGE (order qty), BMNG2 (carton qty), VPREI (unit price)
        #    d. Extract product codes from E1EDP19:
        #         001 → buyer_article_no (Briscoes' code)
        #         002 → vendor_code (MML internal reference = product.default_code)
        #         003 → product_code (GTIN/EAN-13, use as primary)
        #    e. Extract delivery date from E1EDP20 EDATU
        # 8. Group by ship-to store code → one ParsedOrder per store
        # 9. If E1EDK01 KZABS == 'X' → ACK is required (always True for Briscoes)
        #
        # Reference: Briscoes iDOC ORDERSEXT Implementation Guide v1.6 (Oct 2025)
        # Sample files: provided by Briscoes IT in Phase 2

        Phase 1: Return 4 mock ParsedOrder objects for pipeline testing.
        """
        _logger.info(
            "[BriscoesIDOCParser] Phase 1 stub: returning mock parsed orders (iDOC format)"
        )

        today = date.today()
        delivery_date = today + timedelta(days=7)
        changed_delivery_date = today + timedelta(days=14)
        raw_text = raw_content.decode("utf-8", errors="replace")

        # Scenario 1: Clean new order for store 1017
        clean_order = ParsedOrder(
            po_number="4500999001",
            store_code=_MOCK_STORE_A,
            order_date=today,
            requested_delivery_date=delivery_date,
            lines=[
                ParsedOrderLine(
                    product_code="9300601234567",
                    description="Volere Sparkling Water 12pk",
                    quantity=24.0,
                    unit_price=18.99,
                    line_number=1,
                    buyer_article_no="BRS-001234",
                    vendor_code="VOL-SPARK-12",
                ),
                ParsedOrderLine(
                    product_code="9300601234568",
                    description="Volere Still Water 12pk",
                    quantity=12.0,
                    unit_price=18.99,
                    line_number=2,
                    buyer_article_no="BRS-001235",
                    vendor_code="VOL-STILL-12",
                ),
                ParsedOrderLine(
                    product_code="9300601234569",
                    description="Enkel Sparkling 6pk",
                    quantity=6.0,
                    unit_price=11.99,
                    line_number=3,
                    buyer_article_no="BRS-002100",
                    vendor_code="ENK-SPARK-6",
                ),
            ],
            document_type="new_order",
            raw_data=raw_text,
        )

        # Scenario 2: New order with issues for store 1042
        problem_order = ParsedOrder(
            po_number="4500999002",
            store_code=_MOCK_STORE_B,
            order_date=today,
            requested_delivery_date=delivery_date,
            lines=[
                ParsedOrderLine(
                    product_code="9300601234567",
                    description="Volere Sparkling Water 12pk",
                    quantity=24.0,
                    unit_price=999.99,
                    line_number=1,
                    vendor_code="VOL-SPARK-12",
                ),
                ParsedOrderLine(
                    product_code="UNKNOWN_SKU_00000",
                    description="Mystery Product",
                    quantity=10.0,
                    unit_price=9.99,
                    line_number=2,
                ),
            ],
            document_type="new_order",
            raw_data=raw_text,
        )

        # Scenario 3: Change order for PO 4500999001
        change_order = ParsedOrder(
            po_number="4500999001",
            store_code=_MOCK_STORE_A,
            order_date=today,
            requested_delivery_date=changed_delivery_date,
            lines=[
                ParsedOrderLine(
                    product_code="9300601234567",
                    description="Volere Sparkling Water 12pk",
                    quantity=36.0,
                    unit_price=18.99,
                    line_number=1,
                    vendor_code="VOL-SPARK-12",
                ),
                ParsedOrderLine(
                    product_code="9300601234568",
                    description="Volere Still Water 12pk",
                    quantity=12.0,
                    unit_price=18.99,
                    line_number=2,
                    vendor_code="VOL-STILL-12",
                ),
                ParsedOrderLine(
                    product_code="9300601234569",
                    description="Enkel Sparkling 6pk",
                    quantity=6.0,
                    unit_price=11.99,
                    line_number=3,
                    vendor_code="ENK-SPARK-6",
                ),
            ],
            document_type="change_order",
            change_reason="Customer increased order quantity",
            raw_data=raw_text,
        )

        # Scenario 4: Duplicate of scenario 1
        duplicate_order = ParsedOrder(
            po_number="4500999001",
            store_code=_MOCK_STORE_A,
            order_date=today,
            requested_delivery_date=delivery_date,
            lines=[
                ParsedOrderLine(
                    product_code="9300601234567",
                    description="Volere Sparkling Water 12pk",
                    quantity=24.0,
                    unit_price=18.99,
                    line_number=1,
                    vendor_code="VOL-SPARK-12",
                ),
            ],
            document_type="new_order",
            raw_data=raw_text,
        )

        return [clean_order, problem_order, change_order, duplicate_order]

    def generate_ack(self, review_record) -> bytes:
        """
        # PHASE 2: Replace with real iDOC ORDRSP XML generation.
        #
        # The real implementation should:
        # 1. Generate an ORDRSP iDOC XML document (ORDERS05/ORDERSEXT response)
        # 2. Set E1EDK01 BSART to appropriate response code
        # 3. Include accepted/rejected line details with status codes
        # 4. Follow Briscoes-specific iDOC PO Response Guide v1.7
        #
        # Reference: Briscoes iDOC PO Response Implementation Guide v1.7

        Phase 1: return a placeholder XML ACK for pipeline testing.
        """
        _logger.info(
            "[BriscoesIDOCParser] Phase 1 stub: generating placeholder iDOC ACK for %s",
            review_record.customer_po_number,
        )
        return (
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<ORDERS05><EDI_DC40><IDOCTYP>ORDERS05</IDOCTYP>"
            "<CIMTYP>ORDERSEXT</CIMTYP></EDI_DC40>"
            "<!-- PHASE2_PLACEHOLDER PO=%s STATE=%s -->"
            "</ORDERS05>" % (
                review_record.customer_po_number,
                review_record.state,
            )
        ).encode("utf-8")
