"""Pure-Python tests for Nimbrel identity + envelope (Wave1-A).

Covers the C1/C2/C3 shared contracts:
  - edi.trading.partner.get_unb_sender() / get_unb_recipient() (duck-typed,
    no Odoo env — see test_nimbrel_identity_odoo.py for the DB-backed
    sequence/env-switch coverage)
  - nimbrel_edifact.build_unb() explicit-identity + require_real guard
  - build_unb_for_partner() wiring get_unb_sender/get_unb_recipient into the
    UNB segment, and UNZ control-ref echo

No Odoo env. Run: pytest tests/test_nimbrel_identity.py -q
"""
import pytest

from odoo.exceptions import UserError

from mml_edi.models.edi_trading_partner import EDITradingPartner
from mml_edi.parsers.nimbrel_edifact import (
    build_unb, build_unb_for_partner, build_unz, EdifactError,
)


# --- fake partner (duck-typed; mirrors the real edi.trading.partner fields) ---

class _FakePartner:
    def __init__(self, edi_sender_id=None, edi_sender_qualifier="ZZZ",
                 supplier_gln=None, environment="production",
                 unb_recipient_id="NIMBREL",
                 unb_recipient_test_id="TST1NIMBREL",
                 unb_recipient_qualifier=None):
        self.edi_sender_id = edi_sender_id
        self.edi_sender_qualifier = edi_sender_qualifier
        self.supplier_gln = supplier_gln
        self.environment = environment
        # Per-partner counterparty mailbox overrides. Set to fictional values
        # here on purpose: the module DEFAULTS are the real VAN-provisioned
        # mailbox ids (wire routing data), so fixtures configure their own
        # rather than asserting on the shipped constants.
        self.unb_recipient_id = unb_recipient_id
        self.unb_recipient_test_id = unb_recipient_test_id
        self.unb_recipient_qualifier = unb_recipient_qualifier
        self.name = "Nimbrel NZ"

    def ensure_one(self):
        pass

    # Bind the real unbound methods so we exercise the actual implementation,
    # not a re-description of it.
    get_unb_sender = EDITradingPartner.get_unb_sender
    get_unb_recipient = EDITradingPartner.get_unb_recipient


# --- get_unb_sender() -------------------------------------------------------

def test_get_unb_sender_prefers_explicit_sender_id():
    p = _FakePartner(edi_sender_id="0200000000004T", edi_sender_qualifier="ZZZ",
                      supplier_gln="0200000000004")
    assert p.get_unb_sender() == ("0200000000004T", "ZZZ")


def test_get_unb_sender_falls_back_to_gln_with_qualifier_14():
    p = _FakePartner(supplier_gln="0200000000004")
    assert p.get_unb_sender() == ("0200000000004", "14")


def test_get_unb_sender_defaults_qualifier_to_zzz_when_missing():
    p = _FakePartner(edi_sender_id="0200000000004", edi_sender_qualifier=None)
    assert p.get_unb_sender() == ("0200000000004", "ZZZ")


def test_get_unb_sender_raises_when_unconfigured():
    p = _FakePartner()
    with pytest.raises(UserError):
        p.get_unb_sender()


# --- get_unb_recipient() -----------------------------------------------------

def test_get_unb_recipient_production():
    p = _FakePartner(environment="production")
    assert p.get_unb_recipient() == ("NIMBREL", "ZZZ")


def test_get_unb_recipient_test_uses_tst1nimbrel():
    p = _FakePartner(environment="test")
    assert p.get_unb_recipient() == ("TST1NIMBREL", "ZZZ")


def test_get_unb_recipient_falls_back_to_module_defaults_when_unset():
    """A partner row with blank mailbox fields (the state of every existing
    row after upgrade) must fall back to the shipped VAN mailbox ids, NOT to
    a blank/placeholder — a wrong value here is a total inbound EDI outage
    plus an outbound misroute."""
    from mml_edi.parsers.nimbrel_edifact import (
        DEFAULT_RECIPIENT_ID, DEFAULT_RECIPIENT_QUALIFIER,
        DEFAULT_RECIPIENT_TEST_ID,
    )
    prod = _FakePartner(environment="production", unb_recipient_id=None,
                        unb_recipient_test_id=None)
    assert prod.get_unb_recipient() == (
        DEFAULT_RECIPIENT_ID, DEFAULT_RECIPIENT_QUALIFIER)

    test = _FakePartner(environment="test", unb_recipient_id=None,
                        unb_recipient_test_id=None)
    assert test.get_unb_recipient() == (
        DEFAULT_RECIPIENT_TEST_ID, DEFAULT_RECIPIENT_QUALIFIER)


