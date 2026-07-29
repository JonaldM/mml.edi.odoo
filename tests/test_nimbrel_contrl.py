"""Pure-Python TDD for the Nimbrel D.01B CONTRL (functional ack) builder + parser.

No Odoo env. Run: pytest tests/test_nimbrel_contrl.py -q

CONTRL is a syntax/service report acknowledging RECEIPT of an interchange (DE0083
action code 8 = "interchange received"). Per the Nimbrel MIG (Nimbrel_CONTRL.pdf
p.8 worked example, p.13 envelope notes):

  UNA:+.? '
  UNB+UNOC:3+NIMBREL:ZZZ+SUPPLIER_GLN:14+200928:1030+99101'   (NO trailing ++++1)
  UNH+0001+CONTRL:D:3:UN:EAN004'
  UCI+72+SUPPLIER_GLN:14+NIMBREL:ZZZ+8'
  UNT+3+0001'
  UNZ+1+99101'

Key invariants under test (octo C2 / M-CONTRL-UCI / H-INVARIANTS):
- CONTRL UNB OMITS DE0031 (the trailing ``++++1`` application-ref/ack-request tail).
- UCI parties (S002 sender, S003 recipient) are the ORIGINAL acked interchange's
  UNB sender/recipient VERBATIM, NOT the CONTRL envelope's (which inverts them).
- UCI DE0083 action code is always ``8``.
- UCI DE0020 (original interchange control reference) correlates the ack.
"""
from pathlib import Path

import pytest

from mml_edi.parsers.nimbrel_edifact import (
    tokenize, validate_interchange, EdifactError,
)
from mml_edi.parsers.nimbrel_contrl import build_contrl, parse_contrl, extract_unb_identity

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name):
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def _segs(text_or_bytes):
    if isinstance(text_or_bytes, bytes):
        text_or_bytes = text_or_bytes.decode("latin-1")
    return tokenize(text_or_bytes)[1]


def _by_tag(segs, tag):
    return [s for s in segs if s.tag == tag]


# --- (a) build an OUTBOUND CONTRL for a hypothetical inbound ORDERS interchange ---
#
# We (MML/supplier) received an inbound ORDERS interchange FROM Nimbrel and must
# ack it back TO Nimbrel. The inbound interchange's UNB was:
#   UNB+UNOC:3+NIMBREL:ZZZ+SUPPLIER_GLN:14+...+4711'
# So:
#   - the OUTBOUND CONTRL envelope goes supplier->Nimbrel (sender SUPPLIER_GLN:14,
#     recipient NIMBREL:ZZZ), with NO ++++1 tail (contrl=True);
#   - the UCI carries the ORIGINAL interchange's parties verbatim
#     (sender NIMBREL:ZZZ, recipient SUPPLIER_GLN:14), ctrl ref 4711, action 8.

_ORDERS_INBOUND_PAYLOAD = {
    "original_ref": "4711",
    "original_sender_id": "NIMBREL",
    "original_sender_qual": "ZZZ",
    "original_recipient_id": "SUPPLIER_GLN",
    "original_recipient_qual": "14",
    "date": "200930",
    "time": "0915",
}


def test_build_outbound_contrl_envelope_is_supplier_to_nimbrel():
    out = build_contrl(_ORDERS_INBOUND_PAYLOAD, supplier_gln="SUPPLIER_GLN",
                       ctrl_ref=88001, msg_ref=1)
    segs = _segs(out)
    unb = _by_tag(segs, "UNB")[0]
    # OUTBOUND envelope: sender = supplier:14, recipient = NIMBREL:ZZZ
    assert unb.elements[1] == ["SUPPLIER_GLN", "14"]
    assert unb.elements[2] == ["NIMBREL", "ZZZ"]


def test_build_outbound_contrl_unb_has_no_trailing_tail():
    out = build_contrl(_ORDERS_INBOUND_PAYLOAD, ctrl_ref=88001, msg_ref=1)
    segs = _segs(out)
    unb = _by_tag(segs, "UNB")[0]
    # contrl=True: UNB ends at the interchange control reference (no ++++1)
    assert unb.elements[-1] == [unb.comp(4, 0)]
    assert len(unb.elements) == 5
    assert unb.comp(4, 0) == "88001"


def test_build_outbound_contrl_unh_is_contrl_d3_ean004():
    out = build_contrl(_ORDERS_INBOUND_PAYLOAD, ctrl_ref=88001, msg_ref=1)
    segs = _segs(out)
    unh = _by_tag(segs, "UNH")[0]
    assert unh.elements[1] == ["CONTRL", "D", "3", "UN", "EAN004"]


