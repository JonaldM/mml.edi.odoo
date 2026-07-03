"""Pure tests for AnimatesParser._review_to_ordrsp_payload / generate_ack against
the go-live gate review's Testing Scenario Handbook acceptance criteria
(scenarios 1, 2, 4A) plus the AN-01/SS-6/sibling-aggregation/live-qty fixes.

Reviews and sale orders are duck-typed SimpleNamespace objects — no Odoo env
needed except where a test explicitly attaches a fake ``env`` to exercise the
sibling-aggregation / envelope-identity code paths.

Run: pytest tests/test_animates_ordrsp_live.py -q
"""
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from mml_edi.parsers.animates import AnimatesParser, _review_to_ordrsp_payload
from mml_edi.parsers import animates_edifact as edifact

FIXTURES = Path(__file__).parent / "fixtures"
ORDERS = (FIXTURES / "animates_orders_PO169603.edi").read_text(encoding="iso-8859-1")


def _partner(code="V1058", vendor_code="V1058", environment="test", sender_qual="ZZZ"):
    return NS(
        code=code,
        animates_vendor_code=vendor_code,
        get_unb_sender=lambda: ("9419416000008T", sender_qual),
        get_unb_recipient=lambda: (
            ("TST1ANIMATES", "ZZZ") if environment == "test" else ("ANIMATES", "ZZZ")
        ),
    )


def _sol(edi_line_number=1, product_uom_qty=2.0, price_unit=132.44,
         edi_qty_shortfall=0.0, edi_price=132.44, edi_ordered_qty=2.0,
         default_code="5101000", name="Product Description"):
    return NS(
        edi_line_number=edi_line_number, product_uom_qty=product_uom_qty,
        price_unit=price_unit, edi_qty_shortfall=edi_qty_shortfall,
        edi_price=edi_price, edi_ordered_qty=edi_ordered_qty,
        name=name, product_id=NS(default_code=default_code),
    )


def _review(state="auto_approved", sol=None, partner=None, raw=ORDERS,
            po="PO169603", store="12345"):
    sol = sol if sol is not None else _sol()
    return NS(
        name="POR-1", customer_po_number=po, store_code=store,
        state=state, trading_partner_id=partner or _partner(),
        edi_raw_data=raw, sale_order_id=NS(order_line=[sol]),
    )


def _segs_from_bytes(out):
    assert isinstance(out, bytes)
    _, segs = edifact.tokenize(out.decode("latin-1"))
    return segs


def _seg(segs, tag):
    return [s for s in segs if s.tag == tag]


# --- Scenario 1: Ship to Store — Accept in Full (BGM 29, LIN 1229=5) ---

def test_scenario1_accept_in_full():
    # Default partner = C1's preferred edi_sender_id identity (ZZZ-qualified
    # sender), so validate_interchange runs against the real production
    # both-sides-ZZZ envelope (legal per the CONTRL MIG).
    review = _review(sol=_sol(product_uom_qty=2.0, edi_qty_shortfall=0.0))
    out = AnimatesParser().generate_ack(review)
    segs = _segs_from_bytes(out)
    assert edifact.validate_interchange(segs) is True
    assert _seg(segs, "BGM")[0].comp(2, 0) == "29"
    assert _seg(segs, "LIN")[0].comp(1, 0) == "5"


# --- Scenario 2: Ship to Store — Reject in Full (BGM 27, LIN 1229=7, FTX mandatory) ---

def test_scenario2_reject_in_full():
    review = _review(state="rejected", sol=_sol())
    out = AnimatesParser().generate_ack(review)
    segs = _segs_from_bytes(out)
    assert _seg(segs, "BGM")[0].comp(2, 0) == "27"
    assert _seg(segs, "LIN")[0].comp(1, 0) == "7"
    ftx = _seg(segs, "FTX")
    assert ftx and ftx[0].comp(0, 0) == "LIN"
    qty113 = [q for q in _seg(segs, "QTY") if q.comp(0, 0) == "113"][0]
    assert qty113.comp(0, 1) == "0"


# --- Scenario 4A: qty change (stock shortfall) -> BGM 4, LIN 1229=3 ---

def test_scenario4a_quantity_change_from_shortfall():
    review = _review(sol=_sol(product_uom_qty=1.0, edi_qty_shortfall=1.0))
    out = AnimatesParser().generate_ack(review)
    segs = _segs_from_bytes(out)
    assert _seg(segs, "BGM")[0].comp(2, 0) == "4"
    assert _seg(segs, "LIN")[0].comp(1, 0) == "3"
    qty113 = [q for q in _seg(segs, "QTY") if q.comp(0, 0) == "113"][0]
    assert qty113.comp(0, 1) == "1"          # live committed qty, not a recompute


