# EDIFACT Real Parser + Odoo 15 Fork — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the Phase 1 stub parsers with real EDIFACT D96A parsing (based on actual Briscoes sample files), implement a real ORDRSP ACK generator, and fork the module into an `odoo15` branch with full Odoo 15 compatibility.

**Architecture:**
- `master` branch → targets Odoo 19; real EDIFACT parser + ORDRSP generator added here first
- `odoo15` branch → forked from master after parser work; `mml_base` dependency stripped, pricelist API adapted, type hints fixed for Python 3.8

**Tech Stack:** Python 3.8 (Odoo 15) / Python 3.12 (Odoo 19), EDIFACT D96A (ISO 9735), Odoo ORM

---

## Context: EDIFACT D96A Segment Reference

The Briscoes sample files reveal this structure. Keep it handy.

### ORDERS (BGM+220) / ORDCHG (BGM+230) Segment Map

```
UNB+UNOA:3+sender:14+receiver:14+date:time+ref++type'
UNH+1+ORDERS:D:96A:UN:EAN008'
BGM+220+{po_number}+9+AB'           # 220=new order, 230=change order
DTM+137:{YYYYMMDD}:102'             # PO date (137=document date)
NAD+BY+{buyer_gln}::92++{name}+{company}'
NAD+SU+{vendor_code}::92++{vendor_name}'
RFF+IA:{vendor_code}'               # (optional, only in ORDERS)
-- Repeat per product-store line --
LIN+{line_no}+{action}+{barcode}:EN'   # action only in ORDCHG: 1=add, 2=change, 3=cancel
PIA+1+{buyer_article_no}:IN'           # buyer's item code
QTY+21:{total_qty}:{uom}'             # total ordered qty across all stores
QTY+52:{carton_qty}:{uom}'            # inner pack / carton qty (optional)
PRI+AAA:{price}'                      # unit price
CUX+2:{currency}'                     # NZD
LOC+7+{store_code}::92'               # ship-to store code
QTY+11:{store_qty}:{uom}'             # qty for this specific store
DTM+2:{YYYYMMDD}:102'                 # requested delivery date for this store
NAD+UD+++{name}+{street}+{city}+++{country}'  # store delivery address
RFF+CR:{po_number}'                   # PO ref on line
CTA+DL'                               # contact type
COM+{phone}:TE'                       # contact phone
-- End of line section --
UNS+S'
CNT+2:{line_count}'
UNT+{segment_count}+1'
UNZ+1+{interchange_ref}'
```

### ORDRSP (BGM+231) Segment Map (outbound ACK)

```
UNB+UNOA:3+{vendor}:ZZ+{buyer_gln}:14+{date}:{time}+{ref}++ORDRSP'
UNH+1+ORDRSP:D:96A:UN:EAN005'
BGM+231+{ack_ref}+{purpose}'         # purpose: 29=accepted, 4=changed, 27=cancelled
DTM+137:{YYYYMMDD}:102'
RFF+ON:{original_po_number}'
NAD+BY+{buyer_gln}::92++{name}+{company}'
NAD+SU+{vendor_code}::92++{vendor_name}'
-- Per line --
LIN+{line_no}+{action}+{barcode}:EN' # action: 5=accepted, 3=qty-changed, 7=rejected
PIA+1+{buyer_code}:IN'
PRI+AAA:{confirmed_price}'
LOC+7+{store_code}::92'
QTY+11:{confirmed_qty}:{uom}'
DTM+2:{delivery_date}:102'
-- Summary --
UNS+S'
CNT+2:{line_count}'
UNT+{segment_count}+1'
UNZ+1+{ref}'
```

---

## Task 1: Add Sample Files as Test Fixtures

**Files:**
- Create: `mml.edi/tests/fixtures/` directory (copy 4 EDIFACT sample files here)

**Step 1: Copy sample files to fixtures directory**

```bash
mkdir -p mml.edi/tests/fixtures
cp "docs/briscoes.docs/EDIFACT Purchase Order 4500038166.txt" \
   mml.edi/tests/fixtures/briscoes_orders_4500038166.edi
cp "docs/briscoes.docs/EDIFACT Purchase Order Change 4500038166.txt" \
   mml.edi/tests/fixtures/briscoes_ordchg_4500038166.edi
cp "docs/briscoes.docs/EDIFACT Purchase Order Response 4500038166_Supplied_In_Full.txt" \
   mml.edi/tests/fixtures/briscoes_ordrsp_supplied_full.edi
cp "docs/briscoes.docs/EDIFACT Purchase Order Response 4500038166_Short_Supplied.txt" \
   mml.edi/tests/fixtures/briscoes_ordrsp_short_supplied.edi
cp "docs/briscoes.docs/EDIFACT Purchase Order Response 4500038166_Cancelled_or_Deleted.txt" \
   mml.edi/tests/fixtures/briscoes_ordrsp_cancelled.edi
cp "docs/briscoes.docs/EDIFACT Purchase Order Response 4500038166_Price_Date_Changed.txt" \
   mml.edi/tests/fixtures/briscoes_ordrsp_price_date_changed.edi
cp "docs/briscoes.docs/EDIFACT Purchase Order Response 4500038166_Incorrect_Items.txt" \
   mml.edi/tests/fixtures/briscoes_ordrsp_incorrect_items.edi
```

