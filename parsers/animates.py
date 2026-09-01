"""Animates (NZ) EDI parser — UN/EDIFACT D.01B over SPS Commerce.

Inbound: ORDERS -> ParsedOrder (becomes a sale.order in Odoo).
Outbound: generate_ack -> ORDRSP (built in B3, see animates_ordrsp.py).

Item identity (per the MIG + octo review):
  PIA+5+<isc>:IN   = Animates' item code (ISC)  -> buyer_article_no
  PIA+1+<code>:SA  = MML's article number       -> product_code + vendor_code
The processor cascade (_find_product) then matches product_code/vendor_code
(default_code) and falls back to buyer_article_no (supplier_sku / supplierinfo),
so a product keyed by MML's code OR the Animates ISC resolves either way.
"""
from datetime import date

from .base_parser import BaseEDIParser, ParsedOrder, ParsedOrderLine, EDIParseError
from . import animates_edifact as edifact

# BGM 1225 message function code -> our document_type
_BGM_NEW = {"9"}            # 9 = original
_BGM_CHANGE = {"4", "5"}    # 4 = change, 5 = replace
_BGM_CANCEL = {"1"}         # 1 = cancellation (message carries no order lines)


def _parse_dtm(seg):
    """DTM+<qual>:<YYYYMMDD>:<102> -> (qualifier, date|None)."""
    qual = seg.comp(0, 0)
    val = seg.comp(0, 1)
    d = None
    if val and len(val) == 8 and val.isdigit():
        d = date(int(val[0:4]), int(val[4:6]), int(val[6:8]))
    return qual, d


