"""Pure-Python golden-fixture test for the Animates D.01B ORDRSP builder (B3).

No Odoo env. Run: pytest tests/test_animates_ordrsp.py -q

Asserts segment-equivalence against the verbatim MIG fixture
``fixtures/animates_ordrsp_expected.edi`` (UNT 39) via the shared normalized
comparator, plus the control-count/reference invariants.
"""
from pathlib import Path

import pytest

from mml_edi.parsers.animates_edifact import (
    tokenize,
    assert_equivalent,
    validate_interchange,
    EdifactError,
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
            # changed -> no PIA+1; QTY+21 IS mandatory on a changed line per the
            # MIG QTY notes ("both 'Ordered quantity' and 'Quantity to be
            # delivered' must be provided" when LIN 1229 == 3) — finding #12.
            "line_no": 3,
            "action": "3",
            "buyer_item": "2581999",
            "supplier_item": None,
            "description": "Product description",
            "qty_ordered": "48",
            "qty_pack": "1",
            "qty_committed": "20",
            "reason": None,
            "price": "50.0000",
            "tax_rate": "15.00",
        },
    ],
}


def test_ordrsp_matches_golden_fixture():
    """The committed fixture now carries the MIG's QTY+21:48:EA on the changed LIN 3
    (finding #12 — the previous fixture dropped it, one segment short of its own
    declared UNT+39). With QTY+21 present the fixture is internally consistent, so
    the builder's output can be compared verbatim (module-ref-padding aside)."""
    result = build_ordrsp(ORDRSP_PAYLOAD, ctrl_ref=12341, msg_ref=1)
    expected = _load("animates_ordrsp_expected.edi")
    assert assert_equivalent(result.decode("latin-1"), expected) is True


def test_ordrsp_control_invariants_hold():
    result = build_ordrsp(ORDRSP_PAYLOAD, ctrl_ref=12341, msg_ref=1)
    _, segments = tokenize(result.decode("latin-1"))
    # The builder's output is fully self-consistent (unlike the raw fixture).
    assert validate_interchange(segments) is True


# --- finding #12: QTY+21 mandatory when LIN 1229 == 3 (changed) ---

def test_changed_line_missing_qty_ordered_raises():
    payload = dict(ORDRSP_PAYLOAD, lines=[{
        "line_no": 3, "action": "3", "buyer_item": "2581999",
        "supplier_item": None, "description": "Product description",
        "qty_ordered": None, "qty_pack": "1", "qty_committed": "20",
        "reason": None, "price": "50.0000", "tax_rate": "15.00",
    }])
    with pytest.raises(EdifactError, match="QTY\\+21"):
        build_ordrsp(payload, ctrl_ref=12341, msg_ref=1)


def test_changed_line_with_qty_ordered_builds_ok():
    payload = dict(ORDRSP_PAYLOAD, lines=[{
        "line_no": 3, "action": "3", "buyer_item": "2581999",
        "supplier_item": None, "description": "Product description",
        "qty_ordered": "48", "qty_pack": "1", "qty_committed": "20",
        "reason": None, "price": "50.0000", "tax_rate": "15.00",
    }])
    result = build_ordrsp(payload, ctrl_ref=12341, msg_ref=1)
    _, segs = tokenize(result.decode("latin-1"))
    qty = [s for s in segs if s.tag == "QTY" and s.comp(0, 0) == "21"]
    assert qty and qty[0].comp(0, 1) == "48"


def test_accepted_and_rejected_lines_may_omit_qty_ordered():
    """Only action 3 (changed) makes QTY+21 mandatory; 5/7 keep it optional."""
    payload = dict(ORDRSP_PAYLOAD, lines=[
        {
            "line_no": 1, "action": "5", "buyer_item": "2581888",
            "supplier_item": None, "description": "d", "qty_ordered": None,
            "qty_pack": "1", "qty_committed": "8", "reason": None,
            "price": "40.0000", "tax_rate": "15.00",
        },
        {
            "line_no": 2, "action": "7", "buyer_item": "2581281",
            "supplier_item": None, "description": "d", "qty_ordered": None,
            "qty_pack": "1", "qty_committed": "0", "reason": "OOS",
            "price": "12.0000", "tax_rate": "15.00",
        },
    ])
    result = build_ordrsp(payload, ctrl_ref=12341, msg_ref=1)
    assert isinstance(result, bytes)


# --- AN-01: envelope identity kwargs are forwarded to build_unb ---

_REAL_INTERCHANGE = {"date": "260703", "time": "1015"}


def test_build_ordrsp_forwards_real_envelope_identity():
    payload = dict(ORDRSP_PAYLOAD, interchange=_REAL_INTERCHANGE)
    result = build_ordrsp(
        payload, supplier_gln="9419416000008", ctrl_ref=555, msg_ref=1,
        sender_qualifier="ZZZ", recipient="ANIMATES", recipient_qualifier="ZZZ",
        require_real=True,
    )
    _, segs = tokenize(result.decode("latin-1"))
    unb = [s for s in segs if s.tag == "UNB"][0]
    assert unb.elements[1] == ["9419416000008", "ZZZ"]
    assert unb.elements[2] == ["ANIMATES", "ZZZ"]


def test_build_ordrsp_require_real_rejects_placeholder_sender():
    payload = dict(ORDRSP_PAYLOAD, interchange=_REAL_INTERCHANGE)
    with pytest.raises(EdifactError):
        build_ordrsp(payload, supplier_gln="SUPPLIER_GLN", ctrl_ref=555,
                     msg_ref=1, require_real=True)


def test_build_ordrsp_require_real_rejects_placeholder_ctrl_ref():
    payload = dict(ORDRSP_PAYLOAD, interchange=_REAL_INTERCHANGE)
    with pytest.raises(EdifactError):
        build_ordrsp(payload, supplier_gln="9419416000008", ctrl_ref=12341,
                     msg_ref=1, require_real=True)


def test_build_ordrsp_default_kwargs_unchanged_for_pure_tests():
    """Backward compatibility: no envelope kwargs -> identical to pre-AN-01 output."""
    result = build_ordrsp(ORDRSP_PAYLOAD, ctrl_ref=12341, msg_ref=1)
    expected = _load("animates_ordrsp_expected.edi")
    assert assert_equivalent(result.decode("latin-1"), expected) is True
