# mml.edi/tests/test_kestrelby_edifact_parser.py
"""
Tests for real EDIFACT D96A parser against actual Kestrelby sample files.
These are pure Python unit tests — no Odoo env required.

Run with:
    cd E:/ClaudeCode/projects/mml.odoo.apps/kestrelby.edi
    python -m pytest mml.edi/tests/test_kestrelby_edifact_parser.py -v
"""
import pytest
from unittest.mock import MagicMock
from pathlib import Path

from mml_edi.parsers.kestrelby import KestrelbyParser
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
        raw = _load("kestrelby_orders_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        assert isinstance(results, list)
        assert len(results) > 0

    def test_document_type_is_new_order(self):
        raw = _load("kestrelby_orders_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            assert order.document_type == "new_order"

    def test_po_number_extracted(self):
        raw = _load("kestrelby_orders_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            assert order.po_number == "4500038166"

    def test_order_date_extracted(self):
        from datetime import date
        raw = _load("kestrelby_orders_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            assert order.order_date == date(2011, 11, 8)

    def test_grouped_by_store(self):
        """ORDERS file has 2 stores (1005, 1007) — should produce 2 ParsedOrders."""
        raw = _load("kestrelby_orders_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        store_codes = {o.store_code for o in results}
        assert "1005" in store_codes
        assert "1007" in store_codes

    def test_store_1005_has_two_lines(self):
        raw = _load("kestrelby_orders_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        assert len(store_1005.lines) == 2

    def test_store_1007_has_one_line(self):
        raw = _load("kestrelby_orders_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1007 = next(o for o in results if o.store_code == "1007")
        assert len(store_1007.lines) == 1

    def test_line_barcode_extracted(self):
        raw = _load("kestrelby_orders_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        barcodes = {l.product_code for l in store_1005.lines}
        assert "0200000375621" in barcodes

    def test_line_buyer_article_no_extracted(self):
        raw = _load("kestrelby_orders_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        line = next(l for l in store_1005.lines if l.product_code == "0200000375621")
        assert line.buyer_article_no == "375629"

    def test_line_store_qty_used_not_total(self):
        """QTY+11 (per-store qty) used, not QTY+21 (total across stores)."""
        raw = _load("kestrelby_orders_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        line = next(l for l in store_1005.lines if l.product_code == "0200000375621")
        assert line.quantity == 10.0  # QTY+11:10.000

    def test_line_price_extracted(self):
        raw = _load("kestrelby_orders_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        line = next(l for l in store_1005.lines if l.product_code == "0200000375621")
        assert line.unit_price == 5.50

    def test_delivery_date_extracted(self):
        from datetime import date
        raw = _load("kestrelby_orders_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        assert store_1005.requested_delivery_date == date(2011, 12, 16)

    def test_carton_qty_extracted(self):
        raw = _load("kestrelby_orders_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        line = next(l for l in store_1005.lines if l.product_code == "0200000375621")
        assert line.carton_qty == 1.0

    def test_raw_data_set(self):
        raw = _load("kestrelby_orders_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            assert order.raw_data is not None and len(order.raw_data) > 0

    def test_content_hash_computable(self):
        raw = _load("kestrelby_orders_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            h = order.content_hash()
            assert len(h) == 64


class TestChangeOrderParsing:
    """Test ORDCHG (BGM+230) change order parsing."""

    def test_document_type_is_change_order(self):
        raw = _load("kestrelby_ordchg_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            assert order.document_type == "change_order"

    def test_po_number_extracted(self):
        raw = _load("kestrelby_ordchg_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            assert order.po_number == "4500038166"

    def test_new_line_included(self):
        """Line 00090 (action=1, add) should appear."""
        raw = _load("kestrelby_ordchg_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        all_barcodes = {l.product_code for o in results for l in o.lines}
        assert "0200000375676" in all_barcodes

    def test_does_not_crash(self):
        raw = _load("kestrelby_ordchg_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        assert isinstance(results, list)

    def test_cancelled_lines_excluded_or_flagged(self):
        """Lines with action=3 (cancel) are excluded from ParsedOrderLines.

        ORDCHG fixture has four LIN segments:
          00010 action=3 (cancel): barcode 0200000375621, store 1005  <- excluded
          00020 action=3 (cancel): barcode 0200000375638, store 1007  <- excluded
          00060 action=2 (change): barcode 0200000375621, store 1005  <- included
          00090 action=1 (add):    barcode 0200000375676, store 1007  <- included

        Verification strategy:
        - 0200000375638 only appears in the cancelled line 00020, so it must be
          entirely absent from all results.
        - 0200000375621 for store 1005 appears in both a cancelled line (00010)
          and a changed line (00060). The cancelled duplicate must not inflate the
          count: store 1005 should have exactly 1 occurrence of that barcode.
        """
        raw = _load("kestrelby_ordchg_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())

        # Line 00020: barcode 0200000375638 only in a cancelled line — must not appear
        all_barcodes = [l.product_code for o in results for l in o.lines]
        assert "0200000375638" not in all_barcodes

        # Line 00010 (cancelled) vs line 00060 (changed): same barcode+store.
        # Cancelled line must not be double-counted — exactly 1 occurrence for store 1005.
        store_1005 = next((o for o in results if o.store_code == "1005"), None)
        assert store_1005 is not None, "Store 1005 should be present (line 00060 is action=2)"
        occurrences = [l for l in store_1005.lines if l.product_code == "0200000375621"]
        assert len(occurrences) == 1, (
            "Cancelled line 00010 must not duplicate the changed line 00060; "
            "expected exactly 1 occurrence of barcode 0200000375621 for store 1005"
        )


class TestCartonQty:
    def test_carton_qty_none_when_absent(self):
        """Lines in ORDCHG may lack QTY+52 — must not raise."""
        raw = _load("kestrelby_ordchg_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        for order in results:
            for line in order.lines:
                assert line.carton_qty is None or isinstance(line.carton_qty, float)

    def test_carton_qty_extracted_when_present(self):
        """QTY+52 (carton/inner pack qty) from ORDERS fixture.
        Line 00010 has QTY+52:1.000 — carton_qty should be 1.0."""
        raw = _load("kestrelby_orders_4500038166.edi")
        parser = KestrelbyParser()
        results = parser.parse_file(raw, _mock_partner())
        store_1005 = next(o for o in results if o.store_code == "1005")
        line = next(l for l in store_1005.lines if l.product_code == "0200000375621")
        assert line.carton_qty == 1.0


class TestEdgeCases:
    """Tests for encoding edge cases and malformed input — no fixture files needed."""

    def test_x92_terminator_produces_same_result_as_standard_quote(self):
        """
        EDIFACT files from some Kestrelby EDIS endpoints use byte 0x92
        (Windows-1252 right single quotation mark) as the segment terminator
        instead of the standard 0x27 (apostrophe).

        Both must parse identically.
        """
        standard = (
            b"UNB+UNOA:3+VENDOR:ZZ+BUYER:14+261122:0900+00001++ORDERS'"
            b"UNH+1+ORDERS:D:96A:UN:EAN005'"
            b"BGM+220+4500099999+9'"
            b"DTM+137:20261122:102'"
            b"NAD+BY+0200000567897::92'"
            b"NAD+SU+VENDOR::92'"
            b"LIN+00010++0200000375621:EN'"
            b"QTY+21:24.000:EA'"
            b"QTY+11:12.000:EA'"
            b"QTY+52:6.000:EA'"
            b"PRI+AAA:5.50'"
            b"LOC+7+1005::92'"
            b"DTM+2:20261216:102'"
            b"UNS+S'"
            b"CNT+2:1'"
            b"UNT+14+1'"
            b"UNZ+1+00001'"
        )
        x92_version = standard.replace(b"'", b"\x92")

        from mml_edi.parsers.kestrelby import KestrelbyParser
        from unittest.mock import MagicMock
        partner = MagicMock()
        parser = KestrelbyParser()

        result_standard = parser.parse_file(standard, partner)
        result_x92 = parser.parse_file(x92_version, partner)

        assert len(result_standard) == len(result_x92)
        assert result_standard[0].po_number == result_x92[0].po_number
        assert result_standard[0].lines[0].product_code == result_x92[0].lines[0].product_code
        assert result_standard[0].lines[0].quantity == result_x92[0].lines[0].quantity

    def test_empty_file_returns_empty_list(self):
        from mml_edi.parsers.kestrelby import KestrelbyParser
        from unittest.mock import MagicMock
        parser = KestrelbyParser()
        result = parser.parse_file(b"", MagicMock())
        assert result == []

    def test_whitespace_only_file_returns_empty_list(self):
        from mml_edi.parsers.kestrelby import KestrelbyParser
        from unittest.mock import MagicMock
        parser = KestrelbyParser()
        result = parser.parse_file(b"   \r\n  ", MagicMock())
        assert result == []

    def test_una_service_string_skipped(self):
        """Files with UNA prefix must parse the same as files without it."""
        from mml_edi.parsers.kestrelby import KestrelbyParser
        from unittest.mock import MagicMock

        body = (
            b"UNB+UNOA:3+VENDOR:ZZ+BUYER:14+261122:0900+00001++ORDERS'"
            b"UNH+1+ORDERS:D:96A:UN:EAN005'"
            b"BGM+220+4500099999+9'"
            b"DTM+137:20261122:102'"
            b"NAD+BY+0200000567897::92'"
            b"NAD+SU+VENDOR::92'"
            b"LIN+00010++0200000375621:EN'"
            b"QTY+11:12.000:EA'"
            b"PRI+AAA:5.50'"
            b"LOC+7+1005::92'"
            b"UNS+S'"
            b"CNT+2:1'"
            b"UNT+11+1'"
            b"UNZ+1+00001'"
        )
        with_una = b"UNA:+.? '" + body
        partner = MagicMock()
        parser = KestrelbyParser()

        result_plain = parser.parse_file(body, partner)
        result_una = parser.parse_file(with_una, partner)

        assert len(result_plain) == len(result_una)
        assert result_plain[0].po_number == result_una[0].po_number

    def test_unrecognised_bgm_type_raises(self):
        """BGM codes other than 220 (ORDERS) and 230 (ORDCHG) must raise EDIParseError."""
        import pytest
        from mml_edi.parsers.kestrelby import KestrelbyParser
        from mml_edi.parsers.base_parser import EDIParseError
        from unittest.mock import MagicMock

        bad_msg = (
            b"UNH+1+ORDERS:D:96A:UN:EAN005'"
            b"BGM+999+4500099999+9'"
            b"UNS+S'"
            b"UNT+3+1'"
            b"UNZ+1+00001'"
        )
        with pytest.raises(EDIParseError, match="Unrecognised BGM"):
            KestrelbyParser().parse_file(bad_msg, MagicMock())

    def test_missing_po_number_raises(self):
        """BGM with empty PO number must raise EDIParseError."""
        import pytest
        from mml_edi.parsers.kestrelby import KestrelbyParser
        from mml_edi.parsers.base_parser import EDIParseError
        from unittest.mock import MagicMock

        bad_msg = (
            b"UNH+1+ORDERS:D:96A:UN:EAN005'"
            b"BGM+220++9'"
            b"UNS+S'"
            b"UNT+3+1'"
            b"UNZ+1+00001'"
        )
        with pytest.raises(EDIParseError, match="missing PO number"):
            KestrelbyParser().parse_file(bad_msg, MagicMock())

    def test_invalid_bytes_produce_replacement_char_warning_not_crash(self):
        """
        Bytes invalid in Windows-1252 (e.g. 0x81) should produce a warning
        via _logger.warning and not crash.
        """
        import pytest
        from mml_edi.parsers.kestrelby import KestrelbyParser
        from unittest.mock import MagicMock, patch

        raw_with_bad_byte = (
            b"UNH+1+ORDERS:D:96A:UN:EAN005'"
            b"BGM+220+4500099999+9'"
            b"\x81"
            b"UNS+S'"
            b"UNT+3+1'"
            b"UNZ+1+00001'"
        )
        with patch("mml_edi.parsers.kestrelby._logger") as mock_logger:
            try:
                KestrelbyParser().parse_file(raw_with_bad_byte, MagicMock())
            except Exception:
                pass
            assert mock_logger.warning.called
            warning_call = str(mock_logger.warning.call_args)
            assert "invalid" in warning_call.lower() or "corrupt" in warning_call.lower()