# --- Scenario 4A: price correction -> BGM 4, LIN 1229=3, corrected PRI ---

def test_scenario4a_price_correction():
    """Operator corrected sol.price_unit away from the EDI-received edi_price
    -> action 3 (changed) with the CORRECTED price on the wire, per the
    gate-review price-correction finding."""
    sol = _sol(price_unit=99.5000, edi_price=132.44, product_uom_qty=2.0,
               edi_qty_shortfall=0.0, edi_ordered_qty=2.0)
    review = _review(sol=sol)
    out = AnimatesParser().generate_ack(review)
    segs = _segs_from_bytes(out)
    assert _seg(segs, "BGM")[0].comp(2, 0) == "4"
    assert _seg(segs, "LIN")[0].comp(1, 0) == "3"
    pri = _seg(segs, "PRI")[0]
    assert pri.comp(0, 1) == "99.5000"       # corrected price, not the EDI one


# --- Scenario 4A: quantity correction (operator, no stock shortfall) -> action 3 ---

def test_scenario4a_operator_quantity_correction_no_shortfall():
    """product_uom_qty differs from edi_ordered_qty with edi_qty_shortfall == 0
    -> an OPERATOR correction (not a stock event) — still action 3/changed with
    the corrected QTY."""
    sol = _sol(product_uom_qty=5.0, edi_ordered_qty=2.0, edi_qty_shortfall=0.0)
    review = _review(sol=sol)
    out = AnimatesParser().generate_ack(review)
    segs = _segs_from_bytes(out)
    assert _seg(segs, "LIN")[0].comp(1, 0) == "3"
    qty113 = [q for q in _seg(segs, "QTY") if q.comp(0, 0) == "113"][0]
    assert qty113.comp(0, 1) == "5"


# --- Scenario 4A: rejection -> action 7 + FTX with a reason ---

def test_scenario4a_line_rejection_via_zero_committed():
    """A single line explicitly zeroed out (e.g. operator marks OOS on review
    without rejecting the WHOLE PO) still emits action 7 + mandatory FTX."""
    sol = _sol(product_uom_qty=0.0, edi_qty_shortfall=2.0, edi_ordered_qty=2.0)
    review = _review(sol=sol)
    out = AnimatesParser().generate_ack(review)
    segs = _segs_from_bytes(out)
    assert _seg(segs, "LIN")[0].comp(1, 0) == "7"
    ftx = _seg(segs, "FTX")
    assert ftx and ftx[0].comp(0, 0) == "LIN"
    assert ftx[0].comp(3, 0)   # a non-empty reason literal


# --- SS-6: fail-closed default when no SOL matches the EDI line ---

def test_ss6_missing_sol_defaults_to_rejected_not_accepted():
    """No SO line at all for this EDI line number (product_not_found style
    gap) -> the inverse of the old accepted-in-full default: action 7 +
    mandatory FTX, never a silent full-supply confirmation."""
    review = _review(sol=None)
    review.sale_order_id = NS(order_line=[])   # no SOL for line 1 at all
    out = AnimatesParser().generate_ack(review)
    segs = _segs_from_bytes(out)
    assert _seg(segs, "LIN")[0].comp(1, 0) == "7"
    ftx = _seg(segs, "FTX")
    assert ftx and ftx[0].comp(3, 0)
    qty113 = [q for q in _seg(segs, "QTY") if q.comp(0, 0) == "113"][0]
    assert qty113.comp(0, 1) == "0"


def test_ss6_missing_sale_order_entirely_defaults_to_rejected():
    """No linked sale_order_id at all — must still fail closed, not echo the
    ordered qty as accepted (the pure-mock echo-all path is for review data
    that legitimately cannot exist; here it CAN exist, it's just empty)."""
    review = _review(sol=None)
    review.sale_order_id = None
    out = AnimatesParser().generate_ack(review)
    segs = _segs_from_bytes(out)
    assert _seg(segs, "LIN")[0].comp(1, 0) == "7"


# --- BGM function selection from the live aggregate state ---

def test_bgm_all_accepted_unchanged_is_29():
    review = _review(sol=_sol(product_uom_qty=2.0, edi_qty_shortfall=0.0,
                               price_unit=132.44, edi_price=132.44))
    payload = _review_to_ordrsp_payload(review)
    assert payload["ack_code"] == "29"


def test_bgm_all_rejected_is_27():
    review = _review(state="rejected")
    payload = _review_to_ordrsp_payload(review)
    assert payload["ack_code"] == "27"


def test_bgm_any_change_is_4():
    review = _review(sol=_sol(product_uom_qty=1.0, edi_qty_shortfall=1.0))
    payload = _review_to_ordrsp_payload(review)
    assert payload["ack_code"] == "4"


