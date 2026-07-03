"""Pure-Python tests for the Animates ORDERS inbound parser (B2).

No Odoo env. Run: pytest tests/test_animates_parser.py -q
"""
from datetime import date
from pathlib import Path

from mml_edi.parsers.animates import AnimatesParser

FIXTURES = Path(__file__).parent / "fixtures"


def _orders():
    raw = (FIXTURES / "animates_orders_PO169603.edi").read_bytes()
    return AnimatesParser().parse_file(raw, trading_partner=None)


def test_single_order_parsed():
    orders = _orders()
    assert len(orders) == 1
    o = orders[0]
    assert o.po_number == "PO169603"
    assert o.document_type == "new_order"
    assert o.order_date == date(2020, 9, 16)        # DTM+137
    assert o.requested_delivery_date == date(2020, 9, 18)  # DTM+2
    assert o.store_code == "12345"                  # NAD+ST
    assert o.delivery_address_code == "12345"


def test_line_fields():
    o = _orders()[0]
    assert len(o.lines) == 1
    ln = o.lines[0]
    assert ln.line_number == 1
    assert ln.buyer_article_no == "122134"   # ISC  (PIA+5+..:IN)
    assert ln.product_code == "5101000"      # MML  (PIA+1+..:SA)
    assert ln.vendor_code == "5101000"
    assert ln.quantity == 2.0                # QTY+21
    assert ln.uom == "EA"
    assert ln.unit_price == 132.44           # PRI+AAA
    assert ln.carton_qty == 1.0              # QTY+59
    assert "Product Description" in ln.description


def test_str_input_also_accepted():
    raw = (FIXTURES / "animates_orders_PO169603.edi").read_text(encoding="iso-8859-1")
    orders = AnimatesParser().parse_file(raw, trading_partner=None)
    assert orders[0].po_number == "PO169603"


def test_no_unh_raises():
    from mml_edi.parsers.base_parser import EDIParseError
    import pytest
    with pytest.raises(EDIParseError):
        AnimatesParser().parse_file(b"GARBAGE+not+edifact'", trading_partner=None)


# --- C5 / AN-05: cancellation (BGM 1225=1, Testing Scenario Handbook 3B) ---

def _cancel_order():
    raw = (FIXTURES / "animates_orders_cancel_PO0319333.edi").read_bytes()
    orders = AnimatesParser().parse_file(raw, trading_partner=None)
    assert len(orders) == 1
    return orders[0]


def test_cancellation_document_type():
    o = _cancel_order()
    assert o.document_type == "cancellation"
    assert o.po_number == "PO0319333"


def test_cancellation_has_no_lines():
    """MIG scenario 3B: 'No line items included (LIN SG 28 is omitted)'."""
    o = _cancel_order()
    assert o.lines == []


def test_change_order_is_still_change_order_not_cancellation():
    """BGM 1225=4/5 must not be misclassified as a cancellation."""
    raw = (FIXTURES / "animates_orders_PO169603.edi").read_text(encoding="iso-8859-1")
    changed = raw.replace("BGM+220+PO169603+9'", "BGM+220+PO169603+5'")
    o = AnimatesParser().parse_file(changed, trading_partner=None)[0]
    assert o.document_type == "change_order"
