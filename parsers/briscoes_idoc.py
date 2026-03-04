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

from .base_parser import BaseEDIParser

_logger = logging.getLogger(__name__)


class BriscoesIDOCParser(BaseEDIParser):
    """
    Parser for Briscoes SAP iDOC ORDERSEXT purchase orders.

    Phase 1: Returns mock data for end-to-end pipeline testing.
    Phase 2: Implement real ORDERSEXT XML parsing.
    """

    def parse_file(self, raw_content: bytes, trading_partner) -> list:
        raise NotImplementedError(
            'BriscoesIDOCParser is a development stub and must not be used in production. '
            'Implement against the confirmed Briscoes iDOC specification before activating. '
            'Do not configure any trading partner to use this parser class.'
        )

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
