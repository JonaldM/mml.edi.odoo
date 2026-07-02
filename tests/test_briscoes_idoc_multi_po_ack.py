# mml.edi/tests/test_briscoes_idoc_multi_po_ack.py
"""
Multi-IDOC (multi-PO) ORDRSP isolation tests for BriscoesIDOCParser.

``review.edi_raw_data`` stores the WHOLE inbound file. When EDIS batches two
POs into one file, the ACK for a review must echo ONLY the IDOC whose
E1EDK01/BELNR matches that review's PO — never the other PO's segments (which
would carry the wrong confirmations / force-rejections). Single-PO files (the
common case) must be byte-for-byte unchanged.

Pure-Python unit tests — no Odoo env required.

Run with:
    cd <module dir> && python -m pytest tests/test_briscoes_idoc_multi_po_ack.py -v
"""
import copy
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mml_edi.parsers import briscoes_idoc
from mml_edi.parsers.briscoes_idoc import BriscoesIDOCParser
from mml_edi.parsers.base_parser import EDIParseError

FIXTURES = Path(__file__).parent / "fixtures"

SINGLE = "briscoes_idoc_orders_single_7010168258.edi"   # ZNR single-store, 6 lines

PO_A = "7010168258"
PO_B = "9990000001"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _two_po_file() -> str:
    """Build a two-PO inbound file: the real single-store fixture IDOC (PO A)
    plus a deep copy of it re-numbered as PO B — mimicking an EDIS batch."""
    root = ET.fromstring(_load(SINGLE).decode("utf-8"))
    idoc_a = root.find("IDOC")
    idoc_b = copy.deepcopy(idoc_a)
    idoc_b.find("E1EDK01/BELNR").text = PO_B
    idoc_b.find("EDI_DC40/DOCNUM").text = "0000000999999999"
    combined = ET.Element("ORDERSEXT")
    combined.append(idoc_a)
    combined.append(idoc_b)
    return '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(
        combined, encoding="unicode"
    )


class _MockLine:
    def __init__(self, posex, qty):
        self.edi_line_number = posex
        self.product_uom_qty = qty


class _MockSO:
    def __init__(self, lines):
        self.order_line = lines


def _review(po_number, raw, state="approved", confirmed=None):
    """Mock edi.order.review holding the (whole-file) raw data."""
    r = MagicMock()
    r.edi_raw_data = raw
    r.store_code = "1050"
    r.state = state
    r.customer_po_number = po_number
    cmap = confirmed if confirmed is not None else {10: 6, 20: 12, 30: 8, 40: 4, 50: 3, 60: 3}
    r.sale_order_id = _MockSO([_MockLine(k, v) for k, v in cmap.items()])
    return r


# ── two-PO file: each ACK carries ONLY its own PO's IDOC ─────────────────────