def test_build_outbound_contrl_uci_action_is_8():
    out = build_contrl(_ORDERS_INBOUND_PAYLOAD, ctrl_ref=88001, msg_ref=1)
    segs = _segs(out)
    uci = _by_tag(segs, "UCI")[0]
    # UCI+<ref>+<sender>+<recipient>+8 -> action is the 4th element (index 3)
    assert uci.comp(3, 0) == "8"


def test_build_outbound_contrl_uci_parties_are_the_original_interchange_verbatim():
    out = build_contrl(_ORDERS_INBOUND_PAYLOAD, ctrl_ref=88001, msg_ref=1)
    segs = _segs(out)
    uci = _by_tag(segs, "UCI")[0]
    # UCI parties = ORIGINAL interchange's UNB sender/recipient (NOT the CONTRL envelope's)
    assert uci.comp(0, 0) == "4711"                 # original interchange ctrl ref
    assert uci.elements[1] == ["NIMBREL", "ZZZ"]   # original sender (Nimbrel)
    assert uci.elements[2] == ["SUPPLIER_GLN", "14"]  # original recipient (supplier)


def test_build_outbound_contrl_uci_parties_differ_from_envelope():
    """M-CONTRL-UCI: the UCI parties are the REVERSE of the CONTRL envelope's parties."""
    out = build_contrl(_ORDERS_INBOUND_PAYLOAD, ctrl_ref=88001, msg_ref=1)
    segs = _segs(out)
    unb = _by_tag(segs, "UNB")[0]
    uci = _by_tag(segs, "UCI")[0]
    assert unb.elements[1] == uci.elements[2]  # envelope sender == UCI recipient
    assert unb.elements[2] == uci.elements[1]  # envelope recipient == UCI sender


def test_build_outbound_contrl_structure_unt3_unz1():
    out = build_contrl(_ORDERS_INBOUND_PAYLOAD, ctrl_ref=88001, msg_ref=1)
    segs = _segs(out)
    tags = [s.tag for s in segs]
    assert tags == ["UNB", "UNH", "UCI", "UNT", "UNZ"]
    unt = _by_tag(segs, "UNT")[0]
    unz = _by_tag(segs, "UNZ")[0]
    assert unt.comp(0, 0) == "3"          # UNH..UNT inclusive = 3 segments
    assert unz.comp(0, 0) == "1"          # one message
    assert unz.comp(1, 0) == "88001"      # interchange ctrl ref matches UNB


def test_build_outbound_contrl_validates_as_interchange():
    out = build_contrl(_ORDERS_INBOUND_PAYLOAD, ctrl_ref=88001, msg_ref=1)
    assert validate_interchange(_segs(out)) is True


def test_build_outbound_contrl_returns_bytes():
    out = build_contrl(_ORDERS_INBOUND_PAYLOAD, ctrl_ref=88001, msg_ref=1)
    assert isinstance(out, bytes)


# --- (b) parse the inbound fixture: action == 8 and original_ref matches ---
def test_parse_inbound_fixture_action_and_ref():
    parsed = parse_contrl(_load("nimbrel_contrl_in.edi"))
    assert parsed["action"] == "8"
    assert parsed["original_ref"] == "72"


def test_parse_inbound_fixture_parties():
    parsed = parse_contrl(_load("nimbrel_contrl_in.edi"))
    # UCI parties in the fixture: SUPPLIER_GLN:14 (sender) -> NIMBREL:ZZZ (recipient)
    assert parsed["original_sender_id"] == "SUPPLIER_GLN"
    assert parsed["original_sender_qual"] == "14"
    assert parsed["original_recipient_id"] == "NIMBREL"
    assert parsed["original_recipient_qual"] == "ZZZ"


def test_parse_accepts_bytes():
    raw = (FIXTURES / "nimbrel_contrl_in.edi").read_bytes()
    parsed = parse_contrl(raw)
    assert parsed["action"] == "8"
    assert parsed["original_ref"] == "72"


def test_parse_raises_when_no_uci():
    from mml_edi.parsers.nimbrel_edifact import EdifactError
    text = "UNA:+.? 'UNB+UNOC:3+NIMBREL:ZZZ+SUPPLIER_GLN:14+200928:1030+1'UNH+1+CONTRL:D:3:UN:EAN004'UNT+2+1'UNZ+1+1'"
    with pytest.raises(EdifactError):
        parse_contrl(text)


# --- positive/negative classification (extends parse_contrl) ---

def test_parse_action_8_is_positive():
    parsed = parse_contrl(_load("nimbrel_contrl_in.edi"))
    assert parsed["positive"] is True


def test_parse_non_8_action_is_not_positive():
    """Nimbrel only documents action 8, but any other/unexpected DE0083
    value must be surfaced as NOT positive rather than silently accepted."""
    text = (
        "UNA:+.? '"
        "UNB+UNOC:3+NIMBREL:ZZZ+SUPPLIER_GLN:14+200928:1030+99101'"
        "UNH+0001+CONTRL:D:3:UN:EAN004'"
        "UCI+72+SUPPLIER_GLN:14+NIMBREL:ZZZ+7'"
        "UNT+3+0001'"
        "UNZ+1+99101'"
    )
    parsed = parse_contrl(text)
    assert parsed["action"] == "7"
    assert parsed["positive"] is False