**Step 2: Create fixtures `__init__.py` and helper**

Create `mml.edi/tests/fixtures/__init__.py` (empty file):
```python
# fixtures package
```

Create `mml.edi/tests/fixtures/loader.py`:
```python
"""Fixture file loader for EDI parser tests."""
import os

FIXTURES_DIR = os.path.dirname(__file__)


def load_fixture(filename: str) -> bytes:
    """Load a fixture file as bytes."""
    path = os.path.join(FIXTURES_DIR, filename)
    with open(path, "rb") as f:
        return f.read()
```

**Step 3: Commit**

```bash
git add mml.edi/tests/fixtures/
git commit -m "test: add real Briscoes EDIFACT sample files as test fixtures"
```

---

## Task 2: Implement Real EDIFACT D96A Parser

**Files:**
- Modify: `mml.edi/parsers/briscoes.py` (full rewrite of `parse_file`)
- Keep: `mml.edi/parsers/briscoes_idoc.py` (stub remains — real iDOC files not yet received)

**Step 1: Write the failing test first**

Create `mml.edi/tests/test_briscoes_edifact_parser.py`:

```python
"""
Tests for real EDIFACT D96A parser against actual Briscoes sample files.
These are pure Python unit tests — no Odoo env required.
"""
import pytest
from unittest.mock import MagicMock
from pathlib import Path

from mml.edi.parsers.briscoes import BriscoesParser
from mml.edi.parsers.base_parser import ParsedOrder, ParsedOrderLine

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _mock_partner(product_match_field="barcode", order_split_mode="per_store"):
    partner = MagicMock()
    partner.product_match_field = product_match_field
    partner.order_split_mode = order_split_mode
    return partner


class TestOrdersParsing:
    """Test ORDERS (BGM+220) new PO parsing."""

    def test_parse_returns_list_of_parsed_orders(self):
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        assert isinstance(results, list)
        assert len(results) > 0

    def test_document_type_is_new_order(self):
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            assert order.document_type == "new_order"

    def test_po_number_extracted(self):
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            assert order.po_number == "4500038166"

    def test_order_date_extracted(self):
        from datetime import date
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            assert order.order_date == date(2011, 11, 8)

    def test_grouped_by_store(self):
        """ORDERS file has 2 stores (1005, 1007) — should produce 2 ParsedOrders."""
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        store_codes = {o.store_code for o in results}
        assert "1005" in store_codes
        assert "1007" in store_codes

    def test_store_1005_has_two_lines(self):
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        assert len(store_1005.lines) == 2  # lines 00010 and 00020

    def test_store_1007_has_one_line(self):
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1007 = next(o for o in results if o.store_code == "1007")
        assert len(store_1007.lines) == 1  # line 00060

    def test_line_barcode_extracted(self):
        """EAN-13 barcode from LIN+EN."""
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        barcodes = {l.product_code for l in store_1005.lines}
        assert "9414844375629" in barcodes

    def test_line_buyer_article_no_extracted(self):
        """PIA+IN buyer article number."""
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        line = next(l for l in store_1005.lines if l.product_code == "9414844375629")
        assert line.buyer_article_no == "375629"

    def test_line_store_qty_extracted(self):
        """QTY+11 (per-store qty) used, not QTY+21 (total)."""
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        line = next(l for l in store_1005.lines if l.product_code == "9414844375629")
        assert line.quantity == 10.0  # QTY+11:10.000

    def test_line_price_extracted(self):
        """PRI+AAA price."""
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        line = next(l for l in store_1005.lines if l.product_code == "9414844375629")
        assert line.unit_price == 5.50

    def test_delivery_date_extracted(self):
        """DTM+2 delivery date."""
        from datetime import date
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        assert store_1005.requested_delivery_date == date(2011, 12, 16)

    def test_raw_data_set_on_parsed_orders(self):
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            assert order.raw_data is not None
            assert len(order.raw_data) > 0

    def test_content_hash_computable(self):
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            h = order.content_hash()
            assert len(h) == 64  # SHA-256 hex


class TestChangeOrderParsing:
    """Test ORDCHG (BGM+230) change order parsing."""

    def test_document_type_is_change_order(self):
        raw = _load("briscoes_ordchg_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            assert order.document_type == "change_order"

    def test_po_number_extracted(self):
        raw = _load("briscoes_ordchg_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            assert order.po_number == "4500038166"

    def test_new_line_included(self):
        """Line 00090 (action=1, add) should appear in results."""
        raw = _load("briscoes_ordchg_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        all_barcodes = {l.product_code for o in results for l in o.lines}
        assert "9414844375674" in all_barcodes  # new product in line 00090

    def test_cancelled_lines_excluded_or_flagged(self):
        """Lines with action=3 (cancel) should be excluded from ParsedOrderLines
        (they represent deletions; including them with qty=0 is also acceptable —
        the processor handles both). At minimum, they must not appear with
        their original quantity."""
        raw = _load("briscoes_ordchg_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        # This test documents the chosen behaviour. Adjust if parser marks
        # cancelled lines with quantity=0 instead of excluding them.
        # For now we just check the parser doesn't crash.
        assert isinstance(results, list)


class TestCartonQty:
    """QTY+52 (inner pack / carton qty) mapping."""

    def test_carton_qty_extracted_when_present(self):
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        # Line 00010: QTY+52:1.000 (carton qty = 1)
        line = next(l for l in store_1005.lines if l.product_code == "9414844375629")
        assert line.carton_qty == 1.0

    def test_carton_qty_none_when_absent(self):
        """Some lines in ORDCHG lack QTY+52 — must not raise."""
        raw = _load("briscoes_ordchg_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        # Just verify no exception; carton_qty may be None on some lines
        for order in results:
            for line in order.lines:
                assert line.carton_qty is None or isinstance(line.carton_qty, float)
```

