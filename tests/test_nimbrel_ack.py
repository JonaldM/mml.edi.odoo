"""Pure tests for NimbrelParser.generate_ack (review -> ORDRSP adapter).

The review/sale-order are duck-typed namespaces, so no Odoo env is needed.
Run: pytest tests/test_nimbrel_ack.py -q
"""
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from mml_edi.parsers.nimbrel import NimbrelParser
from mml_edi.parsers import nimbrel_edifact as edifact

FIXTURES = Path(__file__).parent / "fixtures"
ORDERS = (FIXTURES / "nimbrel_orders_PO169603.edi").read_text(encoding="iso-8859-1")


def _review(state="auto_approved", shortfall=0.0, committed=2.0):
    sol = NS(edi_line_number=1, product_uom_qty=committed, price_unit=132.44,
             edi_qty_shortfall=shortfall, name="Product Description",
             product_id=NS(default_code="5101000"))
    return NS(
        name="POR-1", customer_po_number="PO169603", store_code="12345",
        state=state, trading_partner_id=NS(code="V1058"),
        edi_raw_data=ORDERS, sale_order_id=NS(order_line=[sol]),
    )


def _segs(review):
    out = NimbrelParser().generate_ack(review)
    assert isinstance(out, bytes)
    _, segs = edifact.tokenize(out.decode("latin-1"))
    return segs


def _seg(segs, tag):
    return [s for s in segs if s.tag == tag]


def test_accepted_ack_is_valid_and_shaped():
    segs = _segs(_review())
    assert edifact.validate_interchange(segs) is True
    bgm = _seg(segs, "BGM")[0]
    assert bgm.comp(0, 0) == "231" and bgm.comp(2, 0) == "29"   # response, accepted
    assert _seg(segs, "RFF")[0].comp(0, 1) == "PO169603"   # RFF+ON:<po> (one composite)
    lin = _seg(segs, "LIN")[0]
    assert lin.comp(1, 0) == "5"                                 # accepted
    pia = _seg(segs, "PIA")
    assert pia[0].comp(1, 0) == "122134" and pia[0].comp(1, 1) == "IN"   # ISC
    assert pia[1].comp(1, 0) == "5101000" and pia[1].comp(1, 1) == "SA"  # MML
    qty113 = [q for q in _seg(segs, "QTY") if q.comp(0, 0) == "113"][0]
    assert qty113.comp(0, 1) == "2"                             # committed in full


def test_rejected_ack_sets_action_7_and_reason():
    segs = _segs(_review(state="rejected", committed=0.0))
    assert _seg(segs, "BGM")[0].comp(2, 0) == "27"             # rejected/cancelled
    assert _seg(segs, "LIN")[0].comp(1, 0) == "7"
    assert _seg(segs, "FTX")[0].comp(0, 0) == "LIN"            # mandatory reason present
    qty113 = [q for q in _seg(segs, "QTY") if q.comp(0, 0) == "113"][0]
    assert qty113.comp(0, 1) == "0"
    assert edifact.validate_interchange(segs) is True


def test_shortfall_sets_action_3_changed():
    segs = _segs(_review(shortfall=1.0, committed=1.0))
    assert _seg(segs, "BGM")[0].comp(2, 0) == "4"             # changed
    assert _seg(segs, "LIN")[0].comp(1, 0) == "3"
    qty113 = [q for q in _seg(segs, "QTY") if q.comp(0, 0) == "113"][0]
    assert qty113.comp(0, 1) == "1"                           # ordered 2 - shortfall 1


# --- build_outbound partner-dispatched seam ---
def test_build_outbound_routes_known_types_to_their_builders():
    p = NimbrelParser()
    for mt in ("ORDRSP", "DESADV", "INVOIC", "CONTRL"):
        try:
            p.build_outbound(mt, {})
        except NotImplementedError:
            pytest.fail("%s must route to its builder, not the dispatch default" % mt)
        except Exception:
            pass  # builder-level error on an empty payload is fine — routing happened


def test_build_outbound_rejects_unknown_type():
    with pytest.raises(NotImplementedError):
        NimbrelParser().build_outbound("APERAK", {})


# --- AN-05 / C5: generate_ack refuses cleanly for a cancellation review ---

def test_generate_ack_refuses_cancellation_via_document_type():
    from mml_edi.parsers.nimbrel import CancellationAckRefused
    review = _review()
    review.document_type = "cancellation"
    with pytest.raises(CancellationAckRefused):
        NimbrelParser().generate_ack(review)


def test_generate_ack_refuses_cancellation_via_change_summary_marker():
    """Belt-and-braces: also recognises the production CANCELLATION_MARKER
    prefix models/edi_processor.py writes onto change_summary, in case a
    caller passes a review namespace that carries that field instead of
    document_type='cancellation'."""
    from mml_edi.parsers.nimbrel import CancellationAckRefused
    review = _review()
    review.change_summary = "EDI-CANCELLATION: PO PO169603 cancelled by customer"
    with pytest.raises(CancellationAckRefused):
        NimbrelParser().generate_ack(review)


def test_generate_ack_still_works_for_ordinary_change_order():
    """document_type='change_order' (not 'cancellation') must NOT trip the guard."""
    review = _review()
    review.document_type = "change_order"
    out = NimbrelParser().generate_ack(review)
    assert isinstance(out, bytes)


def test_cancellation_ack_refused_is_an_edi_parse_error():
    """CancellationAckRefused subclasses EDIParseError so any generic
    except EDIParseError handler upstream still catches it."""
    from mml_edi.parsers.nimbrel import CancellationAckRefused
    from mml_edi.parsers.base_parser import EDIParseError
    assert issubclass(CancellationAckRefused, EDIParseError)