class TestMultiPoAckIsolation:

    def setup_method(self):
        self.raw = _two_po_file()

    def test_ack_for_po_a_contains_only_po_a(self):
        out = BriscoesIDOCParser().generate_ack(_review(PO_A, self.raw)).decode("utf-8")
        root = ET.fromstring(out)
        idocs = root.findall("IDOC")
        assert len(idocs) == 1, "ACK must echo exactly ONE IDOC (its own PO)"
        assert idocs[0].find("E1EDK01/BELNR").text == PO_A
        assert PO_B not in out, "Other PO's segments leaked into this ACK"

    def test_ack_for_po_b_contains_only_po_b(self):
        out = BriscoesIDOCParser().generate_ack(_review(PO_B, self.raw)).decode("utf-8")
        root = ET.fromstring(out)
        idocs = root.findall("IDOC")
        assert len(idocs) == 1
        assert idocs[0].find("E1EDK01/BELNR").text == PO_B
        assert PO_A not in out, "Other PO's segments leaked into this ACK"

    def test_line_counts_match_own_po_only(self):
        # The fixture IDOC has 6 lines; before the fix the ACK carried 12
        # (both IDOCs echoed) with the second PO's lines force-rejected.
        for po in (PO_A, PO_B):
            root = ET.fromstring(BriscoesIDOCParser().generate_ack(_review(po, self.raw)))
            lines = root.findall("IDOC/E1EDP01")
            assert len(lines) == 6, "Expected 6 lines for PO %s, got %d" % (po, len(lines))

    def test_own_po_lines_confirmed_not_force_rejected(self):
        # Full-supply confirmations for PO A must yield a clean ACK — no ABGRU
        # anywhere (before the fix, PO B's echoed lines carried ABGRU).
        root = ET.fromstring(BriscoesIDOCParser().generate_ack(_review(PO_A, self.raw)))
        assert root.find("IDOC/E1EDP01/ABGRU") is None
        bmng2 = [float(l.find("BMNG2").text) for l in root.findall("IDOC/E1EDP01")]
        assert bmng2 == [6.0, 12.0, 8.0, 4.0, 3.0, 3.0]

    def test_e1edp02_links_reference_own_po(self):
        root = ET.fromstring(BriscoesIDOCParser().generate_ack(_review(PO_B, self.raw)))
        for l in root.findall("IDOC/E1EDP01"):
            assert l.find("E1EDP02/BELNR").text == PO_B

    def test_multi_po_file_with_unmatched_po_fails_closed(self):
        # A multi-PO file where NO IDOC matches the review's PO is a data
        # integrity fault: echoing everything would corrupt other POs' ACKs,
        # so generation must abort (fail-closed) rather than guess.
        with pytest.raises(EDIParseError):
            BriscoesIDOCParser().generate_ack(_review("4500000000", self.raw))


# ── single-PO file: output byte-for-byte unchanged ───────────────────────────

class _FrozenDatetime(datetime):
    """datetime whose now() is pinned so ACK output is deterministic."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 2, 12, 34, 56)


class TestSinglePoUnchanged:

    def test_single_po_output_identical_to_unfiltered_echo(self, monkeypatch):
        # Byte-for-byte regression proof: for a single-PO file the ACK must be
        # EXACTLY what the pre-filter implementation produced — i.e. every IDOC
        # in the file echoed via _build_ordrsp with the same confirmations.
        monkeypatch.setattr(briscoes_idoc, "datetime", _FrozenDatetime)
        parser = BriscoesIDOCParser()
        raw = _load(SINGLE).decode("utf-8")
        review = _review(PO_A, raw)

        actual = parser.generate_ack(review)

        # Reference: the original (unfiltered) generate_ack behaviour.
        root = ET.fromstring(raw)
        confirmed, has_review_data = parser._gather_confirmations(review)
        now = _FrozenDatetime.now()
        serial = now.strftime("%Y%m%d%H%M%S")
        out = ['<?xml version="1.0" encoding="UTF-8"?>', "<ORDERSEXT>"]
        for idoc in root.findall("IDOC"):
            out.extend(parser._build_ordrsp(
                idoc, PO_A, confirmed, now, serial, has_review_data,
            ))
        out.append("</ORDERSEXT>")
        expected = ("\n".join(out) + "\n").encode("utf-8")

        assert actual == expected

    def test_single_po_all_lines_still_echoed(self):
        root = ET.fromstring(BriscoesIDOCParser().generate_ack(_review(PO_A, _load(SINGLE).decode("utf-8"))))
        assert len(root.findall("IDOC")) == 1
        assert len(root.findall("IDOC/E1EDP01")) == 6

    def test_single_po_mismatched_po_ref_still_echoes(self):
        # A single-PO file is deliberately NEVER filtered (byte-for-byte
        # compatibility), even if the review's PO string differs from BELNR
        # (legacy/mock paths rely on the echo).
        root = ET.fromstring(BriscoesIDOCParser().generate_ack(
            _review("0000000000", _load(SINGLE).decode("utf-8"))
        ))
        assert len(root.findall("IDOC")) == 1
        assert root.find("IDOC/E1EDK01/BELNR").text == PO_A
