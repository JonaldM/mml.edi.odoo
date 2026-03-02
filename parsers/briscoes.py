# mml.edi/parsers/briscoes.py
"""
Briscoes EDI Parser — Phase 1 Stub.

Returns mock ParsedOrder data that exercises all pipeline code paths:
  1. Clean new order (no issues) → auto-approved if partner allows
  2. New order with price discrepancy + unknown product → pending_review
  3. Change order (modifies order #1) → pending_review
  4. Duplicate of order #1 → dedup engine skips it

PHASE 2: Replace parse_file() and generate_ack() with real EDIFACT D96A logic
when sample files are provided by Briscoes IT.
"""

import logging
from datetime import date, timedelta

from .base_parser import BaseEDIParser, ParsedOrder, ParsedOrderLine

_logger = logging.getLogger(__name__)

_MOCK_STORE_A = "1017"
_MOCK_STORE_B = "1042"


class BriscoesParser(BaseEDIParser):
    """
    Parser for Briscoes EDIFACT D96A purchase orders.

    Phase 1: Returns mock data for end-to-end pipeline testing.
    Phase 2: Implement real EDIFACT D96A parsing.
    """

    def parse_file(
        self, raw_content: bytes, trading_partner
    ) -> list[ParsedOrder]:
        """
        # PHASE 2: Replace this stub with real EDIFACT D96A parsing.
        #
        # The real implementation should:
        # 1. Decode raw_content as EDIFACT (ISO 9735)
        # 2. Extract UNH/UNT segment groups
        # 3. Identify message type: ORDERS (new) or ORDCHG (change)
        # 4. Extract BGM, DTM, NAD, LIN, QTY, PRI segments
        # 5. Map store code from NAD+DP GLN to res.partner.ref
        # 6. Return one ParsedOrder per store group
        #
        # Reference: EDIFACT D96A ORDERS standard
        # Sample files: provided by Briscoes IT in Phase 2

        For now, return 4 mock ParsedOrder objects that exercise all code paths.
        """
        _logger.info("[BriscoesParser] Phase 1 stub: returning mock parsed orders")

        today = date.today()
        delivery_date = today + timedelta(days=7)
        changed_delivery_date = today + timedelta(days=14)
        raw_text = raw_content.decode("utf-8", errors="replace")

        # Scenario 1: Clean new order for store 1017
        # Expected outcome: auto-approved if partner.auto_confirm_clean=True
        clean_order = ParsedOrder(
            po_number="4500999001",
            store_code=_MOCK_STORE_A,
            order_date=today,
            requested_delivery_date=delivery_date,
            lines=[
                ParsedOrderLine(
                    product_code="9300601234567",   # Valid EAN-13
                    description="Volere Sparkling Water 12pk",
                    quantity=24.0,
                    unit_price=18.99,
                    line_number=1,
                ),
                ParsedOrderLine(
                    product_code="9300601234568",
                    description="Volere Still Water 12pk",
                    quantity=12.0,
                    unit_price=18.99,
                    line_number=2,
                ),
                ParsedOrderLine(
                    product_code="9300601234569",
                    description="Enkel Sparkling 6pk",
                    quantity=6.0,
                    unit_price=11.99,
                    line_number=3,
                ),
            ],
            document_type="new_order",
            raw_data=raw_text,
        )

        # Scenario 2: New order with issues for store 1042
        # Line 1: price discrepancy (EDI price != pricelist price)
        # Line 2: product code not in Odoo
        # Expected outcome: pending_review, 2 blocking issues
        problem_order = ParsedOrder(
            po_number="4500999002",
            store_code=_MOCK_STORE_B,
            order_date=today,
            requested_delivery_date=delivery_date,
            lines=[
                ParsedOrderLine(
                    product_code="9300601234567",   # Known product, wrong price
                    description="Volere Sparkling Water 12pk",
                    quantity=24.0,
                    unit_price=999.99,               # Deliberately wrong price
                    line_number=1,
                ),
                ParsedOrderLine(
                    product_code="UNKNOWN_SKU_00000",  # Not in Odoo
                    description="Mystery Product",
                    quantity=10.0,
                    unit_price=9.99,
                    line_number=2,
                ),
            ],
            document_type="new_order",
            raw_data=raw_text,
        )

        # Scenario 3: Change order for PO 4500999001 (scenario 1)
        # Changes: qty on line 1 increased, delivery date changed
        # Expected outcome: pending_review, change_summary computed
        change_order = ParsedOrder(
            po_number="4500999001",
            store_code=_MOCK_STORE_A,
            order_date=today,
            requested_delivery_date=changed_delivery_date,
            lines=[
                ParsedOrderLine(
                    product_code="9300601234567",
                    description="Volere Sparkling Water 12pk",
                    quantity=36.0,              # Was 24 — qty increased
                    unit_price=18.99,
                    line_number=1,
                ),
                ParsedOrderLine(
                    product_code="9300601234568",
                    description="Volere Still Water 12pk",
                    quantity=12.0,
                    unit_price=18.99,
                    line_number=2,
                ),
                ParsedOrderLine(
                    product_code="9300601234569",
                    description="Enkel Sparkling 6pk",
                    quantity=6.0,
                    unit_price=11.99,
                    line_number=3,
                ),
            ],
            document_type="change_order",
            change_reason="Customer increased order quantity",
            raw_data=raw_text,
        )

        # Scenario 4: Duplicate of scenario 1 (same PO + store)
        # Expected outcome: dedup engine skips it — no new SO or review
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
                ),
            ],
            document_type="new_order",
            raw_data=raw_text,
        )

        return [clean_order, problem_order, change_order, duplicate_order]

    def generate_ack(self, review_record) -> bytes:
        """
        # PHASE 2: Replace with real EDIFACT ORDRSP/APERAK ACK generation.
        #
        # The real implementation should:
        # 1. Generate EDIFACT ORDRSP (order response) or APERAK (application error)
        # 2. Include all accepted/rejected line details
        # 3. Follow Briscoes-specific segment requirements from their tech spec
        #
        # Sample ACK format: provided by Briscoes IT in Phase 2

        Phase 1: return a placeholder ACK for pipeline testing.
        """
        _logger.info(
            "[BriscoesParser] Phase 1 stub: generating placeholder ACK for %s",
            review_record.customer_po_number,
        )
        return (
            "ACK|%s|%s|PHASE2_PLACEHOLDER" % (
                review_record.customer_po_number,
                review_record.state,
            )
        ).encode("utf-8")
