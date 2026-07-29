"""Pure-Python tests for the outbound Nimbrel D.01B INVOIC builder (B5).

No Odoo env. Run: pytest tests/test_nimbrel_invoic.py -q

The payload below carries EXACTLY the data in the verbatim MIG worked example
(docs/nimbrel/Nimbrel_INVOIC.pdf p64-65 == tests/fixtures/nimbrel_invoic_expected.edi):
line amounts/price as 4dp strings, summary amounts + tax rate as 2dp strings.
"""
from pathlib import Path

from mml_edi.parsers.nimbrel_edifact import (
    assert_equivalent,
    tokenize,
    validate_interchange,
)
from mml_edi.parsers.nimbrel_invoic import build_invoic

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name):
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def _fixture_payload():
    """The exact data encoded in nimbrel_invoic_expected.edi."""
    return {
        "date_yymmdd": "200918",
        "time_hhmm": "2130",
        "invoice_number": "INV566343",
        "message_function": "9",
        "invoice_date": "20200918",
        "ref_aak": "25488",
        "ref_cn": "9900857",
        "ref_on": "POR169603",
        "buyer": {
            "code": "NIMBREL",
            "name": "Nimbrel NZ Holding LTD",
            "street": "PO BOX 11959 Ellerslie",
            "city": "Auckland",
            "state": "",
            "postcode": "1051",
            "country": "NZ",
            "nzbn": "9429040432250",
        },
        "supplier": {
            "code": "V1058",
            "name": "M&M Pty Ltd",
            "street": "PO BOX 999",
            "city": "Richmond",
            "state": "VIC",
            "postcode": "3121",
            "country": "AU",
            "abn": "12345678901",
            "contact_name": "Ms M",
            "phone": "03 9077 0683",
            "email": "MM@mimo.com.au",
        },
        "ship_to": {
            "code": "12345",
            "name": "Nimbrel Invercargill",
            "street": "186 Tay Street",
            "city": "Invercargill",
            "state": "",
            "postcode": "9810",
            "country": "NZ",
        },
        "currency": "NZD",
        "lines": [
            {
                "line_no": "1",
                "buyer_item": "122134",
                "supplier_item": "5101000",
                "description": "Product Description",
                "qty_invoiced": "2",
                "qty_unit": "EA",
                "qty_consumer_units": "1",
                "moa_128": "264.8800",
                "moa_369": "39.7300",
                "moa_203": "304.6100",
                "price": "132.4400",
                "tax_rate": "15.00",
                "tax_category": "GST",
            },
        ],
        "summary": {
            "moa_39": "304.61",
            "moa_128": "264.88",
            "moa_369": "39.73",
        },
    }


def test_invoic_matches_golden_fixture():
    result = build_invoic(_fixture_payload(), ctrl_ref=12341, msg_ref=1)
    fixture_text = _load("nimbrel_invoic_expected.edi")
    assert assert_equivalent(result.decode("latin-1"), fixture_text) is True


def test_invoic_passes_control_invariants():
    result = build_invoic(_fixture_payload(), ctrl_ref=12341, msg_ref=1)
    _, segs = tokenize(result.decode("latin-1"))
    assert validate_interchange(segs) is True


def test_invoic_returns_bytes():
    result = build_invoic(_fixture_payload(), ctrl_ref=12341, msg_ref=1)
    assert isinstance(result, bytes)


def test_invoic_cnt_matches_line_count():
    payload = _fixture_payload()
    result = build_invoic(payload, ctrl_ref=12341, msg_ref=1)
    _, segs = tokenize(result.decode("latin-1"))
    cnt = [s for s in segs if s.tag == "CNT" and s.comp(0, 0) == "2"][0]
    lin = sum(1 for s in segs if s.tag == "LIN")
    assert int(cnt.comp(0, 1)) == lin == len(payload["lines"])


def test_invoic_envelope_qualifiers():
    result = build_invoic(_fixture_payload(), ctrl_ref=12341, msg_ref=1)
    _, segs = tokenize(result.decode("latin-1"))
    unb = [s for s in segs if s.tag == "UNB"][0]
    assert {unb.comp(1, 1), unb.comp(2, 1)} == {"14", "ZZZ"}
    assert unb.comp(1, 0) == "SUPPLIER_GLN"
    assert unb.comp(2, 0) == "NIMBREL"