class AnimatesParser(BaseEDIParser):

    def parse_file(self, raw_content, trading_partner) -> list:
        text = raw_content.decode("iso-8859-1") if isinstance(raw_content, (bytes, bytearray)) else raw_content
        _, segments = edifact.tokenize(text)
        if not any(s.tag == "UNH" for s in segments):
            raise EDIParseError("No UNH segment — not a valid EDIFACT interchange")

        orders = []
        cur = None     # dict accumulating the current message
        line = None    # dict accumulating the current LIN group

        def _flush_line():
            nonlocal line
            if line is not None and cur is not None:
                cur["lines"].append(line)
            line = None

        def _flush_order():
            nonlocal cur
            _flush_line()
            if cur is not None and cur.get("po_number"):
                orders.append(cur)
            cur = None

        for seg in segments:
            t = seg.tag
            if t == "UNH":
                _flush_order()
                cur = {"po_number": None, "order_date": None, "delivery_date": None,
                       "store_code": None, "doc_type": "new_order", "lines": []}
            elif cur is None:
                continue
            elif t == "BGM":
                cur["po_number"] = seg.comp(1, 0)
                func = seg.comp(2, 0)
                if func in _BGM_CANCEL:
                    # C5 / AN-05: cancellation carries no order lines (LIN SG is
                    # omitted per the MIG — scenario 3B). document_type maps
                    # DIRECTLY to "cancellation" so the processor's
                    # _process_cancellation routing (keyed on ParsedOrder.
                    # document_type) fires without an extra flag to keep in sync.
                    cur["doc_type"] = "cancellation"
                elif func in _BGM_CHANGE:
                    cur["doc_type"] = "change_order"
                else:
                    cur["doc_type"] = "new_order"
            elif t == "DTM":
                qual, d = _parse_dtm(seg)
                if qual == "137":
                    cur["order_date"] = d
                elif qual in ("2", "63", "64"):  # requested/delivery date
                    cur["delivery_date"] = d
            elif t == "NAD" and seg.comp(0, 0) == "ST":
                cur["store_code"] = seg.comp(1, 0)
            elif t == "LIN":
                _flush_line()
                line = {"line_number": int(seg.comp(0, 0) or 0), "product_code": None,
                        "buyer_article_no": None, "vendor_code": None, "description": "",
                        "quantity": 0.0, "uom": None, "carton_qty": None, "unit_price": 0.0}
            elif t == "PIA" and line is not None:
                code = seg.comp(1, 0)
                kind = seg.comp(1, 1)
                if kind == "IN":          # Animates' item code (ISC)
                    line["buyer_article_no"] = code
                elif kind == "SA":        # MML's article number
                    line["product_code"] = code
                    line["vendor_code"] = code
            elif t == "IMD" and line is not None:
                # IMD+F++:::Description -> description is the last component of C273
                comps = seg.elements[2] if len(seg.elements) > 2 else []
                line["description"] = next((c for c in reversed(comps) if c), "")
            elif t == "QTY" and line is not None:
                q = seg.comp(0, 0)
                val = seg.comp(0, 1)
                fval = float(val) if val else 0.0
                if q == "21":             # ordered quantity
                    line["quantity"] = fval
                    line["uom"] = seg.comp(0, 2) or None
                elif q == "59":           # number of consumer units per pack
                    line["carton_qty"] = fval
            elif t == "PRI" and line is not None:
                if seg.comp(0, 0) in ("AAA", "AAB", "AAE", ""):
                    v = seg.comp(0, 1)
                    if v:
                        line["unit_price"] = float(v)
            elif t == "UNT":
                _flush_order()
        _flush_order()

        result = []
        for o in orders:
            lines = [ParsedOrderLine(
                product_code=ln["product_code"] or ln["buyer_article_no"] or "",
                description=ln["description"],
                quantity=ln["quantity"],
                unit_price=ln["unit_price"],
                line_number=ln["line_number"],
                uom=ln["uom"],
                carton_qty=ln["carton_qty"],
                buyer_article_no=ln["buyer_article_no"],
                vendor_code=ln["vendor_code"],
            ) for ln in o["lines"]]
            result.append(ParsedOrder(
                po_number=o["po_number"],
                order_date=o["order_date"] or date.today(),
                lines=lines,
                store_code=o["store_code"],
                requested_delivery_date=o["delivery_date"],
                delivery_address_code=o["store_code"],
                document_type=o["doc_type"],
            ))
        return result

    def generate_ack(self, review_record) -> bytes:
        """Generate the ORDRSP (D.01B) acknowledgement for a processed review.

        Called by EDIOrderReview._queue_ack() after an order is approved/rejected.
        Re-parses the original ORDERS (review.edi_raw_data) to recover the ISC/MML
        codes + ordered qty per line, then folds in the SO's confirmed qty / shortfall
        / state to set each line's response action (5 accepted, 3 changed, 7 rejected).

        AN-05 / C5: refuses cleanly for a cancellation review (see
        CancellationAckRefused) — the pipeline must never build an ORDRSP for a
        cancellation (Testing Scenario Handbook 3B: CONTRL only). This is a
        belt-and-braces guard: models/edi_processor.py's own routing
        (_process_cancellation / CANCELLATION_MARKER) already keeps a
        cancellation review out of _queue_ack entirely, so this exception
        should never actually fire in the real pipeline — it exists so that
        ANY future/alternate caller of generate_ack cannot silently ship an
        ORDRSP for a cancelled PO.
        """
        if _is_cancellation_review(review_record):
            raise CancellationAckRefused(
                "generate_ack refused: review %r is a cancellation (BGM 1225=1) — "
                "cancellations are acknowledged with CONTRL only, never an ORDRSP "
                "(Testing Scenario Handbook scenario 3B)."
                % (getattr(review_record, "name", None)
                   or getattr(review_record, "customer_po_number", "?"))
            )
        from .animates_ordrsp import build_ordrsp
        payload = _review_to_ordrsp_payload(review_record)
        envelope_kwargs = _ordrsp_envelope_kwargs(review_record.trading_partner_id)
        return build_ordrsp(payload, **envelope_kwargs)

    def generate_contrl(self, raw_text, partner) -> bytes:
        """Build the mandatory CONTRL functional acknowledgement for one inbound
        interchange (C4 / AN-02). Called by
        models/edi_processor.py::_emit_inbound_contrl(partner, file_hash) with
        the review's stored ``edi_raw_data`` and the ``edi.trading.partner``
        record — this is the sole production entry point.

        Extracts the ORIGINAL interchange's UNB identity from ``raw_text``
        (extract_unb_identity) and echoes it verbatim into the UCI per the MIG
        (UCI parties = the original UNB sender/recipient, the REVERSE of this
        CONTRL's own envelope). The CONTRL envelope itself uses OUR real
        identity (partner.get_unb_sender()/get_unb_recipient() — C1) and a
        fresh control reference from the 'mml_edi.animates.interchange.ref'
        sequence (C2), built with require_real=True so a placeholder sender/
        ctrl-ref can never silently reach the wire (AN-01, mirrored from the
        ORDRSP production path).
        """
        from .animates_contrl import build_contrl, extract_unb_identity

        original = extract_unb_identity(raw_text)
        sender_id, sender_qual = partner.get_unb_sender()
        recipient_id, recipient_qual = partner.get_unb_recipient()

        payload = dict(original)
        # This CONTRL's OWN preparation date/time — NOT the original
        # interchange's (which may itself be a MIG worked-example sentinel
        # date that would trip require_real's placeholder-datetime guard).
        now_date, now_time = _contrl_now_yymmdd_hhmm()
        payload["date"] = now_date
        payload["time"] = now_time

        ctrl_ref = _next_interchange_ref(partner)
        return build_contrl(
            payload, supplier_gln=sender_id, ctrl_ref=ctrl_ref, msg_ref=1,
            sender_qualifier=sender_qual, recipient=recipient_id,
            recipient_qualifier=recipient_qual, require_real=True,
        )

    def build_outbound(self, msg_type, payload) -> bytes:
        """Partner-dispatched outbound builder for the non-ack messages.

        ``payload`` is the message-specific dict (see each builder's docstring),
        assembled upstream from a stock.picking (DESADV), account.move (INVOIC) or
        an inbound interchange (CONTRL). ORDRSP is normally emitted via generate_ack
        but is dispatchable here too for completeness.
        """
        mt = (msg_type or "").upper()
        if mt == "ORDRSP":
            from .animates_ordrsp import build_ordrsp
            return build_ordrsp(payload)
        if mt == "DESADV":
            from .animates_desadv import build_desadv
            return build_desadv(payload)
        if mt == "INVOIC":
            from .animates_invoic import build_invoic
            return build_invoic(payload)
        if mt == "CONTRL":
            from .animates_contrl import build_contrl
            return build_contrl(payload)
        raise NotImplementedError("Animates does not emit outbound %s" % msg_type)