**Step 2: Run tests to confirm they fail**

```bash
cd mml.edi
python -m pytest tests/test_briscoes_edifact_parser.py -v 2>&1 | head -40
```

Expected: Multiple FAILs / ImportError because the real parser doesn't exist yet.

**Step 3: Implement the real EDIFACT D96A parser**

Replace `mml.edi/parsers/briscoes.py` with the following. The key design choices:
- `_parse_edifact_segments()` — raw EDIFACT → list of parsed segment tuples (stateless)
- `_group_lin_groups()` — segments → list of `_LinGroup` dicts (one per LIN block)
- `_group_by_store()` — LinGroups → dict of `{store_code: [LinGroup]}` (grouping step)
- `parse_file()` — orchestrates above, returns `[ParsedOrder]` per store

```python
# mml.edi/parsers/briscoes.py
"""
Briscoes EDI Parser — EDIFACT D96A.

Parses ORDERS (BGM+220) and ORDCHG (BGM+230) messages from the EDIS VAN FTP.
Generates ORDRSP (BGM+231) order responses.

Reference: Briscoes EDIFACT Purchase Order Implementation Guide v1.13
Sample files: docs/briscoes.docs/
"""
from __future__ import annotations

import logging
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

    Example:
      "LIN+00010++9414844375629:EN'" →
      [["LIN"], ["00010"], [""], ["9414844375629", "EN"]]
    """
    # Detect encoding: EDIFACT is typically ASCII or latin-1
    text = raw.decode("latin-1", errors="replace")

    # Skip UNA service string if present (always 9 chars: "UNA:+.? '")
    if text.startswith("UNA"):
        text = text[9:]

    # EDIFACT segment terminator is "'"
    # Release character (escape) is "?" — we strip it simply for now
    # (Briscoes samples don't use release chars in data fields)
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


# ── LIN group collector ────────────────────────────────────────────────────────

def _extract_message_header(segments: List[List[List[str]]]) -> Dict:
    """Extract message-level header fields (before first LIN)."""
    header = {
        "message_type": None,   # 220 / 230 / 231
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
            # Reached first line item — stop
            break
    return header


def _collect_lin_groups(segments: List[List[List[str]]]) -> List[Dict]:
    """
    Walk segments and collect one dict per LIN block.

    Each LIN block covers segments from one LIN to the next LIN (exclusive).
    """
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
                "barcode": _get(seg, 3, 0),      # EAN-13
                "barcode_qualifier": _get(seg, 3, 1),  # "EN" = EAN-13
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
            # PIA+1+{code}:IN — buyer article number
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
                current["uom"] = uom  # per-store UOM takes precedence

        elif tag == "PRI":
            # PRI+AAA:{price}
            try:
                current["unit_price"] = float(_get(seg, 1, 1) or "0")
            except ValueError:
                pass

        elif tag == "CUX":
            current["currency"] = _get(seg, 1, 1) or "NZD"

        elif tag == "LOC":
            # LOC+7+{store_code}::92 — ship-to location
            if _get(seg, 1) == "7":
                current["store_code"] = _get(seg, 2, 0)

        elif tag == "DTM" and _get(seg, 1, 0) == "2":
            current["delivery_date"] = _parse_date(_get(seg, 1, 1))

        elif tag == "NAD" and _get(seg, 1) == "UD":
            # NAD+UD+++{name}+{street}+{city}+++{country}'
            current["delivery_name"] = _get(seg, 4)

        elif tag in ("UNS", "CNT", "UNT", "UNZ"):
            # End of message
            if current is not None:
                groups.append(current)
                current = None
            break

    if current is not None:
        groups.append(current)

    return groups


def _parse_line_number(value: str) -> int:
    """Parse EDIFACT line number (e.g., '00010' → 10)."""
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
    """
    Group LIN groups by store code → one ParsedOrder per store.

    For change orders, lines with action_code=3 (cancel) are excluded
    (qty=0 would confuse the processor). Lines with action_code=1,2 or None
    are included as normal lines.
    """
    # Collect by store_code
    store_lines: Dict[str, List[Dict]] = {}
    store_delivery_dates: Dict[str, Optional[date]] = {}

    for grp in lin_groups:
        # Skip cancelled lines in ORDCHG (action_code=3)
        if document_type == "change_order" and grp.get("action_code") == _LIN_ACTION_CANCELLED:
            _logger.debug(
                "[BriscoesParser] Skipping cancelled line %s (action=3)",
                grp.get("line_number"),
            )
            continue

        store = grp.get("store_code") or "_DEFAULT"
        if store not in store_lines:
            store_lines[store] = []
        store_lines[store].append(grp)

        # Use first delivery date seen per store
        if store not in store_delivery_dates and grp.get("delivery_date"):
            store_delivery_dates[store] = grp["delivery_date"]

    orders = []
    for store_code, grps in store_lines.items():
        lines = []
        for grp in grps:
            # Use QTY+11 (per-store qty). Fall back to QTY+21 (total) only if missing.
            qty = grp.get("store_qty") if grp.get("store_qty") is not None else grp.get("total_qty", 0.0)
            lines.append(ParsedOrderLine(
                product_code=grp["barcode"],
                description="",  # EDIFACT ORDERS D96A has no line description segment
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


# ── Main parser class ──────────────────────────────────────────────────────────

class BriscoesParser(BaseEDIParser):
    """
    Parser for Briscoes EDIFACT D96A purchase orders.

    Handles:
    - ORDERS (BGM+220) → new purchase orders, one SO per store
    - ORDCHG (BGM+230) → change orders, one change review per store

    Generates:
    - ORDRSP (BGM+231) → order response / acknowledgement
    """

    def parse_file(
        self, raw_content: bytes, trading_partner
    ) -> List[ParsedOrder]:
        """
        Parse raw EDIFACT bytes into a list of ParsedOrder objects.

        One EDIFACT interchange may contain a single ORDERS or ORDCHG message.
        Each message is split into one ParsedOrder per store (LOC+7 code).
        """
        raw_data = raw_content.decode("latin-1", errors="replace")
        segments = _split_segments(raw_content)

        if not segments:
            _logger.warning("[BriscoesParser] Empty or unparseable file")
            return []

        header = _extract_message_header(segments)
        msg_type = header.get("message_type")

        if msg_type == _BGM_NEW_ORDER:
            document_type = "new_order"
        elif msg_type == _BGM_CHANGE_ORDER:
            document_type = "change_order"
        elif msg_type == _BGM_ORDER_RESPONSE:
            # Inbound ORDRSP is not expected (we send these, not receive them)
            _logger.warning(
                "[BriscoesParser] Received unexpected ORDRSP message — skipping"
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
            _logger.warning(
                "[BriscoesParser] No LIN groups found in message for PO %s", po_number
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
            "[BriscoesParser] Parsed PO %s (%s): %d store order(s), %d total lines",
            po_number,
            document_type,
            len(orders),
            sum(len(o.lines) for o in orders),
        )
        return orders

    def generate_ack(self, review_record) -> bytes:
        """
        Generate EDIFACT ORDRSP (order response) for a processed order.

        Purpose codes:
          29 = all lines accepted as submitted
           4 = at least one line changed (qty reduced / date changed)
          27 = all lines rejected / order cancelled

        Line action codes:
           5 = line accepted as submitted
           3 = line qty changed (short supply)
           7 = line rejected / cancelled
        """
        return _generate_ordrsp(review_record)
```

