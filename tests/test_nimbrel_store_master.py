"""Pure-Python tests for the embedded Nimbrel store master table (Wave2-F,
gate review AN-13/OPS-5).

Covers: table integrity against the source xlsx row/dedup count, ref format
compatibility with what parsers/nimbrel.py extracts from NAD+ST, and the
clinic/retail collision-collapse documented in
wizards/nimbrel_store_master_data.py.

No Odoo. Run: python -m pytest tests/test_nimbrel_store_master.py -q
"""
import re

import pytest

from mml_edi.wizards.nimbrel_store_master_data import (
    _NIMBREL_STORES,
    get_nimbrel_stores,
)
from mml_edi.parsers import nimbrel_edifact as edifact


# Source xlsx has 66 site rows (7 regions) across which 10 store codes are
# deliberately duplicated (vet clinic co-located inside a retail store shares
# that store's code — see the module docstring). 66 - 10 = 56 unique rows.
_EXPECTED_SOURCE_ROW_COUNT = 66
_EXPECTED_DUPLICATE_CODE_COUNT = 10
_EXPECTED_UNIQUE_COUNT = _EXPECTED_SOURCE_ROW_COUNT - _EXPECTED_DUPLICATE_CODE_COUNT

# The 10 codes known to be shared retail/clinic pairs in the source file —
# regenerate this set (and _EXPECTED_DUPLICATE_CODE_COUNT above) only after
# re-running the xlsx extraction against a newer master file.
_KNOWN_COLLAPSED_CODES = {"03", "05", "08", "09", "11", "13", "18", "25", "29", "33"}


# --- table integrity ---------------------------------------------------

def test_get_nimbrel_stores_returns_expected_unique_count():
    stores = get_nimbrel_stores()
    assert len(stores) == _EXPECTED_UNIQUE_COUNT


def test_get_nimbrel_stores_returns_tuple_of_tuples_no_io():
    """Runtime contract: no openpyxl / xlsx access, pure in-memory data."""
    stores = get_nimbrel_stores()
    assert isinstance(stores, tuple)
    for row in stores:
        assert isinstance(row, tuple)
        assert len(row) == 3


def test_no_duplicate_refs_in_embedded_table():
    """The whole point of collapsing clinic/retail pairs: the embedded table
    itself must have zero duplicate store codes, or _resolve_delivery_partner's
    ref lookup (limit=1) would silently misroute orders for one of them."""
    codes = [code for code, _name, _region in _NIMBREL_STORES]
    assert len(codes) == len(set(codes)), "duplicate store_code in embedded table"


def test_known_collapsed_codes_present_exactly_once():
    codes = [code for code, _name, _region in _NIMBREL_STORES]
    for code in _KNOWN_COLLAPSED_CODES:
        assert codes.count(code) == 1


def test_no_blank_code_or_name():
    for code, name, _region in _NIMBREL_STORES:
        assert code and code.strip() == code, "code must be non-empty and trim-clean: %r" % (code,)
        assert name and name.strip() == name, "name must be non-empty and trim-clean: %r" % (name,)


def test_no_bom_or_stray_whitespace_in_names():
    """Source xlsx had a BOM (U+FEFF) in one cell (Hornby) — must not survive
    into seeded partner names."""
    for code, name, region in _NIMBREL_STORES:
        assert "﻿" not in name
        assert "﻿" not in region


# --- store code formats: 2-digit numeric + R-xx retail codes -----------

_NUMERIC_CODE_RE = re.compile(r"^\d{2}$")
_RETAIL_CODE_RE = re.compile(r"^R-\d+$")


def test_all_codes_match_known_formats():
    """Gate review AN-13: 2-digit codes and 'R-xx' retail codes coexist —
    assert the embedded table contains ONLY these two shapes (catches a
    transcription error introducing e.g. a 3-digit or lowercase code)."""
    for code, _name, _region in _NIMBREL_STORES:
        assert _NUMERIC_CODE_RE.match(code) or _RETAIL_CODE_RE.match(code), (
            "unexpected store code format: %r" % (code,)
        )


def test_numeric_codes_are_never_int_cast_leading_zero_preserved():
    """Silverdale's source cell is an Excel int (12) but codes like '02' MUST
    keep their leading zero — proves the table stores codes as strings, not
    zero-padding-losing ints."""
    numeric_codes = [c for c, _n, _r in _NIMBREL_STORES if _NUMERIC_CODE_RE.match(c)]
    assert "02" in numeric_codes
    assert all(isinstance(c, str) for c in numeric_codes)


def test_retail_codes_present():
    retail_codes = {code for code, _n, _r in _NIMBREL_STORES if code.startswith("R-")}
    assert retail_codes == {"R-59", "R-60"}


# --- ref format must match what the parser extracts from NAD+ST -------

def _nad_st_store_code(nad_line: str) -> str:
    """Mirror parsers/nimbrel.py's NAD+ST handling: seg.comp(1, 0), i.e.
    element index 1, component index 0 of the tokenized segment — verbatim,
    no case/format transform."""
    _una, segments = edifact.tokenize("UNA:+.? '\n" + nad_line + "'\n")
    seg = next(s for s in segments if s.tag == "NAD")
    assert seg.comp(0, 0) == "ST"
    return seg.comp(1, 0)


@pytest.mark.parametrize("code", ["08", "R-59", "02", "R-60", "58"])
def test_embedded_ref_format_matches_nad_st_extraction(code):
    """Build a real NAD+ST segment carrying each sample code and confirm the
    parser extracts it byte-for-byte identical to what's embedded here — if
    this ever mismatches (e.g. because of zero-padding or case folding
    somewhere), NAD ST matching in the parser would never resolve the seeded
    partner and every order for that store would fall back to
    unknown_store."""
    nad_line = "NAD+ST+%s::92++Nimbrel Test Store+1 Test Street+Testville++0000+NZ" % code
    extracted = _nad_st_store_code(nad_line)
    assert extracted == code

    codes_in_table = {c for c, _n, _r in _NIMBREL_STORES}
    assert code in codes_in_table


def test_all_embedded_codes_round_trip_through_nad_st_tokenizer():
    """Every code in the embedded table, not just the sample above."""
    for code, _name, _region in _NIMBREL_STORES:
        nad_line = "NAD+ST+%s::92++X+Y+Z++0000+NZ" % code
        assert _nad_st_store_code(nad_line) == code