class CancellationAckRefused(EDIParseError):
    """Raised by AnimatesParser.generate_ack when called on a cancellation
    review (AN-05 / C5). A cancellation is acknowledged with CONTRL only —
    never an ORDRSP — so any caller reaching generate_ack for one has a
    routing bug upstream; this makes that bug loud and specific rather than
    letting a generic EDIParseError (or worse, a fabricated ORDRSP) through.
    """
    pass


def _is_cancellation_review(review) -> bool:
    """True when ``review`` represents a cancellation (BGM 1225=1), duck-typed
    so both a real edi.order.review and a plain test namespace work.

    Checks, in order:
      1. ``document_type`` (ParsedOrder's own value, 'cancellation') — set
         directly on a review/namespace that mirrors ParsedOrder's field.
      2. models/edi_processor.py's CANCELLATION_MARKER prefix on
         ``change_summary`` — the actual production signal
         _process_cancellation writes (see edi_processor.py _is_cancellation_
         review), duplicated here as a string literal so this parser module
         has no import dependency on models/ (parsers must stay Odoo-free/
         pure — see base_parser.py docstring).
    """
    doc_type = getattr(review, "document_type", None)
    if doc_type == "cancellation":
        return True
    summary = getattr(review, "change_summary", None) or ""
    return summary.startswith("EDI-CANCELLATION:")