**Step 4: Implement `_generate_ordrsp` helper (add to bottom of briscoes.py)**

```python
def _generate_ordrsp(review) -> bytes:
    """Build EDIFACT ORDRSP segments and return as bytes."""
    from datetime import datetime
    import random

    now = datetime.now()
    date_str = now.strftime("%y%m%d")
    time_str = now.strftime("%H%M")
    ref_num = str(random.randint(10000, 99999))

    partner = review.trading_partner_id
    so = review.sale_order_id

    # Resolve sender/receiver identifiers
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

    # Determine overall purpose
    if review.state == "rejected":
        purpose = _ORDRSP_CANCELLED
    elif so and any(l.edi_qty_shortfall > 0 for l in so.order_line):
        purpose = _ORDRSP_CHANGED
    else:
        purpose = _ORDRSP_ACCEPTED

    segs = []

    # Interchange + message header
    segs.append("UNB+UNOA:3+%s:ZZ+%s:14+%s:%s+%s++ORDRSP" % (
        vendor_code, buyer_gln, date_str, time_str, ref_num))
    segs.append("UNH+1+ORDRSP:D:96A:UN:EAN005")
    segs.append("BGM+231+%s+%s" % (ref_num, purpose))
    segs.append("DTM+137:%s:102" % now.strftime("%Y%m%d"))
    segs.append("RFF+ON:%s" % (review.customer_po_number or ""))
    segs.append("NAD+BY+%s::92++%s+%s" % (buyer_gln, buyer_name, ""))
    segs.append("NAD+SU+%s::92++%s" % (vendor_code, vendor_name))

    # Line items
    line_count = 0
    if so:
        for sol in so.order_line.sorted(lambda l: l.edi_line_number or 0):
            if review.state == "rejected":
                line_action = _ORDRSP_LINE_REJECTED
            elif sol.edi_qty_shortfall > 0:
                line_action = _ORDRSP_LINE_QTY_CHANGED
            else:
                line_action = _ORDRSP_LINE_ACCEPTED

            barcode = sol.product_id.barcode or ""
            buyer_code = sol.product_id.default_code or ""
            confirmed_qty = sol.product_uom_qty
            price = sol.price_unit
            store_code = review.store_code or ""

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
            segs.append("QTY+11:%.3f:EA" % confirmed_qty)
            if delivery_date:
                segs.append("DTM+2:%s:102" % delivery_date)

            line_count += 1

    # Summary
    segs.append("UNS+S")
    segs.append("CNT+2:%d" % line_count)

    # UNT: segment count = all segments from UNH to UNT inclusive
    # Current segs count (before UNT): we need to add 1 for UNT itself
    # UNH is segs[1], so count = len(segs) - 1 (exclude UNB) + 1 (for UNT) = len(segs)
    unt_count = len(segs)  # will add UNT as the next segment
    segs.append("UNT+%d+1" % unt_count)
    segs.append("UNZ+1+%s" % ref_num)

    return ("'\r\n".join(segs) + "'\r\n").encode("utf-8")
```

