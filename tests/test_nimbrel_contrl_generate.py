"""Pure tests for NimbrelParser.generate_contrl (C4 processor-facing entry point).

No Odoo env — ``partner`` is duck-typed (a plain namespace implementing
get_unb_sender()/get_unb_recipient(), no .env). Run:
    pytest tests/test_nimbrel_contrl_generate.py -q

Contract (C4): generate_contrl(raw_text: str, partner) -> bytes, called by
models/edi_processor.py::_emit_inbound_contrl(partner, file_hash) as
``parser.generate_contrl(raw_text, partner)``.
"""
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from mml_edi.parsers.nimbrel import NimbrelParser
from mml_edi.parsers import nimbrel_edifact as edifact
from mml_edi.parsers.nimbrel_contrl import parse_contrl

FIXTURES = Path(__file__).parent / "fixtures"
ORDERS_RAW = (FIXTURES / "nimbrel_orders_PO169603.edi").read_text(encoding="iso-8859-1")


def _partner(sender_id="0200000000004", sender_qual="ZZZ", environment="test"):
    return NS(
        get_unb_sender=lambda: (sender_id, sender_qual),
        get_unb_recipient=lambda: (
            ("TST1NIMBREL", "ZZZ") if environment == "test" else ("NIMBREL", "ZZZ")
        ),
    )


def test_generate_contrl_returns_bytes():
    out = NimbrelParser().generate_contrl(ORDERS_RAW, _partner())
    assert isinstance(out, bytes)


def test_generate_contrl_accepts_bytes_raw_text():
    out = NimbrelParser().generate_contrl(ORDERS_RAW.encode("iso-8859-1"), _partner())
    assert isinstance(out, bytes)


def test_generate_contrl_validates_as_interchange():
    # C1 default identity: edi_sender_id with qualifier ZZZ (portal
    # test-mailbox convention) — both UNB parties ZZZ-qualified, which
    # validate_interchange accepts per the CONTRL MIG.
    out = NimbrelParser().generate_contrl(ORDERS_RAW, _partner())
    _, segs = edifact.tokenize(out.decode("latin-1"))
    assert edifact.validate_interchange(segs) is True


def test_generate_contrl_validates_with_gln_fallback_sender():
    # GLN-fallback identity (qualifier 14) remains valid alongside ZZZ.
    out = NimbrelParser().generate_contrl(
        ORDERS_RAW, _partner(sender_id="0200000000004", sender_qual="14"))
    _, segs = edifact.tokenize(out.decode("latin-1"))
    assert edifact.validate_interchange(segs) is True


def test_generate_contrl_envelope_is_supplier_to_partner_recipient():
    """Outbound CONTRL envelope: OUR real identity -> the partner's
    environment-correct recipient (TST1NIMBREL in test, NIMBREL in prod)."""
    out = NimbrelParser().generate_contrl(ORDERS_RAW, _partner(environment="test"))
    _, segs = edifact.tokenize(out.decode("latin-1"))
    unb = [s for s in segs if s.tag == "UNB"][0]
    assert unb.elements[1] == ["0200000000004", "ZZZ"]
    assert unb.elements[2] == ["TST1NIMBREL", "ZZZ"]


def test_generate_contrl_envelope_prod_uses_nimbrel_recipient():
    out = NimbrelParser().generate_contrl(ORDERS_RAW, _partner(environment="prod"))
    _, segs = edifact.tokenize(out.decode("latin-1"))
    unb = [s for s in segs if s.tag == "UNB"][0]
    assert unb.elements[2] == ["NIMBREL", "ZZZ"]


def test_generate_contrl_uci_echoes_original_interchange_verbatim():
    """UCI parties = the ORIGINAL inbound interchange's UNB sender/recipient
    (from ORDERS_RAW: NIMBREL:ZZZ -> SUPPLIER_GLN:14), the reverse of this
    CONTRL's own envelope."""
    out = NimbrelParser().generate_contrl(ORDERS_RAW, _partner())
    parsed = parse_contrl(out)
    assert parsed["original_ref"] == "12341"          # ORDERS_RAW's own UNB ctrl ref
    assert parsed["original_sender_id"] == "NIMBREL"
    assert parsed["original_sender_qual"] == "ZZZ"
    assert parsed["original_recipient_id"] == "SUPPLIER_GLN"
    assert parsed["original_recipient_qual"] == "14"
    assert parsed["action"] == "8"                     # interchange received


def test_generate_contrl_uses_a_non_placeholder_ctrl_ref():
    """require_real=True is always set — must never emit one of build_unb's
    known worked-example sentinels (12341/99101/78401)."""
    out = NimbrelParser().generate_contrl(ORDERS_RAW, _partner())
    _, segs = edifact.tokenize(out.decode("latin-1"))
    unb = [s for s in segs if s.tag == "UNB"][0]
    assert unb.comp(4, 0) not in ("12341", "99101", "78401")


def test_generate_contrl_raises_on_malformed_raw_text():
    from mml_edi.parsers.nimbrel_edifact import EdifactError
    with pytest.raises(EdifactError):
        NimbrelParser().generate_contrl("not an edifact interchange", _partner())


def test_generate_contrl_calls_partner_identity_hooks_not_hardcoded():
    """A different partner identity produces a different envelope — proves the
    payload isn't silently defaulting to SUPPLIER_GLN/NIMBREL placeholders."""
    out1 = NimbrelParser().generate_contrl(ORDERS_RAW, _partner(sender_id="AAA111"))
    out2 = NimbrelParser().generate_contrl(ORDERS_RAW, _partner(sender_id="BBB222"))
    _, segs1 = edifact.tokenize(out1.decode("latin-1"))
    _, segs2 = edifact.tokenize(out2.decode("latin-1"))
    unb1 = [s for s in segs1 if s.tag == "UNB"][0]
    unb2 = [s for s in segs2 if s.tag == "UNB"][0]
    assert unb1.comp(1, 0) == "AAA111"
    assert unb2.comp(1, 0) == "BBB222"