def _contrl_now_yymmdd_hhmm():
    """Return (YYMMDD, HHMM) for this CONTRL's own preparation timestamp.

    Uses odoo.fields.Datetime.now() (server/UTC-consistent with the rest of
    Odoo) when the Odoo runtime is importable; falls back to the plain
    datetime module otherwise (pure-test / no-Odoo-env path) so
    generate_contrl always has a real, non-placeholder "now" — never the
    original interchange's own (possibly worked-example-sentinel) date/time.
    """
    try:
        from odoo import fields as odoo_fields
        now = odoo_fields.Datetime.now()
    except Exception:
        from datetime import datetime
        now = datetime.now()
    return now.strftime("%y%m%d"), now.strftime("%H%M")


def _next_interchange_ref(partner):
    """Return the next Animates interchange control reference (C2 sequence
    'mml_edi.animates.interchange.ref'), or a fallback for the pure-test /
    no-env path.

    The fallback deliberately does NOT reuse any of build_unb's
    _PLACEHOLDER_CTRL_REFS sentinels (12341/99101/78401) — a caller testing
    generate_contrl with a duck-typed partner and require_real=True must get
    a value that actually passes validation, not silently trip the very
    guard AN-01 exists to enforce.
    """
    env = getattr(partner, "env", None)
    if env is None:
        return 500000001
    return env["ir.sequence"].sudo().next_by_code(
        "mml_edi.animates.interchange.ref"
    )


def _ordrsp_envelope_kwargs(partner) -> dict:
    """Build the build_ordrsp(...) envelope kwargs from a trading partner (AN-01).

    PRODUCTION path: when ``partner`` implements the real C1 identity contract
    (get_unb_sender/get_unb_recipient — a real edi.trading.partner record, or
    any duck-typed stand-in that mirrors it), forward its real identity plus a
    fresh interchange control reference (C2 sequence) with require_real=True —
    a placeholder can never silently reach the wire.

    PURE-TEST fallback: a partner without those methods (existing tests using
    a bare namespace, e.g. NS(code="V1058")) gets an EMPTY kwargs dict, so
    build_ordrsp falls back to its own documented worked-example defaults
    exactly as before this fix — no behaviour change for those call sites.
    """
    get_sender = getattr(partner, "get_unb_sender", None)
    get_recipient = getattr(partner, "get_unb_recipient", None)
    if not (callable(get_sender) and callable(get_recipient)):
        return {}
    sender_id, sender_qual = get_sender()
    recipient_id, recipient_qual = get_recipient()
    return {
        "supplier_gln": sender_id,
        "ctrl_ref": _next_interchange_ref(partner),
        "msg_ref": 1,
        "sender_qualifier": sender_qual,
        "recipient": recipient_id,
        "recipient_qualifier": recipient_qual,
        "require_real": True,
    }


def _qty_str(v) -> str:
    """Animates quantities are eaches (integers on the wire)."""
    try:
        return str(int(round(float(v))))
    except (TypeError, ValueError):
        return "0"


# Tolerance for float qty/price comparisons when detecting an operator
# correction (avoid flagging a "change" purely from binary float noise).
_QTY_EPS = 1e-6
_PRICE_EPS = 1e-4

_REASON_REJECTED_NO_SOL = (
    "Line not processed"  # SS-6 fail-closed default reason (FTX C108.4440)
)
_REASON_REJECTED_ON_REVIEW = "Rejected on review"
_REASON_REJECTED_ZERO_QTY = "Item out of stock"