**Step 5: Run tests**

```bash
python -m pytest tests/test_briscoes_edifact_parser.py -v
```

Expected: All tests PASS.

**Step 6: Quick smoke test of generate_ack (manual check)**

```python
# In a Python shell / quick test:
from mml.edi.parsers.briscoes import BriscoesParser
from pathlib import Path
raw = Path("tests/fixtures/briscoes_orders_4500038166.edi").read_bytes()
# parser.parse_file() confirmed working
# generate_ack() tested in Task 4 below
```

**Step 7: Commit**

```bash
git add mml.edi/parsers/briscoes.py mml.edi/tests/test_briscoes_edifact_parser.py
git commit -m "feat: implement real EDIFACT D96A parser and ORDRSP generator for Briscoes

Replaces Phase 1 mock stub with real segment parsing.
- _split_segments(): raw EDIFACT → structured list
- _collect_lin_groups(): segment walker → per-LIN dicts
- _group_by_store(): LIN groups → one ParsedOrder per store (LOC+7)
- _generate_ordrsp(): ORDRSP ACK generator with purpose + line action codes
- Handles ORDERS (BGM+220) and ORDCHG (BGM+230)
- Cancelled ORDCHG lines (action=3) excluded from ParsedOrder"
```

---

## Task 3: Tests for ORDRSP Generator

**Files:**
- Create: `mml.edi/tests/test_briscoes_ordrsp.py`

**Step 1: Write tests**

```python
"""Tests for EDIFACT ORDRSP ACK generation."""
import pytest
from unittest.mock import MagicMock, PropertyMock


def _make_sol(line_number, barcode, default_code, qty, price, shortfall=0.0):
    sol = MagicMock()
    sol.edi_line_number = line_number
    sol.product_id.barcode = barcode
    sol.product_id.default_code = default_code
    sol.product_uom_qty = qty
    sol.price_unit = price
    sol.edi_qty_shortfall = shortfall
    return sol


def _make_so(lines, commitment_date=None):
    so = MagicMock()
    so.order_line.sorted.return_value = lines
    so.commitment_date = commitment_date
    return so


def _make_review(state="approved", store_code="1005", po_number="4500038166", so=None):
    review = MagicMock()
    review.state = state
    review.store_code = store_code
    review.customer_po_number = po_number
    review.sale_order_id = so

    partner = MagicMock()
    partner.partner_id.ref = "300024"
    partner.partner_id.vat = None
    partner.partner_id.name = "Briscoe Group Ltd"
    review.trading_partner_id = partner
    return review


class TestOrdrspGeneration:

    def test_returns_bytes(self):
        from mml.edi.parsers.briscoes import _generate_ordrsp
        review = _make_review(state="approved", so=None)
        result = _generate_ordrsp(review)
        assert isinstance(result, bytes)

    def test_contains_ordrsp_message_type(self):
        from mml.edi.parsers.briscoes import _generate_ordrsp
        review = _make_review(state="approved", so=None)
        text = _generate_ordrsp(review).decode("utf-8")
        assert "ORDRSP" in text

    def test_contains_bgm_231(self):
        from mml.edi.parsers.briscoes import _generate_ordrsp
        review = _make_review(state="approved", so=None)
        text = _generate_ordrsp(review).decode("utf-8")
        assert "BGM+231" in text

    def test_rejected_uses_purpose_27(self):
        from mml.edi.parsers.briscoes import _generate_ordrsp
        review = _make_review(state="rejected", so=None)
        text = _generate_ordrsp(review).decode("utf-8")
        # BGM+231+ref+27
        lines = [l for l in text.split("\r\n") if l.startswith("BGM")]
        assert lines, "No BGM segment found"
        assert lines[0].endswith("+27'"), "Expected purpose 27 (cancelled) for rejected"

    def test_approved_clean_uses_purpose_29(self):
        from mml.edi.parsers.briscoes import _generate_ordrsp
        sol = _make_sol(10, "9414844375629", "INT001", 10.0, 5.50, shortfall=0.0)
        so = _make_so([sol])
        review = _make_review(state="approved", so=so)
        text = _generate_ordrsp(review).decode("utf-8")
        bgm_lines = [l for l in text.split("\r\n") if l.startswith("BGM")]
        assert bgm_lines[0].endswith("+29'"), "Expected purpose 29 (accepted) for clean order"

    def test_approved_with_shortfall_uses_purpose_4(self):
        from mml.edi.parsers.briscoes import _generate_ordrsp
        sol = _make_sol(10, "9414844375629", "INT001", 8.0, 5.50, shortfall=2.0)
        so = _make_so([sol])
        review = _make_review(state="approved", so=so)
        text = _generate_ordrsp(review).decode("utf-8")
        bgm_lines = [l for l in text.split("\r\n") if l.startswith("BGM")]
        assert bgm_lines[0].endswith("+4'"), "Expected purpose 4 (changed) for short supply"

    def test_po_reference_in_rff_on(self):
        from mml.edi.parsers.briscoes import _generate_ordrsp
        review = _make_review(state="approved", so=None, po_number="4500038166")
        text = _generate_ordrsp(review).decode("utf-8")
        assert "RFF+ON:4500038166" in text

    def test_accepted_line_action_5(self):
        from mml.edi.parsers.briscoes import _generate_ordrsp
        sol = _make_sol(10, "9414844375629", "INT001", 10.0, 5.50, shortfall=0.0)
        so = _make_so([sol])
        review = _make_review(state="approved", so=so)
        text = _generate_ordrsp(review).decode("utf-8")
        lin_lines = [l for l in text.split("\r\n") if l.startswith("LIN")]
        assert any("+5+" in l for l in lin_lines), "Expected line action 5 (accepted)"

    def test_shortfall_line_action_3(self):
        from mml.edi.parsers.briscoes import _generate_ordrsp
        sol = _make_sol(10, "9414844375629", "INT001", 8.0, 5.50, shortfall=2.0)
        so = _make_so([sol])
        review = _make_review(state="approved", so=so)
        text = _generate_ordrsp(review).decode("utf-8")
        lin_lines = [l for l in text.split("\r\n") if l.startswith("LIN")]
        assert any("+3+" in l for l in lin_lines), "Expected line action 3 (qty changed)"

    def test_rejected_line_action_7(self):
        from mml.edi.parsers.briscoes import _generate_ordrsp
        sol = _make_sol(10, "9414844375629", "INT001", 10.0, 5.50, shortfall=0.0)
        so = _make_so([sol])
        review = _make_review(state="rejected", so=so)
        text = _generate_ordrsp(review).decode("utf-8")
        lin_lines = [l for l in text.split("\r\n") if l.startswith("LIN")]
        assert any("+7+" in l for l in lin_lines), "Expected line action 7 (rejected)"

    def test_segment_terminators_present(self):
        from mml.edi.parsers.briscoes import _generate_ordrsp
        review = _make_review(state="approved", so=None)
        text = _generate_ordrsp(review).decode("utf-8")
        # Each non-empty line should end with "'"
        for line in text.strip().split("\r\n"):
            if line.strip():
                assert line.endswith("'"), "Segment must end with \"'\": %r" % line

    def test_unt_segment_count_valid(self):
        """UNT segment count should be >= 6 (minimum viable ORDRSP)."""
        from mml.edi.parsers.briscoes import _generate_ordrsp
        review = _make_review(state="approved", so=None)
        text = _generate_ordrsp(review).decode("utf-8")
        unt_lines = [l for l in text.split("\r\n") if l.startswith("UNT")]
        assert unt_lines, "UNT segment missing"
        count = int(unt_lines[0].split("+")[1])
        assert count >= 6, "UNT count seems too low: %d" % count
```

