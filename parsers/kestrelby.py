# mml.edi/parsers/kestrelby.py
"""
Kestrelby EDI Parser — EDIFACT D96A.

Parses ORDERS (BGM+220) and ORDCHG (BGM+230) messages from the EDIS VAN FTP.
Generates ORDRSP (BGM+231) order responses.

Reference: Kestrelby EDIFACT Purchase Order Implementation Guide v1.13
Sample files: docs/kestrelby.docs/
"""
from __future__ import annotations

import logging
import secrets
from datetime import date, datetime
from typing import Dict, List, Optional

from .base_parser import BaseEDIParser, EDIParseError, ParsedOrder, ParsedOrderLine

_logger = logging.getLogger(__name__)

# EDIFACT message type codes
_BGM_NEW_ORDER = "220"
_BGM_CHANGE_ORDER = "230"
_BGM_ORDER_RESPONSE = "231"

# ORDCHG line action codes
_LIN_ACTION_ADDED = "1"
_LIN_ACTION_CHANGED = "2"
_LIN_ACTION_CANCELLED = "3"
_LIN_ACTION_NO_CHANGE = "5"

# ORDRSP purpose codes
_ORDRSP_ACCEPTED = "29"
_ORDRSP_CHANGED = "4"
_ORDRSP_CANCELLED = "27"

# ORDRSP line action codes
_ORDRSP_LINE_ACCEPTED = "5"
_ORDRSP_LINE_QTY_CHANGED = "3"
_ORDRSP_LINE_REJECTED = "7"


# ── EDIFACT escaping ───────────────────────────────────────────────────────────

def _edifact_escape(value):
    """Escape EDIFACT special characters per ISO 9735."""
    if not value:
        return ''
    return str(value).replace('?', '??').replace('+', '?+').replace(':', '?:').replace("'", "?'")


# ── Segment parsing helpers ────────────────────────────────────────────────────

def _parse_date(value: str) -> Optional[date]:
    """Parse YYMMDD or YYYYMMDD date string to date, or None on failure."""
    try:
        if len(value) == 6:
            return datetime.strptime(value, "%y%m%d").date()
        if len(value) == 8:
            return datetime.strptime(value, "%Y%m%d").date()
    except (ValueError, TypeError):
        pass
    return None


def _split_segments(raw: bytes) -> List[List[List[str]]]:
    """
    Parse raw EDIFACT bytes into structured segments.

    Returns: list of segments, each segment is a list of composites,
             each composite is a list of sub-elements.

    Handles both the standard EDIFACT segment terminator (0x27 = single quote)
    and the Windows-1252 right single quotation mark (0x92) which some
    Kestrelby EDIS files use as an alternate terminator.

    Example:
      "LIN+00010++0200000375621:EN'" ->
      [["LIN"], ["00010"], [""], ["0200000375621", "EN"]]
    """
    # Decode as Windows-1252 (superset of latin-1) to preserve 0x92 as a character
    text = raw.decode("cp1252", errors="replace")
    replacement_count = text.count('\ufffd')
    if replacement_count:
        _logger.warning(
            'EDI: file contains %d byte(s) invalid in Windows-1252 encoding. '
            'Data may be corrupt. If the trading partner uses a different encoding '
            '(e.g. UTF-8, ISO-8859-1), contact IT to update encoding configuration.',
            replacement_count,
        )

    # Skip UNA service string if present (always 9 chars: "UNA:+.? '")
    if text.startswith("UNA"):
        text = text[9:]

    # Normalise: replace Windows-1252 right single quote (\x92 → \u2019) with standard '
    # After cp1252 decode, 0x92 becomes '\u2019' (RIGHT SINGLE QUOTATION MARK)
    text = text.replace("\u2019", "'")

    result = []
    for seg_str in text.split("'"):
        seg_str = seg_str.strip().strip("\r\n")
        if not seg_str:
            continue
        composites = seg_str.split("+")
        parsed_seg = [comp.split(":") for comp in composites]
        result.append(parsed_seg)
    return result


def _get(seg: List[List[str]], comp_idx: int, sub_idx: int = 0, default: str = "") -> str:
    """Safe element accessor for parsed EDIFACT segment."""
    try:
        return seg[comp_idx][sub_idx] or default
    except IndexError:
        return default


# ── Message header extraction ──────────────────────────────────────────────────

