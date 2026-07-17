"""Pure tests for AnimatesParser.generate_contrl (C4 processor-facing entry point).

No Odoo env — ``partner`` is duck-typed (a plain namespace implementing
get_unb_sender()/get_unb_recipient(), no .env). Run:
    pytest tests/test_animates_contrl_generate.py -q

Contract (C4): generate_contrl(raw_text: str, partner) -> bytes, called by
models/edi_processor.py::_emit_inbound_contrl(partner, file_hash) as
``parser.generate_contrl(raw_text, partner)``.
"""
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from mml_edi.parsers.animates import AnimatesParser
from mml_edi.parsers import animates_edifact as edifact
from mml_edi.parsers.animates_contrl import parse_contrl

FIXTURES = Path(__file__).parent / "fixtures"
ORDERS_RAW = (FIXTURES / "animates_orders_PO169603.edi").read_text(encoding="iso-8859-1")


def _partner(sender_id="9419416000008", sender_qual="ZZZ", environment="test"):
    return NS(
        get_unb_sender=lambda: (sender_id, sender_qual),
        get_unb_recipient=lambda: (
            ("TST1ANIMATES", "ZZZ") if environment == "test" else ("ANIMATES", "ZZZ")
        ),
    )


def test_generate_contrl_returns_bytes():
    out = AnimatesParser().generate_contrl(ORDERS_RAW, _partner())
    assert isinstance(out, bytes)


def test_generate_contrl_accepts_bytes_raw_text():
    out = AnimatesParser().generate_contrl(ORDERS_RAW.encode("iso-8859-1"), _partner())
    assert isinstance(out, bytes)


def test_generate_contrl_validates_as_interchange():
    # C1 default identity: edi_sender_id with qualifier ZZZ (portal
    # test-mailbox convention) — both UNB parties ZZZ-qualified, which
    # validate_interchange accepts per the CONTRL MIG.
    out = AnimatesParser().generate_contrl(ORDERS_RAW, _partner())
    _, segs = edifact.tokenize(out.decode("latin-1"))
    assert edifact.validate_interchange(segs) is True


def test_generate_contrl_validates_with_gln_fallback_sender():
    # GLN-fallback identity (qualifier 14) remains valid alongside ZZZ.
    out = AnimatesParser().generate_contrl(
        ORDERS_RAW, _partner(sender_id="9419416000008", sender_qual="14"))
    _, segs = edifact.tokenize(out.decode("latin-1"))
    assert edifact.validate_interchange(segs) is True


def test_generate_contrl_envelope_is_supplier_to_partner_recipient():
    """Outbound CONTRL envelope: OUR real identity -> the partner's
    environment-correct recipient (TST1ANIMATES in test, ANIMATES in prod)."""
    out = AnimatesParser().generate_contrl(ORDERS_RAW, _partner(environment="test"))
    _, segs = edifact.tokenize(out.decode("latin-1"))
    unb = [s for s in segs if s.tag == "UNB"][0]
    assert unb.elements[1] == ["9419416000008", "ZZZ"]
    assert unb.elements[2] == ["TST1ANIMATES", "ZZZ"]


def test_generate_contrl_envelope_prod_uses_animates_recipient():
    out = AnimatesParser().generate_contrl(ORDERS_RAW, _partner(environment="prod"))
    _, segs = edifact.tokenize(out.decode("latin-1"))
    unb = [s for s in segs if s.tag == "UNB"][0]
    assert unb.elements[2] == ["ANIMATES", "ZZZ"]


def test_generate_contrl_uci_echoes_original_interchange_verbatim():
    """UCI parties = the ORIGINAL inbound interchange's UNB sender/recipient
    (from ORDERS_RAW: ANIMATES:ZZZ -> SUPPLIER_GLN:14), the reverse of this
    CONTRL's own envelope."""
    out = AnimatesParser().generate_contrl(ORDERS_RAW, _partner())
    parsed = parse_contrl(out)
    assert parsed["original_ref"] == "12341"          # ORDERS_RAW's own UNB ctrl ref
    assert parsed["original_sender_id"] == "ANIMATES"
    assert parsed["original_sender_qual"] == "ZZZ"
    assert parsed["original_recipient_id"] == "SUPPLIER_GLN"
    assert parsed["original_recipient_qual"] == "14"
    assert parsed["action"] == "8"                     # interchange received


def test_generate_contrl_uses_a_non_placeholder_ctrl_ref():
    """require_real=True is always set — must never emit one of build_unb's
    known worked-example sentinels (12341/99101/78401)."""
    out = AnimatesParser().generate_contrl(ORDERS_RAW, _partner())
    _, segs = edifact.tokenize(out.decode("latin-1"))
    unb = [s for s in segs if s.tag == "UNB"][0]
    assert unb.comp(4, 0) not in ("12341", "99101", "78401")


def test_generate_contrl_raises_on_malformed_raw_text():
    from mml_edi.parsers.animates_edifact import EdifactError
    with pytest.raises(EdifactError):
        AnimatesParser().generate_contrl("not an edifact interchange", _partner())


def test_generate_contrl_calls_partner_identity_hooks_not_hardcoded():
    """A different partner identity produces a different envelope — proves the
    payload isn't silently defaulting to SUPPLIER_GLN/ANIMATES placeholders."""
    out1 = AnimatesParser().generate_contrl(ORDERS_RAW, _partner(sender_id="AAA111"))
    out2 = AnimatesParser().generate_contrl(ORDERS_RAW, _partner(sender_id="BBB222"))
    _, segs1 = edifact.tokenize(out1.decode("latin-1"))
    _, segs2 = edifact.tokenize(out2.decode("latin-1"))
    unb1 = [s for s in segs1 if s.tag == "UNB"][0]
    unb2 = [s for s in segs2 if s.tag == "UNB"][0]
    assert unb1.comp(1, 0) == "AAA111"
    assert unb2.comp(1, 0) == "BBB222"