def _gather_sol_by_line(review) -> dict:
    """Collect {edi_line_number: sale.order.line} across ALL sibling reviews of
    the same PO (mirrors briscoes_idoc._gather_confirmations — finding #11).

    A multi-store Animates interchange (or a partial-failure retry that split
    one PO across several review records) must not ACK another store's/
    sibling's lines as accepted-in-full just because THIS review's own SO
    doesn't carry them. Falls back to review.sale_order_id ALONE when no
    Odoo env is reachable (pure unit-test / duck-typed namespace path) —
    that is the existing, intended pure-test behaviour.
    """
    sol_by_line = {}

    def _index(so):
        for sol in getattr(so, "order_line", []) or []:
            n = getattr(sol, "edi_line_number", None)
            if n:
                sol_by_line[int(n)] = sol

    env = getattr(review, "env", None)
    partner = getattr(review, "trading_partner_id", None)
    po = getattr(review, "customer_po_number", None)
    if env is not None and partner is not None and po:
        try:
            siblings = env["edi.order.review"].search([
                ("trading_partner_id", "=", getattr(partner, "id", partner)),
                ("customer_po_number", "=", po),
            ]) or [review]
        except Exception:
            siblings = [review]
        for rev in siblings:
            _index(getattr(rev, "sale_order_id", None))
    else:
        _index(getattr(review, "sale_order_id", None))

    return sol_by_line


def _line_action_and_committed(sol, rejected: bool):
    """Decide (action, committed_qty, reason) for one ORDRSP line per the
    Testing Scenario Handbook scenarios 1/2/4A + SS-6 fail-closed default.

    Priority (first match wins):
      1. Whole-review rejection (state == 'rejected')                 -> 7
      2. SS-6: no matching SOL at all — never processed                -> 7
      3. Committed qty is zero/negative (fully short/OOS on review)    -> 7
      4. Stock shortfall (edi_qty_shortfall > 0)                       -> 3
      5. Operator quantity correction (live qty != edi_ordered_qty,
         no shortfall recorded)                                        -> 3
      6. Operator price correction (live price != edi-received price)  -> 3
      7. Otherwise: accepted without amendment                         -> 5
    """
    if rejected:
        return "7", 0.0, _REASON_REJECTED_ON_REVIEW

    if sol is None:
        # SS-6: fail CLOSED — a line MML never matched a product/SO-line for
        # must never be silently confirmed in full (inverse of the old bug).
        return "7", 0.0, _REASON_REJECTED_NO_SOL

    committed = float(getattr(sol, "product_uom_qty", 0) or 0)
    shortfall = float(getattr(sol, "edi_qty_shortfall", 0) or 0)

    if committed <= _QTY_EPS:
        return "7", 0.0, _REASON_REJECTED_ZERO_QTY

    if shortfall > _QTY_EPS:
        return "3", committed, None

    edi_ordered_qty = getattr(sol, "edi_ordered_qty", None)
    if edi_ordered_qty is not None and abs(committed - float(edi_ordered_qty)) > _QTY_EPS:
        # Operator corrected the qty directly on the SO line (no stock event
        # recorded) — still a change, not a silent full-acceptance.
        return "3", committed, None

    edi_price = getattr(sol, "edi_price", None)
    price_unit = getattr(sol, "price_unit", None)
    if edi_price is not None and price_unit is not None and \
            abs(float(price_unit) - float(edi_price)) > _PRICE_EPS:
        # Operator corrected the price — scenario 4A price-change case.
        return "3", committed, None

    return "5", committed, None


def _line_price(sol, ol) -> float:
    """Live (possibly operator-corrected) price when a SOL exists; otherwise
    fall back to the EDI-parsed price (SS-6 rejected-line-with-no-SOL path
    still needs SOME price on the wire — echo what was ordered)."""
    price = getattr(sol, "price_unit", None) if sol is not None else None
    if price is None:
        price = ol.unit_price
    return float(price or 0.0)


def _line_supplier_item(sol, ol):
    """MML article number for PIA+1:SA — live product's default_code when the
    line resolved to a product, else the EDI-parsed vendor code."""
    if sol is not None:
        product = getattr(sol, "product_id", None)
        code = getattr(product, "default_code", None)
        if code:
            return code
    return ol.vendor_code