# --- AN-01: build_contrl forwards real envelope identity ---

def test_build_contrl_forwards_real_envelope_identity():
    out = build_contrl(
        _ORDERS_INBOUND_PAYLOAD, supplier_gln="0200000000008", ctrl_ref=555,
        msg_ref=1, sender_qualifier="ZZZ", recipient="NIMBREL",
        recipient_qualifier="ZZZ", require_real=True,
    )
    segs = _segs(out)
    unb = _by_tag(segs, "UNB")[0]
    assert unb.elements[1] == ["0200000000008", "ZZZ"]
    assert unb.elements[2] == ["NIMBREL", "ZZZ"]


def test_build_contrl_require_real_rejects_placeholder_sender():
    with pytest.raises(EdifactError):
        build_contrl(_ORDERS_INBOUND_PAYLOAD, supplier_gln="SUPPLIER_GLN",
                     ctrl_ref=555, msg_ref=1, require_real=True)


def test_build_contrl_require_real_rejects_placeholder_ctrl_ref():
    with pytest.raises(EdifactError):
        build_contrl(_ORDERS_INBOUND_PAYLOAD, supplier_gln="0200000000008",
                     ctrl_ref=99101, msg_ref=1, require_real=True)


def test_build_contrl_default_kwargs_unchanged_for_pure_tests():
    """Backward compatibility: no envelope kwargs -> identical to pre-AN-01 output."""
    out = build_contrl(_ORDERS_INBOUND_PAYLOAD, ctrl_ref=88001, msg_ref=1)
    segs = _segs(out)
    unb = _by_tag(segs, "UNB")[0]
    assert unb.elements[1] == ["SUPPLIER_GLN", "14"]
    assert unb.elements[2] == ["NIMBREL", "ZZZ"]


# --- extract_unb_identity: pull original UNB identity from ANY interchange ---

def test_extract_unb_identity_from_orders_fixture():
    ident = extract_unb_identity(_load("nimbrel_orders_PO169603.edi"))
    assert ident["original_ref"] == "12341"
    assert ident["original_sender_id"] == "NIMBREL"
    assert ident["original_sender_qual"] == "ZZZ"
    assert ident["original_recipient_id"] == "SUPPLIER_GLN"
    assert ident["original_recipient_qual"] == "14"
    assert ident["date"] == "200916"
    assert ident["time"] == "1030"


def test_extract_unb_identity_accepts_bytes():
    raw = (FIXTURES / "nimbrel_orders_PO169603.edi").read_bytes()
    ident = extract_unb_identity(raw)
    assert ident["original_ref"] == "12341"


def test_extract_unb_identity_raises_without_unb():
    with pytest.raises(EdifactError):
        extract_unb_identity("UNH+1+ORDERS:D:01B:UN'BGM+220'UNT+2+1'")


def test_extract_unb_identity_feeds_build_contrl_directly():
    """The dict shape returned by extract_unb_identity is exactly what
    build_contrl needs for original_ref/original_sender_*/original_recipient_*
    (date/time are the ORIGINAL interchange's — generate_contrl overrides them
    with this CONTRL's own prep timestamp when it has one)."""
    ident = extract_unb_identity(_load("nimbrel_orders_PO169603.edi"))
    out = build_contrl(ident, supplier_gln="SUPPLIER_GLN", ctrl_ref=88301, msg_ref=1)
    parsed = parse_contrl(out)
    assert parsed["original_ref"] == "12341"
    assert parsed["original_sender_id"] == "NIMBREL"
    assert parsed["original_recipient_id"] == "SUPPLIER_GLN"


# --- round-trip: build an ack for the fixture's original interchange, re-parse it ---
def test_build_then_parse_roundtrip_correlates():
    # The inbound fixture acked interchange 72 (supplier->Nimbrel). If WE instead
    # received an interchange 72 from Nimbrel and built our own ack, the UCI ref
    # must still correlate to 72.
    payload = {
        "original_ref": "72",
        "original_sender_id": "NIMBREL",
        "original_sender_qual": "ZZZ",
        "original_recipient_id": "SUPPLIER_GLN",
        "original_recipient_qual": "14",
        "date": "200928",
        "time": "1030",
    }
    out = build_contrl(payload, ctrl_ref=99102, msg_ref=1)
    parsed = parse_contrl(out)
    assert parsed["original_ref"] == "72"
    assert parsed["action"] == "8"
    assert parsed["original_sender_id"] == "NIMBREL"
    assert parsed["original_recipient_id"] == "SUPPLIER_GLN"