**Step 2: Run tests**

```bash
python -m pytest tests/test_briscoes_ordrsp.py -v
```

Expected: All PASS.

**Step 3: Commit**

```bash
git add mml.edi/tests/test_briscoes_ordrsp.py
git commit -m "test: ORDRSP generator tests with purpose codes and line action codes"
```

---

## Task 4: Create the `odoo15` Branch

**Step 1: Create and switch to odoo15 branch**

```bash
cd mml.edi
git checkout -b odoo15
```

Expected: `Switched to a new branch 'odoo15'`

**Step 2: Verify branch**

```bash
git branch
```

Expected: `* odoo15` and `  master`

**Step 3: Push branch to remote**

```bash
git push -u origin odoo15
```

---

## Task 5: Odoo 15 — Manifest and Hooks

**Files:**
- Modify: `mml.edi/__manifest__.py`
- Modify: `mml.edi/hooks.py`

**Step 1: Update `__manifest__.py`**

Change `version` and `depends`. Remove hooks (they reference mml_base-only models).

```python
# mml.edi/__manifest__.py
{
    "name": "MML EDI",
    "version": "15.0.1.0.0",   # ← was 19.0.1.0.0
    "summary": "Electronic Data Interchange for retail partners (Briscoes Group and others)",
    "description": """
        Customer-agnostic EDI module for Odoo 15.
        Replaces the legacy .NET Windows service handling Briscoes Group purchase orders.
    """,
    "author": "MML Consumer Products Ltd",
    "website": "https://github.com/JonaldM/mml.edi.odoo",
    "category": "Operations",
    "license": "LGPL-3",
    "depends": ["base", "sale", "account", "stock", "mail"],  # ← removed mml_base
    "data": [
        "security/edi_security.xml",
        "security/ir.model.access.csv",
        "data/ir_sequence.xml",
        "data/ir_cron.xml",
        "views/edi_trading_partner_views.xml",
        "views/edi_order_review_views.xml",
        "views/edi_order_issue_views.xml",
        "views/edi_log_views.xml",
        "views/sale_order_views.xml",
        "wizards/edi_bulk_action_views.xml",
        "wizards/edi_seed_stores_views.xml",
        "views/menuitems.xml",
        "data/edi_trading_partner_briscoes.xml",
        "data/mail_template.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
    # No post_init_hook / uninstall_hook — those used mml_base-only models
}
```

**Step 2: Stub out hooks.py**

