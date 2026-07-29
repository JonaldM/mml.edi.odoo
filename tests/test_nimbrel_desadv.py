"""Pure-Python golden-fixture tests for the Nimbrel D.01B DESADV builder (B4).

No Odoo env. Run: pytest tests/test_nimbrel_desadv.py -q

Two shapes are exercised, both reproduced verbatim from the MIG worked examples:
- nimbrel_desadv_pallet.edi : 1 pallet (8 inner cartons) + 2 cartons, UNT=39, CNT+2:3
- nimbrel_desadv_split.edi  : split shipment (ALI+++165 + QVR + DTM+17), UNT=27, CNT+2:1
"""
from pathlib import Path

from mml_edi.parsers.nimbrel_edifact import (
    tokenize, validate_interchange, normalized_segments,
)
from mml_edi.parsers.nimbrel_desadv import build_desadv

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
    "buyer": "NIMBREL",
    "ship_to": "12345",
    "supplier": "V1058",
    "split": False,
    "shipment_totals": {"pallets": 1, "units": 2, "unit_pac_type": "CT"},
    "units": [
        {
            # Pallet: opens CPS+2, carries inner-carton PAC+8++CT.
            "cps_idx": 2,
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
            # First carton: physically carried on the pallet, but per AN-18 it
            # still opens its OWN CPS+3 (MIG worked example p.56-57).
            "cps_idx": 3,
            "pac_type": "CT",
            "sscc": "00693161000027682504",
            "line_no": "2",
            "isc": "2581888",
            "vendor_code": "VEN222",
            "qty": "24",
        },
        {
            # Second carton: free-standing -> opens CPS+4.
            "cps_idx": 4,
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
    "buyer": "NIMBREL",
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
    expected = _load("nimbrel_desadv_pallet.edi")
    # Every body/envelope segment matches the golden fixture verbatim; the only
    # divergence is the fixture's non-conformant UNT count (39 vs real 38).
    assert assert_equivalent_modulo_unt(result.decode("latin-1"), expected) is True


def test_desadv_pallet_validates():
    result = build_desadv(PALLET_PAYLOAD, ctrl_ref=78401, msg_ref=1)
    _, segs = tokenize(result.decode("latin-1"))
    assert validate_interchange(segs) is True


def test_desadv_split_matches_golden():
    result = build_desadv(SPLIT_PAYLOAD, ctrl_ref=78402, msg_ref=1)
    expected = _load("nimbrel_desadv_split.edi")
    # As above: only the fixture's non-conformant UNT count (27 vs real 24) differs.
    assert assert_equivalent_modulo_unt(result.decode("latin-1"), expected) is True


def test_desadv_split_validates():
    result = build_desadv(SPLIT_PAYLOAD, ctrl_ref=78402, msg_ref=1)
    _, segs = tokenize(result.decode("latin-1"))
    assert validate_interchange(segs) is True


def test_desadv_returns_bytes():
    assert isinstance(build_desadv(SPLIT_PAYLOAD, ctrl_ref=78402, msg_ref=1), bytes)


def test_every_unit_gets_its_own_cps():
    """AN-18: the pallet-contained carton (unit[1]) must open its own CPS,
    not be silently nested under the pallet's CPS group."""
    result = build_desadv(PALLET_PAYLOAD, ctrl_ref=78401, msg_ref=1)
    _, segs = tokenize(result.decode("latin-1"))
    cps_segs = [s for s in segs if s.tag == "CPS"]
    # CPS+1 (shipment) + CPS+2 (pallet) + CPS+3 (contained carton) + CPS+4 (free carton)
    assert len(cps_segs) == 4
    indices = [s.comp(0, 0) for s in cps_segs]
    assert indices == ["1", "2", "3", "4"]


def test_cps_count_matches_unt_span():
    """Per-SSCC CPS count is consistent with the builder's own UNT segment
    count (no silently-dropped or double-counted segments)."""
    result = build_desadv(PALLET_PAYLOAD, ctrl_ref=78401, msg_ref=1)
    text = result.decode("latin-1")
    _, segs = tokenize(text)
    start = next(i for i, s in enumerate(segs) if s.tag == "UNH")
    end = next(i for i, s in enumerate(segs) if s.tag == "UNT")
    real_span = end - start + 1
    unt = next(s for s in segs if s.tag == "UNT")
    assert unt.comp(0, 0) == str(real_span)


# --- Scenario 5A/5B: partial then completing shipment ----------------------
# Testing Scenario Handbook p.14-16: PO0319555 has 3 lines (ISC1/ISC2/ISC3).
# 5A ships ISC1+ISC2 only (ALI 4183=165, split); ISC3 "should not be present".
# 5B (a separate DESADV, days later) ships ISC3 only (ALI 4183=164, complete).

SCENARIO_5A_PARTIAL_PAYLOAD = {
    "advice_no": "55501",
    "doc_date": "20260701",
    "despatch_date": "20260701",
    "po": "PO0319555",
    "connote": "SY55501",
    "buyer": "NIMBREL",
    "ship_to": "12345",
    "supplier": "V1058",
    "split": True,  # ALI+++165 — subsequent shipment(s) will follow
    "shipment_totals": {"units": 2, "unit_pac_type": "CT"},
    "units": [
        {
            "cps_idx": 2,
            "pac_type": "CT",
            "sscc": "00593161000045350200",
            "line_no": "1",
            "isc": "2581001",
            "vendor_code": "VEN001",
            "qty": "12",
        },
        {
            "cps_idx": 3,
            "pac_type": "CT",
            "sscc": "00593161000045350217",
            "line_no": "2",
            "isc": "2581002",
            "vendor_code": "VEN002",
            "qty": "12",
        },
        # ISC3 deliberately absent — scenario 5A: "should not be present in the DESADV".
    ],
}

SCENARIO_5B_COMPLETE_PAYLOAD = {
    "advice_no": "55502",
    "doc_date": "20260705",
    "despatch_date": "20260705",
    "po": "PO0319555",
    "connote": "SY55502",
    "buyer": "NIMBREL",
    "ship_to": "12345",
    "supplier": "V1058",
    "split": False,  # completing shipment: ALI carries 164 explicitly (see below)
    "shipment_totals": {"units": 1, "unit_pac_type": "CT"},
    "units": [
        {
            "cps_idx": 2,
            "pac_type": "CT",
            "sscc": "00593161000045350224",
            "line_no": "1",
            "isc": "2581003",
            "vendor_code": "VEN003",
            "qty": "12",
        },
        # ISC1/ISC2 (already shipped in 5A) are NOT repeated — the completing
        # DESADV carries only the remainder.
    ],
}


def test_scenario_5a_partial_desadv_omits_unshipped_line():
    """5A: DESADV for ISC1+ISC2 only; ISC3 is absent (not a zero-qty line)."""
    result = build_desadv(SCENARIO_5A_PARTIAL_PAYLOAD, ctrl_ref=55501, msg_ref=1)
    _, segs = tokenize(result.decode("latin-1"))
    lin_lines = {s.comp(0, 0) for s in segs if s.tag == "LIN"}
    assert lin_lines == {"1", "2"}
    isc_values = {s.comp(1, 0) for s in segs if s.tag == "PIA" and s.comp(0, 0) == "5"}
    assert isc_values == {"2581001", "2581002"}
    assert "2581003" not in isc_values


def test_scenario_5a_partial_desadv_uses_ali_165():
    result = build_desadv(SCENARIO_5A_PARTIAL_PAYLOAD, ctrl_ref=55501, msg_ref=1)
    _, segs = tokenize(result.decode("latin-1"))
    ali = next(s for s in segs if s.tag == "ALI")
    assert ali.comp(2, 0) == "165"


def test_scenario_5a_partial_cnt_matches_two_lines():
    result = build_desadv(SCENARIO_5A_PARTIAL_PAYLOAD, ctrl_ref=55501, msg_ref=1)
    _, segs = tokenize(result.decode("latin-1"))
    cnt = next(s for s in segs if s.tag == "CNT")
    assert cnt.comp(0, 1) == "2"


def test_scenario_5b_completing_desadv_carries_only_remainder():
    """5B: the completing DESADV contains only ISC3 — not a re-send of ISC1/ISC2."""
    result = build_desadv(SCENARIO_5B_COMPLETE_PAYLOAD, ctrl_ref=55502, msg_ref=1)
    _, segs = tokenize(result.decode("latin-1"))
    lin_lines = {s.comp(0, 0) for s in segs if s.tag == "LIN"}
    assert lin_lines == {"1"}
    isc_values = {s.comp(1, 0) for s in segs if s.tag == "PIA" and s.comp(0, 0) == "5"}
    assert isc_values == {"2581003"}


def test_scenario_5b_completing_desadv_has_no_ali_by_default():
    """split=False -> no ALI segment emitted; the caller sets ALI+++164 by
    passing split payload data explicitly (see test below) when a completing
    shipment must still assert '164' rather than omitting ALI entirely."""
    result = build_desadv(SCENARIO_5B_COMPLETE_PAYLOAD, ctrl_ref=55502, msg_ref=1)
    _, segs = tokenize(result.decode("latin-1"))
    assert not [s for s in segs if s.tag == "ALI"]


def test_scenario_5b_completing_desadv_can_assert_ali_164():
    """MIG requires ALI+++164 on the shipment that completes a split order — the
    builder supports it via the same 'split' flag carrying a 164 payload flavour."""
    payload = dict(SCENARIO_5B_COMPLETE_PAYLOAD, ali_code="164")
    result = build_desadv(payload, ctrl_ref=55502, msg_ref=1)
    _, segs = tokenize(result.decode("latin-1"))
    ali = next(s for s in segs if s.tag == "ALI")
    assert ali.comp(2, 0) == "164"


def test_scenario_5a_then_5b_validate_independently():
    for payload, ref in (
        (SCENARIO_5A_PARTIAL_PAYLOAD, 55501),
        (SCENARIO_5B_COMPLETE_PAYLOAD, 55502),
    ):
        result = build_desadv(payload, ctrl_ref=ref, msg_ref=1)
        _, segs = tokenize(result.decode("latin-1"))
        assert validate_interchange(segs) is True
