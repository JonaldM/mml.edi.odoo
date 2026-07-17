# mml.edi/parsers/base_parser.py
"""
Base EDI parser contracts.

All parser implementations must subclass BaseEDIParser.
ParsedOrder and ParsedOrderLine are the intermediate representation
passed between parsers and the processing engine.

These interfaces are stable — do not change field names or method
signatures without updating all parsers and the processor.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # Odoo model types referenced by string only to avoid circular imports


@dataclass
class ParsedOrderLine:
    """Represents a single line item from an EDI order document."""

    product_code: str          # Primary code (EAN-13 by convention) — matched against product_match_field
    description: str
    quantity: float
    unit_price: float          # Price from EDI — what the customer expects to pay
    line_number: int           # EDI line number for ACK reference
    uom: str | None = None     # Unit of measure from EDI (may differ from Odoo UOM)
    carton_qty: float | None = None       # QTY+52 (EDIFACT) / BMNG2 (iDOC) — qty per carton/inner pack
    buyer_article_no: str | None = None   # PIA+IN (EDIFACT) / E1EDP19 001 (iDOC) — buyer's own item code
    vendor_code: str | None = None        # PIA+SA (EDIFACT) / E1EDP19 002 (iDOC) — MML internal reference


@dataclass
class ParsedOrder:
    """
    Standardised intermediate representation.

    Parser output → Processing engine input.
    One ParsedOrder per store/SO that will be created.
    """

    po_number: str
    order_date: date
    lines: list[ParsedOrderLine]

    # None for single-order customers (order_split_mode == 'single')
    store_code: str | None = None

    requested_delivery_date: date | None = None

    # Delivery address GLN/code — looked up against res.partner.ref
    delivery_address_code: str | None = None

    # 'new_order', 'change_order', or 'cancellation'. Parsers set this from the
    # EDI message type. If the format doesn't distinguish, detect by matching PO
    # number to existing SO. 'cancellation' (e.g. Animates BGM 1225=1 / contract
    # C5) carries no order lines — the processor cancels the existing SO and
    # never queues an ORDRSP for it (see models/edi_processor.py
    # _process_cancellation / CANCELLATION_MARKER).
    document_type: str = "new_order"

    # Optional reason code / description from the EDI change order message
    change_reason: str | None = None

    # Raw EDI content stored for audit trail and debugging (set by processor)
    raw_data: str | None = None

    def content_hash(self) -> str:
        """SHA-256 of raw_data for deduplication. Must be set before calling."""
        if not self.raw_data:
            raise ValueError("raw_data must be set before computing content_hash")
        return hashlib.sha256(self.raw_data.encode()).hexdigest()


class BaseEDIParser(ABC):
    """
    Abstract base class for EDI parsers.

    One subclass per trading partner (or per EDI format if multiple
    partners share the same format).

    The parser is stateless — all configuration comes via the
    trading_partner argument.
    """

    @abstractmethod
    def parse_file(
        self, raw_content: bytes, trading_partner
    ) -> list[ParsedOrder]:
        """
        Parse raw file bytes into a list of ParsedOrder objects.

        One file may contain multiple orders (e.g., one per store).
        Returns an empty list if the file contains no processable orders.

        Args:
            raw_content: Raw bytes downloaded from FTP
            trading_partner: edi.trading.partner record (Odoo model instance)

        Raises:
            EDIParseError: If the file is structurally invalid and cannot
                           be partially parsed. For line-level errors, create
                           a ParsedOrderLine with quantity=0 and flag in issues.
        """
        raise NotImplementedError

    @abstractmethod
    def generate_ack(self, review_record) -> bytes:
        """
        Generate acknowledgement file bytes for a processed order.

        Args:
            review_record: edi.order.review record

        Returns:
            Raw bytes to upload to the FTP outbox
        """
        raise NotImplementedError

    def build_outbound(self, msg_type: str, payload) -> bytes:
        """Build an outbound document OTHER than the ORDRSP ack (e.g. DESADV/INVOIC/
        CONTRL). Partner-dispatched outbound seam.

        Not abstract: parsers that only emit an ORDRSP ack (via ``generate_ack``)
        need not override it. ``payload`` is a partner-specific dict assembled by the
        caller from the relevant Odoo record (stock.picking, account.move, the inbound
        interchange, ...). Returns the serialized message bytes for the FTP outbox.
        """
        raise NotImplementedError(
            "%s does not emit outbound %s" % (type(self).__name__, msg_type)
        )


class EDIParseError(Exception):
    """Raised when an EDI file is structurally invalid."""
    pass


class EDIFTPError(Exception):
    """Raised on FTP connection or transfer failures."""
    pass
