"""Pure-Python tests for the outbound Animates D.01B INVOIC builder (B5).

No Odoo env. Run: pytest tests/test_animates_invoic.py -q

The payload below carries EXACTLY the data in the verbatim MIG worked example
(docs/animates/Animates_INVOIC.pdf p64-65 == tests/fixtures/animates_invoic_expected.edi):
line amounts/price as 4dp strings, summary amounts + tax rate as 2dp strings.
"""
from pathlib import Path

from mml_edi.parsers.animates_edifact import (
    assert_equivalent,
    tokenize,
    validate_interchange,
)
from mml_edi.parsers.animates_invoic import build_invoic

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name):
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def _fixture_payload():
    """The exact data encoded in animates_invoic_expected.edi."""
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
            "code": "ANIMATES",
            "name": "Animates NZ Holding LTD",
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
            "name": "Animates Invercargill",
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
    fixture_text = _load("animates_invoic_expected.edi")
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
    assert unb.comp(2, 0) == "ANIMATES"


class TestInvoicEmptyCompositesAndLengths:
    """SPS validation rejections observed on live invoice 3362524357 (Aug 2026).

    Three MIG violations, all from emitting data we did not actually have:
      1. "extra trailing Sub-Element separators ... Composite RFF010"  -> RFF+AMT:
      2. "extra trailing Sub-Element separators ... Composite CTA020"  -> CTA+OC+:
      3. "length of Sub-Element NAD040-010 (Party name) is '38' ... max '35'"
    """

    def _segments(self, raw):
        return [s for s in raw.decode("latin-1").split("'") if s]

    def test_blank_nzbn_omits_rff_amt_entirely(self):
        payload = _fixture_payload()
        payload["buyer"] = dict(payload["buyer"], nzbn="")
        segs = self._segments(build_invoic(payload))
        assert "RFF+AMT:" not in segs, "empty composite must not be emitted"
        # the supplier's ABN is still present, so exactly one RFF+AMT survives
        assert sum(s.startswith("RFF+AMT") for s in segs) == 1

    def test_blank_contact_name_emits_bare_cta(self):
        payload = _fixture_payload()
        payload["supplier"] = dict(payload["supplier"], contact_name="")
        segs = self._segments(build_invoic(payload))
        assert "CTA+OC+:" not in segs, "empty composite must not be emitted"
        # CTA must survive so its COM children are not orphaned
        assert "CTA+OC" in segs
        assert any(s.startswith("COM+") for s in segs)

    def test_overlong_party_name_is_clipped_to_35(self):
        payload = _fixture_payload()
        payload["ship_to"] = dict(
            payload["ship_to"], name="Animates Invercargill (SPS TEST 12345)"  # 38
        )
        segs = self._segments(build_invoic(payload))
        nad_st = next(s for s in segs if s.startswith("NAD+ST"))
        name = nad_st.split("+")[4]
        assert len(name) <= 35, "%r is %d chars" % (name, len(name))
        assert name == "Animates Invercargill (SPS TEST 123"

    def test_populated_values_still_emitted_unchanged(self):
        """Back-compat: the MIG worked example must be byte-identical."""
        segs = self._segments(build_invoic(_fixture_payload()))
        assert "RFF+AMT:9429040432250" in segs
        assert "RFF+AMT:12345678901" in segs
        assert "CTA+OC+:Ms M" in segs
