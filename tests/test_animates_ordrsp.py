"""Pure-Python golden-fixture test for the Animates D.01B ORDRSP builder (B3).

No Odoo env. Run: pytest tests/test_animates_ordrsp.py -q

Asserts segment-equivalence against the verbatim MIG fixture
``fixtures/animates_ordrsp_expected.edi`` (UNT 39) via the shared normalized
comparator, plus the control-count/reference invariants.
"""
from pathlib import Path

from mml_edi.parsers.animates_edifact import (
    tokenize,
    assert_equivalent,
    validate_interchange,
)
from mml_edi.parsers.animates_ordrsp import build_ordrsp

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name):
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


# Payload carrying exactly the data in animates_ordrsp_expected.edi.
ORDRSP_PAYLOAD = {
    "po_response_no": "POR-278156789",
    "ack_code": "4",
    "message_date": "20200916",
    "requested_date": "20200918",
    "po_number": "PO169603",
    "buyer": "ANIMATES",
    "supplier": "V1058",
    "ship_to": "12345",
    "currency": "NZD",
    "interchange": {"date": "200916", "time": "0730"},
    "lines": [
        {
            # rejected -> committed 0, FTX reason mandatory
            "line_no": 1,
            "action": "7",
            "buyer_item": "2581281",
            "supplier_item": "5101000",
            "description": "Product description",
            "qty_ordered": "10",
            "qty_pack": "1",
            "qty_committed": "0",
            "reason": "Item out of stock",
            "price": "12.0000",
            "tax_rate": "15.00",
        },
        {
            # accepted in full
            "line_no": 2,
            "action": "5",
            "buyer_item": "2581888",
            "supplier_item": "621000",
            "description": "Product description",
            "qty_ordered": "8",
            "qty_pack": "1",
            "qty_committed": "8",
            "reason": None,
            "price": "40.0000",
            "tax_rate": "15.00",
        },
        {
            # changed -> no PIA+1, no QTY+21 echoed in this fixture
            "line_no": 3,
            "action": "3",
            "buyer_item": "2581999",
            "supplier_item": None,
            "description": "Product description",
            "qty_ordered": None,
            "qty_pack": "1",
            "qty_committed": "20",
            "reason": None,
            "price": "50.0000",
            "tax_rate": "15.00",
        },
    ],
}


def _normalize_unt_count(text):
    """Return ``text`` with the UNT 0074 segment-count rewritten to the true UNH..UNT
    count.

    The committed golden fixture ``animates_ordrsp_expected.edi`` is internally
    INCONSISTENT: it declares ``UNT+39`` while only carrying 38 segments from UNH..UNT
    inclusive (the SPS Commerce MIG sample's PDF shows a ``QTY+21:48:EA`` on LIN 3 that
    was dropped from the committed fixture, leaving the 0074 count one too high). The raw
    fixture therefore fails ``validate_interchange`` (UNT0074 39 != actual 38).

    This is exactly the class of MIG sample defect the runbook's octo C3 correction calls
    out ("MIG samples are internally inconsistent ... assert the INVARIANT, not literal
    bytes"). ``assert_equivalent`` already normalizes UNH/UNT *0062* msg-ref padding; here
    we additionally normalize the UNT *0074* count so the verbatim body comparison is not
    held hostage to the fixture's off-by-one control count. The builder itself always
    emits the correct, validating count (asserted separately below).
    """
    _, segs = tokenize(text)
    tags = [s.tag for s in segs]
    if "UNH" in tags and "UNT" in tags:
        i0, i1 = tags.index("UNH"), tags.index("UNT")
        true_count = str(i1 - i0 + 1)
        return text.replace("UNT+39+", "UNT+%s+" % true_count)
    return text


def test_ordrsp_matches_golden_fixture():
    result = build_ordrsp(ORDRSP_PAYLOAD, ctrl_ref=12341, msg_ref=1)
    expected = _load("animates_ordrsp_expected.edi")
    # Compare every segment verbatim except the fixture's self-inconsistent UNT 0074 count.
    assert assert_equivalent(
        result.decode("latin-1"), _normalize_unt_count(expected)
    ) is True


def test_ordrsp_control_invariants_hold():
    result = build_ordrsp(ORDRSP_PAYLOAD, ctrl_ref=12341, msg_ref=1)
    _, segments = tokenize(result.decode("latin-1"))
    # The builder's output is fully self-consistent (unlike the raw fixture).
    assert validate_interchange(segments) is True