def _review_to_ordrsp_payload(review) -> dict:
    """Map an edi.order.review (+ its sale.order, + sibling reviews of the
    same PO) to the build_ordrsp payload.

    Duck-typed (no Odoo import) so it is unit-testable with plain namespaces;
    the sibling-aggregation and real-envelope-identity paths additionally
    exercise review.env / trading_partner_id.get_unb_sender() when present.
    """
    from datetime import date

    partner = review.trading_partner_id
    rejected = getattr(review, "state", None) == "rejected"

    # Original ORDERS lines (ISC / MML / ordered qty / price / description).
    orig_lines = []
    requested = None
    raw = getattr(review, "edi_raw_data", None)
    if raw:
        try:
            data = raw if isinstance(raw, (bytes, bytearray)) else raw.encode("iso-8859-1")
            for order in AnimatesParser().parse_file(data, partner):
                orig_lines.extend(order.lines)
                requested = requested or order.requested_delivery_date
        except Exception:
            orig_lines = []

    # SO lines keyed by EDI line number, aggregated across sibling reviews of
    # this PO (finding #11) — never just this review's own (possibly empty
    # for a multi-store interchange) sale_order_id.
    sol_by_line = _gather_sol_by_line(review)

    # Any line that deviates from plain full-acceptance (action 5) — a change
    # (3) OR a per-line fail-closed/zero-qty rejection (7) while the WHOLE
    # review is not itself rejected — puts the whole PO response under BGM=4
    # ("accepted with changes"; per the MIG, 27/full-rejection is reserved for
    # review.state == 'rejected', where every line is forced to action 7 by
    # the `rejected` branch above and ack_code short-circuits to 27 instead).
    any_changed = False
    lines = []
    for ol in orig_lines:
        sol = sol_by_line.get(ol.line_number)
        ordered = float(ol.quantity or 0)
        action, committed, reason = _line_action_and_committed(sol, rejected)
        if action != "5":
            any_changed = True

        lines.append({
            "line_no": ol.line_number,
            "action": action,
            "buyer_item": ol.buyer_article_no or "",
            "supplier_item": _line_supplier_item(sol, ol) or None,
            "description": ol.description or (getattr(sol, "name", "") if sol is not None else "") or "",
            "qty_ordered": _qty_str(ordered),
            "qty_pack": _qty_str(ol.carton_qty) if ol.carton_qty else "1",
            "qty_committed": _qty_str(committed),
            "reason": reason,
            "price": "%.4f" % _line_price(sol, ol),
            "tax_rate": "15.00",  # NZ GST
        })

    today = date.today().strftime("%Y%m%d")
    req = requested.strftime("%Y%m%d") if requested else today

    # BGM 1225 selection from the live aggregate state (per the MIG + Testing
    # Scenario Handbook): whole-review rejection -> 27 (all lines forced to
    # action 7 above); any per-line change/fail-closed-reject -> 4; otherwise
    # every line is accepted without amendment -> 29.
    ack_code = "27" if rejected else ("4" if any_changed else "29")

    has_real_identity = callable(getattr(partner, "get_unb_recipient", None))
    buyer, _buyer_qual = partner.get_unb_recipient() if has_real_identity else ("ANIMATES", "ZZZ")
    supplier = getattr(partner, "animates_vendor_code", None) or getattr(partner, "code", "") or ""

    payload = {
        "po_response_no": getattr(review, "name", None) or getattr(review, "customer_po_number", "") or "",
        "ack_code": ack_code,
        "message_date": today,
        "requested_date": req,
        "po_number": getattr(review, "customer_po_number", "") or "",
        "buyer": buyer,
        "supplier": supplier,
        "ship_to": getattr(review, "store_code", "") or "",
        "currency": "NZD",
        "lines": lines,
    }
    if has_real_identity:
        # AN-01: this ORDRSP's own preparation timestamp, never the frozen
        # 2020 MIG worked-example UNB date/time build_ordrsp defaults to.
        now_date, now_time = _contrl_now_yymmdd_hhmm()
        payload["interchange"] = {"date": now_date, "time": now_time}
    return payload