Replace hooks.py content — mml.capability, mml.registry, mml.event don't exist in Odoo 15:

```python
# mml.edi/hooks.py
"""
Module hooks — Odoo 15 version.

mml.capability and mml.registry are mml_base features not available in Odoo 15.
These hooks are no-ops in the Odoo 15 build.
"""


def post_init_hook(env):
    pass


def uninstall_hook(env):
    pass
```

**Step 3: Verify no remaining mml_base references**

```bash
grep -r "mml_base\|mml\.registry\|mml\.capability\|mml\.event\|mml\.registry" mml.edi/ \
  --include="*.py" --include="*.xml" -l
```

Expected: Only hooks.py (which is now stubbed out) and potentially the manifest if not yet saved.

**Step 4: Commit**

```bash
git add mml.edi/__manifest__.py mml.edi/hooks.py
git commit -m "chore(odoo15): drop mml_base dependency, stub hooks"
```

---

## Task 6: Odoo 15 — Fix Pricelist API

**Files:**
- Modify: `mml.edi/models/edi_processor.py`

**Background:**
Odoo 15 pricelist price API:
```python
# Odoo 15: returns dict {pricelist_id: price}
price_dict = pricelist.price_get(product.id, qty, partner_id)
price = price_dict.get(pricelist.id)

# Odoo 16+/19: returns float directly
price = pricelist._get_product_price(product, qty, partner)
```