def _extract_message_header(segments: List[List[List[str]]]) -> Dict:
    """Extract message-level header fields (before first LIN)."""
    header = {
        "message_type": None,
        "po_number": None,
        "order_date": None,
        "buyer_gln": None,
        "buyer_name": None,
        "vendor_code": None,
    }
    for seg in segments:
        tag = _get(seg, 0)
        if tag == "BGM":
            header["message_type"] = _get(seg, 1)
            header["po_number"] = _get(seg, 2)
        elif tag == "DTM" and _get(seg, 1, 0) == "137":
            header["order_date"] = _parse_date(_get(seg, 1, 1))
        elif tag == "NAD":
            role = _get(seg, 1)
            if role == "BY":
                header["buyer_gln"] = _get(seg, 2, 0)
                header["buyer_name"] = _get(seg, 5) or _get(seg, 6)
            elif role == "SU":
                header["vendor_code"] = _get(seg, 2, 0)
        elif tag == "LIN":
            break
    return header


# ── LIN group collector ────────────────────────────────────────────────────────

def _collect_lin_groups(segments: List[List[List[str]]]) -> List[Dict]:
    """Walk segments and collect one dict per LIN block."""
    groups = []
    current: Optional[Dict] = None
    in_lines = False

    for seg in segments:
        tag = _get(seg, 0)

        if tag == "LIN":
            if current is not None:
                groups.append(current)
            current = {
                "line_number": _parse_line_number(_get(seg, 1)),
                "action_code": _get(seg, 2) or None,
                "barcode": _get(seg, 3, 0),
                "barcode_qualifier": _get(seg, 3, 1),
                "buyer_article_no": None,
                "total_qty": None,
                "store_qty": None,
                "carton_qty": None,
                "uom": "EA",
                "unit_price": None,
                "currency": "NZD",
                "store_code": None,
                "delivery_date": None,
                "delivery_name": None,
            }
            in_lines = True
            continue

        if not in_lines or current is None:
            continue

        if tag == "PIA":
            qualifier = _get(seg, 2, 1)
            if qualifier == "IN":
                current["buyer_article_no"] = _get(seg, 2, 0)

        elif tag == "QTY":
            qty_type = _get(seg, 1, 0)
            try:
                qty_val = float(_get(seg, 1, 1) or "0")
            except ValueError:
                qty_val = 0.0
            uom = _get(seg, 1, 2) or "EA"
            if qty_type == "21":
                current["total_qty"] = qty_val
                current["uom"] = uom
            elif qty_type == "52":
                current["carton_qty"] = qty_val
            elif qty_type == "11":
                current["store_qty"] = qty_val
                current["uom"] = uom

        elif tag == "PRI":
            try:
                current["unit_price"] = float(_get(seg, 1, 1) or "0")
            except ValueError:
                pass

        elif tag == "CUX":
            current["currency"] = _get(seg, 1, 1) or "NZD"

        elif tag == "LOC":
            if _get(seg, 1) == "7":
                current["store_code"] = _get(seg, 2, 0)

        elif tag == "DTM" and _get(seg, 1, 0) == "2":
            current["delivery_date"] = _parse_date(_get(seg, 1, 1))

        elif tag == "NAD" and _get(seg, 1) == "UD":
            current["delivery_name"] = _get(seg, 4)

        elif tag in ("UNS", "CNT", "UNT", "UNZ"):
            if current is not None:
                groups.append(current)
                current = None
            break

    if current is not None:
        groups.append(current)

    return groups


