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
