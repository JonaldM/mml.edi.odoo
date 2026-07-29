"""Pure-Python tests for the Nimbrel D.01B EDIFACT foundation (B1).

No Odoo env. Run: pytest tests/test_nimbrel_edifact.py -q
"""
from pathlib import Path

import pytest

from mml_edi.parsers.nimbrel_edifact import (
    Delims, tokenize, serialize, escape, build_unb, build_unh, build_unt, build_unz,
    pad_ref, validate_interchange, normalized_segments, assert_equivalent, EdifactError,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name):
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


# --- UNA / delimiters ---
def test_una_parsed_from_message():
    d = Delims.from_una("UNA:+.? '")
    assert (d.component, d.element, d.decimal, d.release, d.segment) == (":", "+", ".", "?", "'")


# --- ? release-char tokenizer (octo M-TOKENIZER) ---
def test_release_doubled_is_literal_question():
    _, segs = tokenize("UNA:+.? 'FTX+ab??cd'")
    assert segs[0].comp(0, 0) == "ab?cd"

def test_release_protects_element_separator():
    _, segs = tokenize("UNA:+.? 'FTX+a?+b'")
    assert segs[0].comp(0, 0) == "a+b"  # not split into two elements

def test_release_protects_component_separator():
    _, segs = tokenize("UNA:+.? 'FTX+a?:b'")
    assert segs[0].comp(0, 0) == "a:b"  # one component, not two

def test_release_protects_segment_terminator():
    _, segs = tokenize("UNA:+.? 'FTX+a?'b'")
    assert len(segs) == 1 and segs[0].comp(0, 0) == "a'b"

def test_escape_round_trip():
    d = Delims()
    raw = "a+b:c'd?e"
    text = "UNA:+.? '" + "FTX+" + escape(raw, d) + "'"
    _, segs = tokenize(text)
    assert segs[0].comp(0, 0) == raw

def test_escape_releases_release_char_first():
    d = Delims()
    assert escape("a?b", d) == "a??b"  # not over-escaped


# --- envelope qualifiers (octo C1) + CONTRL tail (octo C2) ---
def test_unb_business_uses_supplier14_nimbrel_zzz_with_tail():
    unb = build_unb("5412345000013", 12341, "200916", "1030")
    assert unb.elements[1] == ["5412345000013", "14"]   # sender = supplier:14
    assert unb.elements[2] == ["NIMBREL", "ZZZ"]        # recipient = NIMBREL:ZZZ
    assert unb.elements[-1] == ["1"]                      # business message trailing ack-request
    assert len(unb.elements) == 9                         # UNOC:3, sender, recipient, dt, ref, +++1

def test_unb_contrl_omits_trailing_tail():
    unb = build_unb("5412345000013", 99101, "200928", "1030", contrl=True)
    assert unb.elements[-1] == ["99101"]                 # ends at the control reference
    assert len(unb.elements) == 5                        # no ++++1

def test_unb_inbound_inverts_parties():
    unb = build_unb("5412345000013", 12341, "200916", "1030", inbound=True)
    assert unb.elements[1] == ["NIMBREL", "ZZZ"]
    assert unb.elements[2] == ["5412345000013", "14"]


# --- control-count validation on a self-built correct message ---
def _build_min_message(ctrl=555, ref=1, extra_lines=0, cnt=None):
    segs = [build_unb("GLN1", ctrl, "200916", "1030"),
            build_unh(pad_ref(ref), "ORDERS", release="01B", agency="UN")]
    body = []
    for i in range(extra_lines):
        body.append(type(segs[0])("LIN", [[str(i + 1)]]))
    if cnt is not None:
        body.append(type(segs[0])("CNT", [["2", str(cnt)]]))
    segs += body
    count = 2 + len(body)  # UNH..UNT inclusive
    segs.append(build_unt(count, pad_ref(ref)))
    segs.append(build_unz(1, ctrl))
    return segs

def test_validate_passes_on_well_formed():
    assert validate_interchange(_build_min_message(extra_lines=3, cnt=3)) is True

