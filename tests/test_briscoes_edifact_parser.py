# mml.edi/tests/test_briscoes_edifact_parser.py
"""
Tests for real EDIFACT D96A parser against actual Briscoes sample files.
These are pure Python unit tests — no Odoo env required.

Run with:
    cd E:/ClaudeCode/projects/mml.odoo.apps/briscoes.edi
    python -m pytest mml.edi/tests/test_briscoes_edifact_parser.py -v
"""
import pytest
from unittest.mock import MagicMock
from pathlib import Path

from mml_edi.parsers.briscoes import BriscoesParser
from mml_edi.parsers.base_parser import ParsedOrder, ParsedOrderLine

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
        assert len(store_1005.lines) == 2

    def test_store_1007_has_one_line(self):
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1007 = next(o for o in results if o.store_code == "1007")
        assert len(store_1007.lines) == 1

    def test_line_barcode_extracted(self):
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        barcodes = {l.product_code for l in store_1005.lines}
        assert "9414844375629" in barcodes

    def test_line_buyer_article_no_extracted(self):
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        line = next(l for l in store_1005.lines if l.product_code == "9414844375629")
        assert line.buyer_article_no == "375629"

    def test_line_store_qty_used_not_total(self):
        """QTY+11 (per-store qty) used, not QTY+21 (total across stores)."""
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        line = next(l for l in store_1005.lines if l.product_code == "9414844375629")
        assert line.quantity == 10.0  # QTY+11:10.000

    def test_line_price_extracted(self):
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        line = next(l for l in store_1005.lines if l.product_code == "9414844375629")
        assert line.unit_price == 5.50

    def test_delivery_date_extracted(self):
        from datetime import date
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        assert store_1005.requested_delivery_date == date(2011, 12, 16)

    def test_carton_qty_extracted(self):
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        line = next(l for l in store_1005.lines if l.product_code == "9414844375629")
        assert line.carton_qty == 1.0

    def test_raw_data_set(self):
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            assert order.raw_data is not None and len(order.raw_data) > 0

    def test_content_hash_computable(self):
        raw = _load("briscoes_orders_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            h = order.content_hash()
            assert len(h) == 64


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
        """Line 00090 (action=1, add) should appear."""
        raw = _load("briscoes_ordchg_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        all_barcodes = {l.product_code for o in results for l in o.lines}
        assert "9414844375674" in all_barcodes

    def test_does_not_crash(self):
        raw = _load("briscoes_ordchg_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        assert isinstance(results, list)


class TestCartonQty:
    def test_carton_qty_none_when_absent(self):
        """Lines in ORDCHG may lack QTY+52 — must not raise."""
        raw = _load("briscoes_ordchg_4500038166.edi")
        parser = BriscoesParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            for line in order.lines:
                assert line.carton_qty is None or isinstance(line.carton_qty, float)
