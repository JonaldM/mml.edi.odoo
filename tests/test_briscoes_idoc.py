# mml.edi/tests/test_briscoes_idoc.py
"""
Tests for the SAP iDOC XML parser (BriscoesIDOCParser) against real Briscoes
sample files. Pure-Python unit tests — no Odoo env required.

Run with:
    cd <module dir> && python -m pytest tests/test_briscoes_idoc.py -v
"""
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mml_edi.parsers.briscoes_idoc import BriscoesIDOCParser
from mml_edi.parsers.base_parser import EDIParseError, ParsedOrder, ParsedOrderLine

FIXTURES = Path(__file__).parent / "fixtures"

SINGLE = "briscoes_idoc_orders_single_7010168258.edi"   # ZNR single-store, 6 lines
MULTI = "briscoes_idoc_orders_multi_4500176574.edi"     # ZNB bulk multi-store, 173 lines


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _partner(product_match_field="barcode", order_split_mode="per_store"):
    p = MagicMock()
    p.product_match_field = product_match_field
    p.order_split_mode = order_split_mode
    return p


# ── parse_file: single-store ZNR ─────────────────────────────────────────────

class TestSingleStoreParsing:

    def setup_method(self):
        self.orders = BriscoesIDOCParser().parse_file(_load(SINGLE), _partner())

    def test_single_store_one_order(self):
        assert len(self.orders) == 1
        assert isinstance(self.orders[0], ParsedOrder)

    def test_header_fields(self):
        o = self.orders[0]
        assert o.po_number == "7010168258"
        assert o.store_code == "1050"            # from header E1EDKA1[WE].LIFNR
        assert o.document_type == "new_order"
        assert o.order_date == date(2026, 5, 18)         # IDDAT 012
        assert o.requested_delivery_date == date(2026, 5, 25)  # line EDATU

    def test_line_count_and_numbers(self):
        o = self.orders[0]
        assert len(o.lines) == 6
        assert [l.line_number for l in o.lines] == [10, 20, 30, 40, 50, 60]

    def test_quantities_are_each_not_cartons(self):
        # BMNG2 (each), NOT MENGE (cartons 2/4/2/1/1/1) — the key correctness point.
        o = self.orders[0]
        assert [l.quantity for l in o.lines] == [6.0, 12.0, 8.0, 4.0, 3.0, 3.0]
        assert [l.carton_qty for l in o.lines] == [2.0, 4.0, 2.0, 1.0, 1.0, 1.0]

    def test_prices(self):
        o = self.orders[0]
        assert [l.unit_price for l in o.lines] == [9.29, 19.03, 15.30, 22.00, 6.08, 6.08]

    def test_netwr_consistency(self):
        # quantity (each) x unit_price == source NETWR for each line
        o = self.orders[0]
        expected = [55.74, 228.36, 122.40, 88.00, 18.24, 18.24]
        for line, netwr in zip(o.lines, expected):
            assert round(line.quantity * line.unit_price, 2) == netwr

    def test_product_codes(self):
        line0 = self.orders[0].lines[0]
        # match key = MML internal code (QUALF 002 -> default_code), NOT the
        # carton/shipper GTIN-14 (QUALF 003 = 19419416111909, the case barcode)
        assert line0.product_code == "HESPG"
        assert line0.vendor_code == "HESPG"
        assert line0.buyer_article_no == "1023632"        # Briscoes art (QUALF 001, zero-stripped)
        assert line0.description == "Soap Pump VOL Heron Gry"
        assert line0.uom == "EA"

    def test_uom_each(self):
        assert all(l.uom == "EA" for l in self.orders[0].lines)


# ── parse_file: multi-store ZNB ──────────────────────────────────────────────

class TestMultiStoreParsing:

    def setup_method(self):
        self.orders = BriscoesIDOCParser().parse_file(_load(MULTI), _partner())

    def test_explodes_into_multiple_store_orders(self):
        assert len(self.orders) > 1

    def test_po_number_on_every_store_order(self):
        assert all(o.po_number == "4500176574" for o in self.orders)
        assert all(o.document_type == "new_order" for o in self.orders)

    def test_all_173_lines_preserved(self):
        assert sum(len(o.lines) for o in self.orders) == 173

    def test_each_order_has_a_store_code(self):
        assert all(o.store_code for o in self.orders)

    def test_store_codes_unique_per_order(self):
        codes = [o.store_code for o in self.orders]
        assert len(codes) == len(set(codes))   # one ParsedOrder per store

    def test_lines_have_each_quantities_and_posex(self):
        for o in self.orders:
            for l in o.lines:
                assert l.quantity > 0
                assert l.line_number > 0
                assert l.product_code


# ── parse_file: edge cases ───────────────────────────────────────────────────