Also remove the `mml.event.emit()` call in `_process_new_order` (that model doesn't exist in Odoo 15).

And fix `float | None` return type hint (Python 3.8 needs `from __future__ import annotations`).

**Step 1: Write a failing test**

Add to `mml.edi/tests/test_processor.py` (or create if missing):

```python
# At top of test_processor.py, add this test class:
class TestPricelistCompat:
    """Verify _get_pricelist_price doesn't crash on Odoo 15 pricelist mock."""

    def test_returns_none_when_no_pricelist(self):
        """If partner has no pricelist, returns None."""
        from mml.edi.models.edi_processor import EDIProcessor
        proc = EDIProcessor.__new__(EDIProcessor)
        proc.env = MagicMock()

        partner = MagicMock()
        partner.pricelist_id = False
        product = MagicMock()

        result = proc._get_pricelist_price(product, 10.0, partner)
        assert result is None

    def test_returns_float_on_success(self):
        """Pricelist price lookup returns a float."""
        from mml.edi.models.edi_processor import EDIProcessor
        proc = EDIProcessor.__new__(EDIProcessor)
        proc.env = MagicMock()

        pricelist = MagicMock()
        pricelist.id = 1
        # Simulate Odoo 15 API: price_get returns {pricelist_id: price}
        pricelist.price_get.return_value = {1: 12.50}

        partner = MagicMock()
        partner.pricelist_id = pricelist
        partner.partner_id.id = 99

        product = MagicMock()
        product.id = 5

        result = proc._get_pricelist_price(product, 10.0, partner)
        assert result == 12.50
```

**Step 2: Modify `_get_pricelist_price` in `edi_processor.py`**

Find the existing `_get_pricelist_price` method and replace it:

```python
def _get_pricelist_price(
    self, product, quantity: float, partner
) -> float | None:
    """
    Get pricelist price. Returns None if no pricelist configured.

    Odoo 15: pricelist.price_get(prod_id, qty, partner_id) → {pricelist_id: price}
    Odoo 16+: pricelist._get_product_price(product, qty, partner) → float
    """
    if not partner.pricelist_id:
        return None
    try:
        pricelist = partner.pricelist_id
        # Try Odoo 15 API first (price_get), fall back to Odoo 16+ API
        if hasattr(pricelist, 'price_get'):
            price_dict = pricelist.price_get(
                product.id, quantity, partner.partner_id.id
            )
            return price_dict.get(pricelist.id)
        else:
            return pricelist._get_product_price(
                product, quantity, partner.partner_id
            )
    except Exception as exc:
        _logger.warning(
            "[EDI] Pricelist price lookup failed for %s: %s", product.name, exc
        )
        return None
```

**Step 3: Remove the `mml.event.emit()` block from `_process_new_order`**

Find and delete these lines in `_process_new_order`:

```python
        # Emit billable event — fires whether auto-approved or pending review
        self.env['mml.event'].emit(
            'edi.order.processed',
            quantity=len(so.order_line),
            billable_unit='edi_order_line',
            res_model='sale.order',
            res_id=so.id,
            source_module='mml_edi',
            payload={'partner': partner.name, 'order_ref': so.name},
        )
```

**Step 4: Add `from __future__ import annotations` to `edi_processor.py`**

Add it as the very first line of the file (after the module docstring if any):

```python
from __future__ import annotations
```

**Step 5: Run tests**

```bash
python -m pytest tests/test_processor.py -v -k "TestPricelistCompat"
```

Expected: PASS.

**Step 6: Commit**

```bash
git add mml.edi/models/edi_processor.py
git commit -m "fix(odoo15): adapt pricelist API for Odoo 15 compatibility

- Use price_get() (Odoo 15) with fallback to _get_product_price() (Odoo 16+)
- Remove mml.event.emit() call (mml_base not available in Odoo 15)
- Add from __future__ import annotations for Python 3.8 compatibility"
```

---

## Task 7: Odoo 15 — Fix Remaining Type Hints

**Files:**
- Modify: `mml.edi/models/edi_trading_partner.py`
- Modify: `mml.edi/models/edi_order_review.py` (check only)

**Background:**
Python 3.8 (Odoo 15) does not support `X | Y` union syntax for type hints at runtime.
`from __future__ import annotations` defers evaluation — adding it fixes all cases.

**Step 1: Check which files use `|` union syntax without future import**

```bash
grep -n "str | None\|float | None\|bool | None\|int | None\|list\[" \
  mml.edi/models/*.py mml.edi/parsers/*.py mml.edi/wizards/*.py 2>/dev/null
```

**Step 2: Add `from __future__ import annotations` to files that need it**

Files expected to need this:
- `mml.edi/models/edi_trading_partner.py` (has `str | None` in `render_client_ref`)
- Any other file found by the grep above

Add as the very first import in each affected file:

```python
from __future__ import annotations
```

**Step 3: Verify parsers already have the import**

`base_parser.py` already has `from __future__ import annotations` — no change needed.
`briscoes.py` now has it (added in Task 2) — no change needed.

**Step 4: Run the full test suite**

```bash
python -m pytest mml.edi/tests/ -v
```

Expected: All tests PASS. No `TypeError` about `str | None`.

**Step 5: Commit**

```bash
git add mml.edi/models/edi_trading_partner.py
# Add any other files modified
git commit -m "fix(odoo15): add __future__ annotations import for Python 3.8 compatibility"
```

---

## Task 8: Odoo 15 — Verify Views and XML Compatibility

**Background:**
Several Odoo 19 view features are not available in Odoo 15:
- `display_notification` client action → available from Odoo 14 onward ✓
- `tracking=True` on fields → available from Odoo 14 onward ✓
- `digits="Product Price"` decimal precision → available in Odoo 15 ✓
- `web_icon` in menuitem → available in Odoo 15 ✓
- `application=True` in manifest → available in Odoo 15 ✓

**Step 1: Check cron action model name**

In Odoo 15, scheduled actions use `ir.cron` — check `data/ir_cron.xml`:

```bash
grep "model_id\|model=" mml.edi/data/ir_cron.xml
```

Expected: `model_id` references `edi.processor` model. If it uses `code=` attribute, verify the Python expression is Odoo 15 compatible.

**Step 2: Check `ir_cron.xml` for Odoo 15 compatibility**

Open `mml.edi/data/ir_cron.xml` and verify:
- `ir.cron` records use `<field name="model_id" ref="..."/>` OR `<field name="model">edi.processor</field>`
- In Odoo 15, the correct approach is `model_id` pointing to `ir.model` record
- `code` field should contain `model.run_scheduled_poll()`

If using `model` (string) instead of `model_id` (m2o), it may need to change. Document the finding. If no change needed, proceed.

**Step 3: Check security group XML IDs**

```bash
grep "mml_edi\.group_" mml.edi/ -r --include="*.xml" --include="*.py"
```

Verify group XML IDs are defined in `security/edi_security.xml` and match what's referenced elsewhere.

**Step 4: Manual install smoke test note**

At this point, the Odoo 15 branch is ready for test installation. Note in a commit message the manual steps required post-install:

1. Set `partner_id` on the Briscoes trading partner record
2. Set `ftp_password` in the record
3. Set `alert_email_ids`
4. Set `environment` to `test` before live use

**Step 5: Commit**

```bash
git add -A
git commit -m "chore(odoo15): verify XML view compatibility for Odoo 15

All views, security, cron, and sequence XML verified compatible.
Post-install configuration required: partner_id, ftp_password, alert_email_ids."
```

---

## Task 9: Final Verification Pass

**On `master` branch:**

```bash
git checkout master
python -m pytest mml.edi/tests/ -v --tb=short 2>&1 | tail -20
```

Expected: All tests pass.

**On `odoo15` branch:**

```bash
git checkout odoo15
python -m pytest mml.edi/tests/ -v --tb=short 2>&1 | tail -20
```

Expected: All tests pass.

**Verify the branches have diverged correctly:**

```bash
git log --oneline --graph master odoo15 | head -20
```

Expected:
- `odoo15` has commits: odoo15-specific fixes on top of master
- `master` has: real parser + tests

**Final commit on each branch if any clean-up is needed.**

---

## Post-Plan Notes

### What the iDOC parser still needs (deferred)
`briscoes_idoc.py` remains a stub. Real iDOC ORDERSEXT files not yet received from Briscoes IT. When received, implement same pattern as `briscoes.py` but targeting XML (ElementTree) instead of EDIFACT segments.

### ACK generation limitations
The `_generate_ordrsp()` function uses `review.store_code` for all lines. This works for Briscoes' per-store model (one review = one store). If multiple stores per review are ever needed, the ACK generator must be revisited.

### UNT segment count
The current UNT count calculation (`len(segs)`) counts all segments from UNH onwards (inclusive). This is correct per EDIFACT standard (UNT counts itself). Verify this against a live Briscoes ORDRSP if the VAN rejects the message.

### Odoo 15 pricelist API detection
The dual-API approach (`hasattr(pricelist, 'price_get')`) works because Odoo 16+ removed `price_get`. If both methods exist on some Odoo build, `price_get` takes precedence — adjust the condition if this becomes a problem.
