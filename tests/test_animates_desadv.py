"""Pure-Python golden-fixture tests for the Animates D.01B DESADV builder (B4).

No Odoo env. Run: pytest tests/test_animates_desadv.py -q

Two shapes are exercised, both reproduced verbatim from the MIG worked examples:
- animates_desadv_pallet.edi : 1 pallet (8 inner cartons) + 2 cartons, UNT=39, CNT+2:3
- animates_desadv_split.edi  : split shipment (ALI+++165 + QVR + DTM+17), UNT=27, CNT+2:1
"""
from pathlib import Path

from mml_edi.parsers.animates_edifact import (
    tokenize, validate_interchange, normalized_segments,
)
from mml_edi.parsers.animates_desadv import build_desadv

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name):
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def _real_unt_count(text):
    """Actual UNH..UNT-inclusive segment count for a single-message interchange."""
    _, segs = tokenize(text)
    start = next(i for i, s in enumerate(segs) if s.tag == "UNH")
    end = next(i for i, s in enumerate(segs) if s.tag == "UNT")
    return end - start + 1


def assert_equivalent_modulo_unt(actual, expected):
    """Assert full segment equivalence, reconciling the published-but-non-conformant
    UNT 0074 count in the MIG fixtures.

    The p.57/p.58 worked examples declare UNT counts (39 / 27) that do not equal the
    real number of segments they contain (38 / 24) — a documented MIG inconsistency the
    octo review flagged (C3 / H-COMPARE: assert the invariant via validate_interchange,
    not the literal byte). We therefore compare every segment verbatim, but for UNT we
    require the BUILDER's value to be the internally-consistent real count (proven
    separately by validate_interchange), accepting that it differs from the oracle byte.
    """
    a = normalized_segments(actual)
    e = normalized_segments(expected)
    assert len(a) == len(e), "segment count differs: %d vs %d" % (len(a), len(e))
    real = _real_unt_count(actual)
    for idx, (sa, se) in enumerate(zip(a, e)):
        if sa[0] == "UNT" and se[0] == "UNT":
            # builder UNT count must equal the true span (NOT the miscounted oracle)
            assert sa[1][0][0] == str(real), "UNT count not self-consistent: %r" % (sa,)
            continue
        assert sa == se, "segment %d differs:\n  actual:   %r\n  expected: %r" % (idx, sa, se)
    return True


# --- Payloads reverse-engineered from the golden fixtures ---

PALLET_PAYLOAD = {
    "advice_no": "95703",
    "doc_date": "20200921",
    "despatch_date": "20200922",
    "po": "PO0319333",
    "connote": "SY00857",
    "buyer": "ANIMATES",
    "ship_to": "12345",
    "supplier": "V1058",
    "split": False,
    "shipment_totals": {"pallets": 1, "units": 2, "unit_pac_type": "CT"},
    "units": [
        {
            # Pallet: opens CPS+2, carries inner-carton PAC+8++CT.
            "cps_idx": 2,
            "emit_cps": True,
            "pac_type": "09",
            "sscc": "00593161000045350112",
            "inner_cartons": 8,
            "line_no": "1",
            "gtin": "9310088126129",
            "isc": "2581281",
            "vendor_code": "VEN111",
            "qty": "96",
        },
        {
            # First carton: contained by the pallet -> NO own CPS (nested).
            "emit_cps": False,
            "pac_type": "CT",
            "sscc": "00693161000027682504",
            "line_no": "2",
            "isc": "2581888",
            "vendor_code": "VEN222",
            "qty": "24",
        },
        {
            # Second carton: free-standing -> opens CPS+4 (index 3 is reserved/omitted).
            "cps_idx": 4,
            "emit_cps": True,
            "pac_type": "CT",
            "sscc": "00693161000027682498",
            "line_no": "3",
            "isc": "2581999",
            "vendor_code": "VEN333",
            "qty": "12",
        },
    ],
}

SPLIT_PAYLOAD = {
    "advice_no": "25488",
    "doc_date": "20200921",
    "despatch_date": "20200922",
    "po": "PO0319333",
    "connote": "SY00857",
    "buyer": "ANIMATES",
    "ship_to": "12345",
    "supplier": "V1058",
    "split": True,
    "shipment_totals": {"units": 1, "unit_pac_type": "CT"},
    "units": [
        {
            "pac_type": "CT",
            "sscc": "193106531002906599",
            "line_no": "1",
            "gtin": "9314598018011",
            "isc": "2581777",
            "vendor_code": "VEN777",
            "qty": "24",
            "committed": "200",
            "eta": "20201015",
        },
    ],
}


def test_desadv_pallet_matches_golden():
    result = build_desadv(PALLET_PAYLOAD, ctrl_ref=78401, msg_ref=1)
    expected = _load("animates_desadv_pallet.edi")
    # Every body/envelope segment matches the golden fixture verbatim; the only
    # divergence is the fixture's non-conformant UNT count (39 vs real 38).
    assert assert_equivalent_modulo_unt(result.decode("latin-1"), expected) is True


def test_desadv_pallet_validates():
    result = build_desadv(PALLET_PAYLOAD, ctrl_ref=78401, msg_ref=1)
    _, segs = tokenize(result.decode("latin-1"))
    assert validate_interchange(segs) is True


def test_desadv_split_matches_golden():
    result = build_desadv(SPLIT_PAYLOAD, ctrl_ref=78402, msg_ref=1)
    expected = _load("animates_desadv_split.edi")
    # As above: only the fixture's non-conformant UNT count (27 vs real 24) differs.
    assert assert_equivalent_modulo_unt(result.decode("latin-1"), expected) is True


def test_desadv_split_validates():
    result = build_desadv(SPLIT_PAYLOAD, ctrl_ref=78402, msg_ref=1)
    _, segs = tokenize(result.decode("latin-1"))
    assert validate_interchange(segs) is True


def test_desadv_returns_bytes():
    assert isinstance(build_desadv(SPLIT_PAYLOAD, ctrl_ref=78402, msg_ref=1), bytes)