class TestParseEdgeCases:

    def test_ordrsp_is_skipped(self):
        ordrsp = (
            '<?xml version="1.0"?><ORDERSEXT><IDOC BEGIN="1">'
            "<EDI_DC40 SEGMENT=\"1\"><MESTYP>ORDRSP</MESTYP></EDI_DC40>"
            "<E1EDK01 SEGMENT=\"1\"><BELNR>123</BELNR></E1EDK01>"
            "</IDOC></ORDERSEXT>"
        ).encode()
        assert BriscoesIDOCParser().parse_file(ordrsp, _partner()) == []

    def test_invalid_xml_raises(self):
        with pytest.raises(EDIParseError):
            BriscoesIDOCParser().parse_file(b"not xml <<<", _partner())

    def test_missing_po_number_raises(self):
        bad = (
            '<?xml version="1.0"?><ORDERSEXT><IDOC BEGIN="1">'
            "<EDI_DC40 SEGMENT=\"1\"><MESTYP>ORDERS</MESTYP></EDI_DC40>"
            "<E1EDK01 SEGMENT=\"1\"></E1EDK01></IDOC></ORDERSEXT>"
        ).encode()
        with pytest.raises(EDIParseError):
            BriscoesIDOCParser().parse_file(bad, _partner())

    def test_change_order_detected_and_deleted_lines_dropped(self):
        # MESTYP ORDCHG; one normal line + one ACTION=003 (delete) line.
        chg = (
            '<?xml version="1.0"?><ORDERSEXT><IDOC BEGIN="1">'
            '<EDI_DC40 SEGMENT="1"><MESTYP>ORDCHG</MESTYP></EDI_DC40>'
            '<E1EDK01 SEGMENT="1"><BELNR>7010168258</BELNR></E1EDK01>'
            '<E1EDKA1 SEGMENT="1"><PARVW>WE</PARVW><LIFNR>1050</LIFNR></E1EDKA1>'
            '<E1EDP01 SEGMENT="1"><POSEX>00010</POSEX><ACTION>002</ACTION>'
            '<BMNG2>6.000</BMNG2><PMENE>EA</PMENE><VPREI>9.29</VPREI><WERKS>1050</WERKS>'
            '<E1EDP19 SEGMENT="1"><QUALF>003</QUALF><IDTNR>19419416111909</IDTNR></E1EDP19>'
            '</E1EDP01>'
            '<E1EDP01 SEGMENT="1"><POSEX>00020</POSEX><ACTION>003</ACTION>'
            '<BMNG2>4.000</BMNG2><PMENE>EA</PMENE><VPREI>1.00</VPREI><WERKS>1050</WERKS>'
            '</E1EDP01></IDOC></ORDERSEXT>'
        ).encode()
        orders = BriscoesIDOCParser().parse_file(chg, _partner())
        assert len(orders) == 1
        assert orders[0].document_type == "change_order"
        # The ACTION=003 line is dropped → only POSEX 10 remains.
        assert [l.line_number for l in orders[0].lines] == [10]

    def test_full_cancellation_yields_lineless_store_order(self):
        # ORDCHG where EVERY line is ACTION=003 (cancel the whole store): the
        # store is still returned with no lines so the engine's diff removes all
        # existing lines (full cancellation) rather than silently doing nothing.
        cancel = (
            '<?xml version="1.0"?><ORDERSEXT><IDOC BEGIN="1">'
            '<EDI_DC40 SEGMENT="1"><MESTYP>ORDCHG</MESTYP></EDI_DC40>'
            '<E1EDK01 SEGMENT="1"><BELNR>7010168258</BELNR></E1EDK01>'
            '<E1EDKA1 SEGMENT="1"><PARVW>WE</PARVW><LIFNR>1050</LIFNR></E1EDKA1>'
            '<E1EDP01 SEGMENT="1"><POSEX>00010</POSEX><ACTION>003</ACTION>'
            '<BMNG2>6.000</BMNG2><WERKS>1050</WERKS></E1EDP01>'
            '<E1EDP01 SEGMENT="1"><POSEX>00020</POSEX><ACTION>003</ACTION>'
            '<BMNG2>4.000</BMNG2><WERKS>1050</WERKS></E1EDP01>'
            '</IDOC></ORDERSEXT>'
        ).encode()
        orders = BriscoesIDOCParser().parse_file(cancel, _partner())
        assert len(orders) == 1
        assert orders[0].store_code == "1050"
        assert orders[0].document_type == "change_order"
        assert orders[0].lines == []


# ── generate_ack: ORDRSP ─────────────────────────────────────────────────────

class _MockLine:
    def __init__(self, posex, qty):
        self.edi_line_number = posex
        self.product_uom_qty = qty


class _MockSO:
    def __init__(self, lines):
        self.order_line = lines


def _review(state="approved", store="1050", confirmed=None):
    """Build a mock edi.order.review for the single-store PO."""
    raw = _load(SINGLE).decode("utf-8")
    r = MagicMock()
    r.edi_raw_data = raw
    r.store_code = store
    r.state = state
    r.customer_po_number = "7010168258"
    # default: full supply matching the ordered each-quantities
    cmap = confirmed if confirmed is not None else {10: 6, 20: 12, 30: 8, 40: 4, 50: 3, 60: 3}
    r.sale_order_id = _MockSO([_MockLine(k, v) for k, v in cmap.items()])
    return r