def test_validate_rejects_bad_party_qualifier():
    segs = _build_min_message()
    segs[0].elements[2] = ["NIMBREL", "14"]  # 14 on BOTH sides = Kestrelby idiom, rejected
    with pytest.raises(EdifactError):
        validate_interchange(segs)

def test_validate_accepts_both_parties_zzz_qualified():
    # C1 identity contract: a partner configured via edi_sender_id (portal
    # test-mailbox convention, e.g. 0200000000004T:ZZZ) + the always-ZZZ
    # Nimbrel recipient is a legitimate production envelope per the CONTRL
    # MIG (sender qualifier may be 14 OR ZZZ, independently of the recipient).
    segs = _build_min_message()
    segs[0].elements[1] = ["0200000000004T", "ZZZ"]
    assert validate_interchange(segs) is True

def test_validate_rejects_unknown_qualifier():
    segs = _build_min_message()
    segs[0].elements[1] = ["0200000000004", "91"]  # not a known Nimbrel qualifier
    with pytest.raises(EdifactError):
        validate_interchange(segs)

def test_validate_rejects_missing_qualifier():
    segs = _build_min_message()
    segs[0].elements[1] = ["0200000000004"]  # qualifier component absent
    with pytest.raises(EdifactError):
        validate_interchange(segs)

def test_validate_rejects_ctrlref_mismatch():
    segs = _build_min_message(ctrl=555)
    segs[-1].elements[1] = ["999"]  # UNZ ctrl ref != UNB
    with pytest.raises(EdifactError):
        validate_interchange(segs)

def test_validate_rejects_cnt_lin_mismatch():
    segs = _build_min_message(extra_lines=2, cnt=5)  # CNT says 5 but only 2 LIN
    with pytest.raises(EdifactError):
        validate_interchange(segs)

def test_validate_rejects_unt_count_mismatch():
    segs = _build_min_message(extra_lines=1)
    segs[-2].elements[0] = ["99"]  # UNT count wrong
    with pytest.raises(EdifactError):
        validate_interchange(segs)


# --- normalized comparison (octo C3 / H-COMPARE) ---
def test_padding_difference_compares_equal():
    a = "UNA:+.? 'UNH+1+ORDERS:D:01B:UN'BGM+220'UNT+3+1'"
    b = "UNA:+.? 'UNH+0001+ORDERS:D:01B:UN'BGM+220'UNT+3+0001'"
    assert assert_equivalent(a, b) is True

def test_real_difference_raises():
    a = "UNA:+.? 'UNH+1+ORDERS:D:01B:UN'BGM+220'UNT+3+1'"
    b = "UNA:+.? 'UNH+1+ORDERS:D:01B:UN'BGM+221'UNT+3+1'"
    with pytest.raises(AssertionError):
        assert_equivalent(a, b)

def test_serialize_round_trip_structural():
    src = _load("nimbrel_orders_PO169603.edi")
    _, segs = tokenize(src)
    assert assert_equivalent(serialize(segs), src) is True


# --- golden fixtures: light structural checks (robust to extraction artifacts) ---
@pytest.mark.parametrize("name", [
    "nimbrel_orders_PO169603.edi", "nimbrel_ordrsp_expected.edi",
    "nimbrel_invoic_expected.edi", "nimbrel_contrl_in.edi", "nimbrel_desadv_split.edi", "nimbrel_desadv_pallet.edi",
])
def test_fixture_envelope_is_nimbrel_shaped(name):
    _, segs = tokenize(_load(name))
    unb = [s for s in segs if s.tag == "UNB"][0]
    unz = [s for s in segs if s.tag == "UNZ"][0]
    assert {unb.comp(1, 1), unb.comp(2, 1)} == {"14", "ZZZ"}     # C1 party qualifiers
    assert unb.comp(4, 0) == unz.comp(1, 0)                       # interchange ctrl ref matches

def test_contrl_fixture_unb_has_no_trailing_tail():
    _, segs = tokenize(_load("nimbrel_contrl_in.edi"))
    unb = [s for s in segs if s.tag == "UNB"][0]
    assert unb.elements[-1] == [unb.comp(4, 0)]                   # ends at control reference