def test_bgm_mixed_accept_and_missing_sol_is_4_not_27():
    """A SS-6 fail-closed per-line rejection (no matching SOL) is a CHANGE (4),
    not a full rejection (27) — 27 is reserved for review.state == 'rejected'
    (a deliberate whole-PO reject), never inferred from individual line
    outcomes. Covered together with
    test_ss6_missing_sol_defaults_to_rejected_not_accepted, which asserts the
    per-line action; this asserts the resulting BGM code specifically."""
    review = _review(sol=_sol(edi_line_number=1))
    review.sale_order_id = NS(order_line=[])   # no SOL for line 1 -> fail-closed
    payload = _review_to_ordrsp_payload(review)
    assert payload["ack_code"] == "4"
    assert payload["lines"][0]["action"] == "7"


# --- AN-01: envelope identity threaded from the partner (no placeholders) ---

def test_generate_ack_uses_real_envelope_identity_not_placeholders():
    review = _review(partner=_partner(vendor_code="V9999", environment="test"))
    out = AnimatesParser().generate_ack(review)
    segs = _segs_from_bytes(out)
    unb = _seg(segs, "UNB")[0]
    assert unb.elements[1] == ["9419416000008T", "ZZZ"]
    assert unb.elements[2] == ["TST1ANIMATES", "ZZZ"]
    assert unb.comp(4, 0) not in ("12341", "99101", "78401")


def test_generate_ack_prod_recipient_is_animates():
    review = _review(partner=_partner(environment="production"))
    out = AnimatesParser().generate_ack(review)
    segs = _segs_from_bytes(out)
    unb = _seg(segs, "UNB")[0]
    assert unb.elements[2][0] == "ANIMATES"


def test_generate_ack_supplier_nad_uses_animates_vendor_code_not_partner_code():
    """NAD+SU must carry the Animates-assigned vendor code (C1
    animates_vendor_code), not our internal partner.code."""
    review = _review(partner=_partner(code="INTERNAL-CODE-123", vendor_code="V1058"))
    out = AnimatesParser().generate_ack(review)
    segs = _segs_from_bytes(out)
    nad_su = [s for s in _seg(segs, "NAD") if s.comp(0, 0) == "SU"][0]
    assert nad_su.comp(1, 0) == "V1058"


def test_generate_ack_buyer_nad_derived_from_partner_recipient():
    review = _review(partner=_partner(environment="test"))
    out = AnimatesParser().generate_ack(review)
    segs = _segs_from_bytes(out)
    nad_by = [s for s in _seg(segs, "NAD") if s.comp(0, 0) == "BY"][0]
    assert nad_by.comp(1, 0) == "TST1ANIMATES"


# --- sibling-review aggregation across a multi-store interchange ---

class _FakeReviewModel:
    def __init__(self, rows):
        self._rows = rows

    def search(self, domain):
        out = self._rows
        for field, op, value in domain:
            out = [r for r in out
                   if getattr(getattr(r, field, None), "id", getattr(r, field, None)) == value]
        return out


class _FakeEnv:
    def __init__(self, models):
        self._models = models

    def __getitem__(self, name):
        return self._models[name]


def test_sibling_aggregation_across_interchange_reviews():
    """Payload builds sol_by_line from ALL sibling reviews of the SAME PO, not
    just review.sale_order_id — a multi-store interchange must not ACK other
    stores' lines as accepted-in-full."""
    partner = _partner()
    partner.id = 99
    sib_sol = _sol(edi_line_number=1, product_uom_qty=2.0, edi_qty_shortfall=0.0)
    self_review = _review(sol=None, partner=partner)
    self_review.id = 1
    self_review.sale_order_id = None  # THIS review's own SO has no matching line
    self_review.trading_partner_id = partner

    sibling = NS(
        id=2, trading_partner_id=partner, customer_po_number="PO169603",
        state="auto_approved", sale_order_id=NS(order_line=[sib_sol]),
    )

    self_review.env = _FakeEnv({
        "edi.order.review": _FakeReviewModel([self_review, sibling]),
    })

    payload = _review_to_ordrsp_payload(self_review)
    # The single EDI line (line 1) must be resolved from the SIBLING's SOL,
    # not fail-closed-rejected just because self_review's own SO is empty.
    assert payload["lines"][0]["action"] == "5"
    assert payload["lines"][0]["qty_committed"] == "2"


def test_sibling_aggregation_falls_back_to_self_when_no_env():
    """Pure duck-typed review with no .env attribute — cannot search siblings,
    falls back to review.sale_order_id alone (existing pure-test behaviour)."""
    review = _review(sol=_sol(product_uom_qty=2.0))
    payload = _review_to_ordrsp_payload(review)
    assert payload["lines"][0]["action"] == "5"