# --- build_unb(): backward-compatible defaults still work -------------------

def test_build_unb_default_qualifiers_unchanged():
    """Existing DESADV/INVOIC/CONTRL positional call sites must be unaffected."""
    from mml_edi.parsers.nimbrel_edifact import (
        DEFAULT_RECIPIENT_ID, DEFAULT_RECIPIENT_QUALIFIER,
    )
    unb = build_unb("0200000000011", 12341, "200916", "1030")
    assert unb.elements[1] == ["0200000000011", "14"]
    assert unb.elements[2] == [DEFAULT_RECIPIENT_ID, DEFAULT_RECIPIENT_QUALIFIER]


def test_build_unb_explicit_qualifiers_override_defaults():
    unb = build_unb("0200000000004T", 55, "260703", "0900",
                     recipient="TST1NIMBREL",
                     sender_qualifier="ZZZ", recipient_qualifier="ZZZ")
    assert unb.elements[1] == ["0200000000004T", "ZZZ"]
    assert unb.elements[2] == ["TST1NIMBREL", "ZZZ"]


# --- build_unb(require_real=True) guard --------------------------------------

def test_require_real_rejects_placeholder_sender():
    with pytest.raises(EdifactError):
        build_unb("SUPPLIER_GLN", 999, "260703", "0900", require_real=True)


def test_require_real_rejects_placeholder_ctrl_ref():
    with pytest.raises(EdifactError):
        build_unb("0200000000004T", 12341, "260703", "0900", require_real=True)


def test_require_real_rejects_frozen_worked_example_date():
    with pytest.raises(EdifactError):
        build_unb("0200000000004T", 42, "200916", "0900", require_real=True)


def test_require_real_accepts_genuinely_real_values():
    unb = build_unb("0200000000004T", 42, "260703", "0900", require_real=True,
                     recipient="TST1NIMBREL")
    assert unb.elements[1] == ["0200000000004T", "14"]  # default sender qualifier still 14
    assert unb.elements[4] == ["42"]


def test_require_real_false_by_default_pure_tests_unaffected():
    """require_real defaults False — every existing pure-test call site
    (which passes the documented SUPPLIER_GLN/12341/200916 sentinels on
    purpose) keeps working without opting in to the guard."""
    unb = build_unb("SUPPLIER_GLN", 12341, "200916", "0730")
    assert unb.elements[1] == ["SUPPLIER_GLN", "14"]


# --- build_unb_for_partner(): the production entry point --------------------

def test_build_unb_for_partner_test_environment():
    p = _FakePartner(edi_sender_id="0200000000004T", environment="test")
    unb = build_unb_for_partner(p, 101, "260703", "1415")
    assert unb.elements[1] == ["0200000000004T", "ZZZ"]
    assert unb.elements[2] == ["TST1NIMBREL", "ZZZ"]
    assert unb.elements[4] == ["101"]


def test_build_unb_for_partner_production_environment():
    p = _FakePartner(edi_sender_id="0200000000004", environment="production")
    unb = build_unb_for_partner(p, 102, "260703", "1415")
    assert unb.elements[1] == ["0200000000004", "ZZZ"]
    assert unb.elements[2] == ["NIMBREL", "ZZZ"]


def test_build_unb_for_partner_raises_when_partner_unconfigured():
    """The production path can never silently ship a placeholder identity:
    an unconfigured partner raises rather than defaulting to SUPPLIER_GLN."""
    p = _FakePartner()  # no edi_sender_id, no supplier_gln
    with pytest.raises(UserError):
        build_unb_for_partner(p, 103, "260703", "1415")


def test_build_unb_for_partner_control_ref_echoed_in_unz():
    """UNZ 0020 must match the UNB 0020 control ref that was actually used —
    the interchange control ref invariant validate_interchange() checks."""
    p = _FakePartner(edi_sender_id="0200000000004T", environment="test")
    ctrl_ref = 777
    unb = build_unb_for_partner(p, ctrl_ref, "260703", "0900")
    unz = build_unz(1, ctrl_ref)
    assert unb.comp(4, 0) == unz.comp(1, 0) == "777"


def test_build_unb_for_partner_uses_require_real_guard():
    """build_unb_for_partner always runs with require_real=True — a caller
    cannot accidentally pass a sentinel control ref through the partner path."""
    p = _FakePartner(edi_sender_id="0200000000004T", environment="test")
    with pytest.raises(EdifactError):
        build_unb_for_partner(p, 12341, "260703", "0900")  # 12341 is a sentinel