def _parse_line_number(value: str) -> int:
    """Parse EDIFACT line number (e.g., '00010' -> 10)."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


# ── Store grouping ─────────────────────────────────────────────────────────────

def _group_by_store(
    lin_groups: List[Dict],
    po_number: str,
    order_date: Optional[date],
    document_type: str,
    raw_data: str,
) -> List[ParsedOrder]:
    """Group LIN groups by store code → one ParsedOrder per store."""
    store_lines: Dict[str, List[Dict]] = {}
    store_delivery_dates: Dict[str, Optional[date]] = {}

    for grp in lin_groups:
        # Skip cancelled lines in ORDCHG (action_code=3)
        if document_type == "change_order" and grp.get("action_code") == _LIN_ACTION_CANCELLED:
            _logger.debug(
                "[KestrelbyParser] Skipping cancelled line %s (action=3)",
                grp.get("line_number"),
            )
            continue

        store = grp.get("store_code") or "_DEFAULT"
        if store not in store_lines:
            store_lines[store] = []
        store_lines[store].append(grp)

        if store not in store_delivery_dates and grp.get("delivery_date"):
            store_delivery_dates[store] = grp["delivery_date"]

    orders = []
    for store_code, grps in store_lines.items():
        lines = []
        for grp in grps:
            qty = grp.get("store_qty") if grp.get("store_qty") is not None else grp.get("total_qty", 0.0)
            lines.append(ParsedOrderLine(
                product_code=grp["barcode"],
                description="",
                quantity=qty or 0.0,
                unit_price=grp.get("unit_price") or 0.0,
                line_number=grp["line_number"],
                uom=grp.get("uom"),
                carton_qty=grp.get("carton_qty"),
                buyer_article_no=grp.get("buyer_article_no"),
            ))

        actual_store_code = store_code if store_code != "_DEFAULT" else None
        orders.append(ParsedOrder(
            po_number=po_number,
            order_date=order_date,
            store_code=actual_store_code,
            requested_delivery_date=store_delivery_dates.get(store_code),
            lines=lines,
            document_type=document_type,
            raw_data=raw_data,
        ))

    return orders


# ── EAN-13 validation ─────────────────────────────────────────────────────────

def _ean13_check_digit_valid(barcode: str) -> bool:
    digits = [int(c) for c in barcode]
    total = sum(d * (3 if i % 2 else 1) for i, d in enumerate(digits[:-1]))
    return (10 - total % 10) % 10 == digits[-1]


def _validate_ean13_for_ordrsp(order_lines):
    """Raise UserError if any *shipping* line product has no valid EAN-13 barcode.

    Only lines that carry a confirmed qty (product_uom_qty > 0) transmit a barcode
    to Kestrelby. A fully out-of-stock line (qty 0, ORDRSP action 7) is an absence
    acknowledgement — it must not block the whole ORDRSP just because that product
    happens to lack a valid barcode (e.g. a fallback-matched OOS SKU)."""
    import re
    ean13_re = re.compile(r'^\d{13}$')
    missing = []
    for line in order_lines:
        if (getattr(line, 'product_uom_qty', 0) or 0) <= 0:
            continue
        barcode = getattr(line.product_id, 'barcode', '') or ''
        if not ean13_re.match(barcode) or not _ean13_check_digit_valid(barcode):
            missing.append(line.product_id.display_name)
    if missing:
        from odoo.exceptions import UserError
        raise UserError(
            "Cannot generate ORDRSP: the following products have no valid EAN-13 barcode:\n%s\n\n"
            "Add a 13-digit barcode with a valid check digit to each product before generating."
            % '\n'.join('  - %s' % name for name in missing)
        )


# ── ORDRSP generator ───────────────────────────────────────────────────────────

def _generate_ordrsp(review) -> bytes:
    """Build EDIFACT ORDRSP segments and return as bytes."""
    now = datetime.now()
    date_str = now.strftime("%y%m%d")
    time_str = now.strftime("%H%M")
    ref_num = str(10000 + secrets.randbelow(90000))

    partner = review.trading_partner_id
    so = review.sale_order_id

    vendor_code = (
        partner.partner_id.ref or "VENDOR"
        if partner and partner.partner_id else "VENDOR"
    )
    buyer_gln = (
        partner.partner_id.vat or partner.partner_id.ref or "BUYER"
        if partner and partner.partner_id else "BUYER"
    )
    buyer_name = partner.partner_id.name if partner and partner.partner_id else ""
    vendor_name = "MML Consumer Products"

    if review.state == "rejected":
        purpose = _ORDRSP_CANCELLED
    elif so and any(l.edi_qty_shortfall > 0 for l in so.order_line):
        purpose = _ORDRSP_CHANGED
    else:
        purpose = _ORDRSP_ACCEPTED

    # Validate EAN-13 barcodes before building segments — Kestrelby requires
    # valid EAN-13 on all ORDRSP lines; missing/invalid barcodes cause silent rejection.
    if so:
        _validate_ean13_for_ordrsp(so.order_line)

    segs = []
    segs.append("UNB+UNOA:3+%s:ZZ+%s:14+%s:%s+%s++ORDRSP" % (
        _edifact_escape(vendor_code), _edifact_escape(buyer_gln), date_str, time_str, ref_num))
    segs.append("UNH+1+ORDRSP:D:96A:UN:EAN005")
    segs.append("BGM+231+%s+%s" % (ref_num, purpose))
    segs.append("DTM+137:%s:102" % now.strftime("%Y%m%d"))
    segs.append("RFF+ON:%s" % _edifact_escape(review.customer_po_number or ""))
    segs.append("NAD+BY+%s::92++%s+%s" % (_edifact_escape(buyer_gln), _edifact_escape(buyer_name), ""))
    segs.append("NAD+SU+%s::92++%s" % (_edifact_escape(vendor_code), _edifact_escape(vendor_name)))

    line_count = 0
    if so:
        for sol in so.order_line.sorted(lambda l: l.edi_line_number or 0):
            if review.state == "rejected":
                line_action = _ORDRSP_LINE_REJECTED
            elif sol.edi_qty_shortfall > 0:
                # Fully out of stock (nothing ships) is a line rejection, not a
                # qty-change carrying a confirmed qty of 0.
                line_action = (
                    _ORDRSP_LINE_REJECTED if sol.product_uom_qty <= 0
                    else _ORDRSP_LINE_QTY_CHANGED
                )
            else:
                line_action = _ORDRSP_LINE_ACCEPTED

            barcode = _edifact_escape(sol.product_id.barcode or "")
            buyer_code = _edifact_escape(sol.product_id.default_code or "")
            confirmed_qty = sol.product_uom_qty
            # A rejected line (action 7) still passes NETWR = PRI × QTY through the
            # EDIStech VAN, which rejects a zero net value ("XML tag NETWR: Value
            # specified is zero"). Emit the ORIGINAL ordered qty (confirmed +
            # shortfall) so NETWR > 0 — the rejection is conveyed by the action
            # code, not a zero quantity. Matches the Kestrelby reference ORDRSPs
            # (Incorrect_Items / Cancelled), where action-7 lines keep the ordered qty.
            line_qty = confirmed_qty
            if line_action == _ORDRSP_LINE_REJECTED:
                line_qty = (sol.product_uom_qty or 0.0) + (sol.edi_qty_shortfall or 0.0)
            price = sol.price_unit
            store_code = _edifact_escape(review.store_code or "")

            delivery_date = ""
            if so.commitment_date:
                delivery_date = so.commitment_date.strftime("%Y%m%d")

            segs.append("LIN+%05d+%s+%s:EN" % (
                sol.edi_line_number or (line_count + 10), line_action, barcode))
            if buyer_code:
                segs.append("PIA+1+%s:IN" % buyer_code)
            segs.append("PRI+AAA:%.2f" % price)
            if store_code:
                segs.append("LOC+7+%s::92" % store_code)
            segs.append("QTY+11:%.3f:EA" % line_qty)
            if delivery_date:
                segs.append("DTM+2:%s:102" % delivery_date)

            line_count += 1

    segs.append("UNS+S")
    segs.append("CNT+2:%d" % line_count)

    # UNT count: all segments from UNH to UNT inclusive
    # segs[1] is UNH; next segment will be UNT
    # count = len(segs) - 1 (exclude UNB) + 1 (for UNT) = len(segs)
    unt_count = len(segs)
    segs.append("UNT+%d+1" % unt_count)
    segs.append("UNZ+1+%s" % ref_num)

    return ("'\r\n".join(segs) + "'\r\n").encode("utf-8")


# ── Main parser class ──────────────────────────────────────────────────────────

class KestrelbyParser(BaseEDIParser):
    """
    Parser for Kestrelby EDIFACT D96A purchase orders.

    Handles:
    - ORDERS (BGM+220) -> new purchase orders, one SO per store
    - ORDCHG (BGM+230) -> change orders, one change review per store

    Generates:
    - ORDRSP (BGM+231) -> order response / acknowledgement
    """

    def parse_file(
        self, raw_content: bytes, trading_partner
    ) -> List[ParsedOrder]:
        """
        Parse raw EDIFACT bytes into a list of ParsedOrder objects.

        One EDIFACT interchange contains a single ORDERS or ORDCHG message.
        Each message is split into one ParsedOrder per store (LOC+7 code).
        """
        raw_data = raw_content.decode("cp1252", errors="replace")
        segments = _split_segments(raw_content)

        if not segments:
            _logger.warning("[KestrelbyParser] Empty or unparseable file")
            return []

        header = _extract_message_header(segments)
        msg_type = header.get("message_type")

        if msg_type == _BGM_NEW_ORDER:
            document_type = "new_order"
        elif msg_type == _BGM_CHANGE_ORDER:
            document_type = "change_order"
        elif msg_type == _BGM_ORDER_RESPONSE:
            _logger.warning(
                "[KestrelbyParser] Received unexpected ORDRSP message — skipping"
            )
            return []
        else:
            raise EDIParseError(
                "Unrecognised BGM message type: %s (expected 220 or 230)" % msg_type
            )

        po_number = header.get("po_number")
        if not po_number:
            raise EDIParseError("BGM segment missing PO number")

        lin_groups = _collect_lin_groups(segments)
        if not lin_groups:
            _logger.info(
                "[KestrelbyParser] No LIN groups found in message for PO %s", po_number
            )
            return []

        orders = _group_by_store(
            lin_groups,
            po_number=po_number,
            order_date=header.get("order_date"),
            document_type=document_type,
            raw_data=raw_data,
        )

        _logger.info(
            "[KestrelbyParser] Parsed PO %s (%s): %d store order(s), %d total lines",
            po_number,
            document_type,
            len(orders),
            sum(len(o.lines) for o in orders),
        )
        return orders

    def generate_ack(self, review_record) -> bytes:
        """
        Generate EDIFACT ORDRSP (order response) for a processed order.

        Purpose codes: 29=accepted, 4=changed, 27=cancelled
        Line action codes: 5=accepted, 3=qty-changed, 7=rejected
        """
        return _generate_ordrsp(review_record)