class TestGenerateAck:

    def test_returns_valid_ordrsp_xml(self):
        out = BriscoesIDOCParser().generate_ack(_review())
        assert isinstance(out, bytes)
        root = ET.fromstring(out)            # parses without error
        idoc = root.find("IDOC")
        assert idoc.find("EDI_DC40/MESTYP").text == "ORDRSP"
        assert idoc.find("EDI_DC40/DIRECT").text == "2"
        assert idoc.find("E1EDK01/BELNR").text == "7010168258"

    def test_response_reference_segment_added(self):
        root = ET.fromstring(BriscoesIDOCParser().generate_ack(_review()))
        quals = [k.find("QUALF").text for k in root.iter("E1EDK02")]
        assert "002" in quals      # response reference added

    def test_all_lines_echoed_with_posex_and_link(self):
        root = ET.fromstring(BriscoesIDOCParser().generate_ack(_review()))
        lines = root.findall("IDOC/E1EDP01")
        assert [l.find("POSEX").text for l in lines] == ["00010", "00020", "00030", "00040", "00050", "00060"]
        # Each response line links back to its PO line via E1EDP02/ZEILE = POSEX
        for l in lines:
            link = l.find("E1EDP02")
            assert link.find("ZEILE").text == l.find("POSEX").text
            assert link.find("BELNR").text == "7010168258"

    def test_full_supply_echoes_ordered_each_qty(self):
        root = ET.fromstring(BriscoesIDOCParser().generate_ack(_review()))
        bmng2 = [float(l.find("BMNG2").text) for l in root.findall("IDOC/E1EDP01")]
        assert bmng2 == [6.0, 12.0, 8.0, 4.0, 3.0, 3.0]
        # no rejection flags on a clean full-supply ACK
        assert root.find("IDOC/E1EDP01/ABGRU") is None

    def test_short_supply_flags_abgru_and_reduces_qty(self):
        # confirm only 3 of 6 on POSEX 10
        rv = _review(confirmed={10: 3, 20: 12, 30: 8, 40: 4, 50: 3, 60: 3})
        root = ET.fromstring(BriscoesIDOCParser().generate_ack(rv))
        line10 = root.findall("IDOC/E1EDP01")[0]
        assert float(line10.find("BMNG2").text) == 3.0
        assert line10.find("ABGRU") is not None

    def test_rejected_zeroes_qty_and_flags_all(self):
        root = ET.fromstring(BriscoesIDOCParser().generate_ack(_review(state="rejected")))
        for l in root.findall("IDOC/E1EDP01"):
            assert float(l.find("BMNG2").text) == 0.0
            assert l.find("ABGRU") is not None

    def test_missing_raw_data_raises(self):
        r = MagicMock()
        r.edi_raw_data = None
        with pytest.raises(EDIParseError):
            BriscoesIDOCParser().generate_ack(r)


# ── per-PO ACK: multi-store collapses to ONE ORDRSP ──────────────────────────

def _multi_review(state="approved"):
    """Mock review for the multi-store PO (no SO confirmations -> full supply)."""
    r = MagicMock()
    r.edi_raw_data = _load(MULTI).decode("utf-8")
    r.store_code = "1004"
    r.state = state
    r.customer_po_number = "4500176574"
    r.sale_order_id = _MockSO([])
    return r


class TestPerPoAck:

    def test_multistore_is_one_ordrsp_covering_all_stores(self):
        root = ET.fromstring(BriscoesIDOCParser().generate_ack(_multi_review()))
        # exactly one IDOC / ORDRSP for the whole PO
        assert len(root.findall("IDOC")) == 1
        lines = root.findall("IDOC/E1EDP01")
        assert len(lines) == 173                       # every store's line echoed
        werks = {l.find("WERKS").text for l in lines}
        assert len(werks) > 1                          # spans multiple stores
        assert root.find("IDOC/EDI_DC40/MESTYP").text == "ORDRSP"
        assert root.find("IDOC/E1EDK01/BELNR").text == "4500176574"

    def test_posex_unique_and_linked_across_stores(self):
        root = ET.fromstring(BriscoesIDOCParser().generate_ack(_multi_review()))
        lines = root.findall("IDOC/E1EDP01")
        posex = [l.find("POSEX").text for l in lines]
        assert len(posex) == len(set(posex))           # globally unique POSEX
        for l in lines:                                # each links back to PO line
            assert l.find("E1EDP02/ZEILE").text == l.find("POSEX").text


# ── round-trip: parse a generated ACK is recognised as a response ────────────

class TestRoundTrip:

    def test_generated_ack_is_skipped_if_reparsed(self):
        ack = BriscoesIDOCParser().generate_ack(_review())
        # An ORDRSP fed back into parse_file must be ignored (not re-imported).
        assert BriscoesIDOCParser().parse_file(ack, _partner()) == []
