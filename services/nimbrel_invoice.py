# mml.edi/services/nimbrel_invoice.py
"""account.move -> Nimbrel D.01B INVOIC builder (Wave2-E, AN-03 INVOIC half /
scenarios 1, 4B, 5A, 5B).

Pure-ish glue: turns a posted ``account.move`` (customer invoice) into the
``parsers.nimbrel_invoic.build_invoic`` payload dict and (optionally)
uploads it to the partner's EDI outbox. No new ORM models are introduced
here — this module is functions only, callable from wherever the eventual
account.move trigger lives (see "Dispatch seam" below).

Hard contract (gate review AN-03 INVOIC half / Testing Scenario Handbook
scenarios 4B/5A/5B, verbatim from Nimbrel_INVOIC.pdf p.20 RFF notes and the
MIG worked example at p.44/52): quantity invoiced (QTY 47) MUST equal
quantity SHIPPED for that line, never the quantity ORDERED on the SO. Shipped
quantity is read from the SAME source services.edi_service._pack_units_for_
nimbrel uses for the DESADV itself -- the picking's ``done`` stock.move
records linked via ``sale_line_id`` -- not re-derived from the sale order
line's ordered qty and not re-parsed out of a prior DESADV's logged text.
This makes scenario 5A (invoice only the 2 shipped lines, ISC3 absent) and
5B (the follow-up invoice for the remainder, ISC3 only) fall out of the same
function with no special-casing: a line with zero shipped qty is simply
omitted from the INVOIC.

GST semantics (see the corrected docstring in parsers/nimbrel_invoic.py):
MOA 128 is the EX-tax line/summary amount, MOA 369 is the GST amount, and
MOA 203 (line) / MOA 39 (summary) is the INCL-tax amount. This module reads
``account.move.line.price_subtotal`` (ex-tax) as the MOA 128 source and
``price_total`` (incl-tax) as the MOA 203/39 source, with MOA 369 computed as
their difference -- so if Odoo's own tax computation is inconsistent with
those two fields (e.g. a misconfigured tax), the discrepancy shows up as a
non-zero-but-wrong GST line rather than silently mismatching the MIG's own
128 + 369 == 203 invariant. Fail-CLOSED: a line with no resolvable GST tax
percentage raises rather than guessing a rate (house style: uncertain ->
reject, never silent accept).

Dispatch seam (per Wave2-E's brief -- edi_service.py is out of scope for
this wave): services/edi_service.py's ``on_3pl_despatch_confirmed`` /
``_dispatch_nimbrel`` pair is picking-triggered (DESADV only) and has no
account.move equivalent yet. The natural mirror -- an
``on_invoice_posted(event)`` method on ``EDIService`` that routes per-partner
exactly like the despatch hook does -- does not exist. Until that hook is
added, ``generate_and_upload_invoic()`` below is the complete, directly
callable unit (build payload -> render bytes -> upload -> attach -> log) that
such a hook would call with (env, move, partner); it takes the same
(env, move, partner) shape ``_dispatch_nimbrel`` takes (picking, sale_order,
partner) so wiring it in later is a one-line call, not a rewrite. See the
DECISIONS note passed back to the reviewer for the exact missing hook point.
"""
import base64
import logging
from datetime import datetime, timezone

_logger = logging.getLogger(__name__)

# account.tax.amount_type values that are unambiguously "a GST percentage".
# A fixed/group/division tax on an Nimbrel invoice line would make the TAX
# 5118 rate ill-defined, so those fail closed rather than guessing.
_PERCENT_TAX_TYPES = frozenset({"percent"})


class NimbrelInvoiceError(Exception):
    """Raised when an account.move cannot be safely built into an Nimbrel
    INVOIC -- fail-closed (house style): callers must review/alert, never
    guess and ship a malformed tax invoice."""
    pass


def _resolve_sale_order(move):
    """Return the (single) sale.order behind this invoice's product lines,
    or None if the invoice has no sale-order-linked lines at all.

    Explicit per-line walk (not the ORM's implicit ``recordset.field``
    flattening across ``invoice_line_ids.sale_line_ids.order_id``) so the
    resolution is obvious and independently unit-testable against plain
    fakes rather than relying on Odoo-specific recordset sugar.
    """
    for move_line in move.invoice_line_ids:
        for sol in move_line.sale_line_ids:
            order = _first(sol.order_id)
            if order:
                return order
    return None


def _first(recordset):
    """Return the first record of a (possibly empty) Odoo recordset/Many2one
    value, or None. Works for both a real Many2one (already a single-record
    recordset, falsy when empty) and a One2many/Many2many collection."""
    if not recordset:
        return None
    try:
        return recordset[0]
    except (TypeError, IndexError):
        return recordset


# ── Shipped-quantity derivation (the qty-invoiced source of truth) ─────────

def shipped_qty_by_sale_line(sale_order) -> dict:
    """Return {sale_order_line: shipped_qty} from DONE stock moves only.

    Mirrors services.edi_service.EDIService._pack_units_for_nimbrel's own
    quantity source (``move.quantity`` on ``state == 'done'`` moves) so the
    INVOIC and the DESADV that shipped it can never disagree about what was
    actually shipped. Cancelled/draft/waiting moves contribute nothing.

    Keyed by the sale.order.line RECORD (not edi_line_number) so the caller
    can pair this directly against invoice lines via their own
    ``sale_line_ids`` -- edi_line_number is an Nimbrel-specific identifier
    that belongs in the payload builder, not this generic qty rollup.
    """
    totals = {}
    for picking in sale_order.picking_ids:
        for move in picking.move_ids:
            if move.state != "done":
                continue
            sol = move.sale_line_id
            if not sol:
                continue
            totals[sol] = totals.get(sol, 0.0) + move.quantity
    return totals


def _shipping_reference(sale_order_line) -> dict:
    """Return {'advice_no': ..., 'connote': ..., 'picking': <stock.picking|None>}
    for the picking that shipped ``sale_order_line`` -- i.e. the DESADV
    reference this INVOIC line traces back to (RFF+AAK / RFF+CN). Picks the
    most recently done picking touching this line when more than one shipped
    partial quantity (should not normally happen for a single invoice, but
    stays deterministic rather than raising).
    """
    done_moves = sale_order_line.move_ids.filtered(lambda m: m.state == "done")
    if not done_moves:
        return {"advice_no": None, "connote": None, "picking": None}
    picking = sorted(
        done_moves.mapped("picking_id"),
        key=lambda p: p.date_done or p.write_date or p.create_date,
    )[-1]
    advice_no = "DESADV-%s" % picking.name.replace("/", "") if picking else None
    connote = getattr(picking, "carrier_tracking_ref", None) or (
        picking.name if picking else None
    )
    return {"advice_no": advice_no, "connote": connote, "picking": picking}


# ── GST extraction ──────────────────────────────────────────────────────────

def _line_tax_rate(move_line) -> str:
    """Return the GST rate as a 2dp string (TAX 5118), e.g. '15.00'.

    Fail-closed: raises NimbrelInvoiceError if the line has no tax, more
    than one tax, or a non-percentage tax -- an ambiguous/unrepresentable
    rate must stop the build, not silently ship '0.00' or the first tax
    found.
    """
    taxes = move_line.tax_ids
    if not taxes:
        raise NimbrelInvoiceError(
            "Invoice line '%s' has no tax set — cannot determine the GST "
            "rate for TAX 5118. Assign a GST tax before invoicing an "
            "Nimbrel order." % (move_line.name or move_line.id)
        )
    if len(taxes) != 1:
        raise NimbrelInvoiceError(
            "Invoice line '%s' has %d taxes — Nimbrel INVOIC (TAX 7+GST) "
            "expects exactly one GST rate per line." % (
                move_line.name or move_line.id, len(taxes),
            )
        )
    tax = taxes[0]
    if getattr(tax, "amount_type", "percent") not in _PERCENT_TAX_TYPES:
        raise NimbrelInvoiceError(
            "Invoice line '%s' tax '%s' is not a percentage tax — cannot "
            "represent it as TAX 5118." % (move_line.name or move_line.id, tax.name)
        )
    return "%.2f" % tax.amount


def _line_moa(move_line) -> dict:
    """Return {'moa_128': ex-tax, 'moa_369': gst, 'moa_203': incl-tax} as 4dp
    strings, computed from Odoo's own price_subtotal/price_total so the MIG's
    128 + 369 == 203 invariant holds by construction (369 is the
    DIFFERENCE, never independently recomputed from the rate)."""
    ex_tax = move_line.price_subtotal
    incl_tax = move_line.price_total
    gst = incl_tax - ex_tax
    return {
        "moa_128": "%.4f" % ex_tax,
        "moa_369": "%.4f" % gst,
        "moa_203": "%.4f" % incl_tax,
    }


def _format_qty(value) -> str:
    """Render an invoiced quantity as a plain decimal string for QTY 47 --
    whole numbers as integers ('10', matching services/edi_service.py's own
    ``str(int(round(qty)))`` convention for QTY+12 on the DESADV), fractional
    quantities kept as a plain (non-scientific-notation) decimal. Avoids
    '%g' which silently switches to scientific notation above 6 significant
    digits -- never acceptable in an EDI quantity field."""
    if float(value).is_integer():
        return str(int(round(value)))
    return ("%f" % value).rstrip("0").rstrip(".")


def _product_isc(product):
    """The Nimbrel ISC (buyer item code, PIA+5:IN) persisted on a product.

    MML stores it in ``x_articleno`` (a Studio field present in production but
    absent from a bare module install). Tolerates real recordsets, test fakes,
    and a missing field — returns None (the caller then fails closed) rather
    than raising, so this is only a SECONDARY source behind the re-parsed
    ORDERS, which are authoritative for the ISC Nimbrel actually sent.
    """
    tmpl = getattr(product, "product_tmpl_id", None) or product
    return getattr(tmpl, "x_articleno", None) or getattr(product, "x_articleno", None)


def _isc_map_from_orders(sale_order, partner):
    """Best-effort {sale.order.line: ISC} by re-parsing the ORDERS that created
    the SO — the authoritative source of Nimbrel' item number (PIA+5:IN),
    which is NOT persisted on the SO line (mirrors the DESADV path's
    _nimbrel_item_identity_map). Returns {} on any failure (no review, no raw
    data, parse error, or a non-Odoo move in pure tests)."""
    if not sale_order:
        return {}
    try:
        env = sale_order.env
        reviews = env["edi.order.review"].search(
            [("sale_order_id", "=", sale_order.id)])
        line_isc = {}
        for review in reviews:
            if not review.edi_raw_data:
                continue
            orders = partner.get_parser_instance().parse_file(
                review.edi_raw_data.encode("iso-8859-1", errors="replace"), partner)
            for order in orders:
                if order.po_number != review.customer_po_number:
                    continue
                for ln in order.lines:
                    if ln.buyer_article_no:
                        line_isc[ln.line_number] = ln.buyer_article_no
        return {
            sol: line_isc[sol.edi_line_number]
            for sol in sale_order.order_line
            if sol.edi_line_number in line_isc
        }
    except Exception:
        return {}


# ── Payload construction ────────────────────────────────────────────────────

def build_invoic_payload_from_move(move, partner, *, isc_by_line=None) -> dict:
    """Build the ``parsers.nimbrel_invoic.build_invoic`` payload dict from a
    posted ``account.move`` (customer invoice), enforcing qty-invoiced ==
    qty-shipped and EX-tax/INCL-tax MOA semantics.

    Args:
        move: account.move recordset (single record; a posted customer
            invoice, ``move_type == 'out_invoice'``).
        partner: edi.trading.partner record for Nimbrel (used for
            NAD+SU / PIA+1:SA identity fallback and buyer/ship-to lookups
            via the linked sale.order's edi_review_id, mirroring the DESADV
            builder's own convention).
        isc_by_line: optional {sale_order_line: isc} override map (buyer
            item code, PIA+5:IN). Falls back to ``sale_line.edi_line_number``
            cast to str when absent, matching how the DESADV path treats
            edi_line_number as the ISC of last resort (services/
            edi_service.py::_pack_units_for_nimbrel).

    Returns:
        Payload dict ready for ``parsers.nimbrel_invoic.build_invoic``, or
        raises NimbrelInvoiceError if nothing on the invoice is shippable
        (i.e. every line has zero shipped qty -- would produce an empty
        INVOIC, which the MIG's "at least one SG26" requirement forbids).

    Never falls back to the SO's ordered qty: a line whose linked
    sale_order_line has shipped_qty <= 0 (nothing shipped yet for it) is
    OMITTED from the INVOIC entirely -- this is exactly scenario 5A (ISC3
    not yet shipped -> not on the INVOIC) and 5B (the follow-up invoice
    carries ONLY ISC3, because by then it is the only line with shipped
    qty on THIS invoice's move lines).
    """
    move.ensure_one()

    sale_order = _resolve_sale_order(move)
    # ISC (PIA+5:IN) comes from re-parsing the original ORDERS unless a caller
    # supplied the map — the authoritative source Nimbrel sent, not persisted
    # on the SO line. _product_isc (x_articleno) is a per-line fallback below.
    isc_by_line = isc_by_line or _isc_map_from_orders(sale_order, partner)
    shipped = shipped_qty_by_sale_line(sale_order) if sale_order else {}

    lines = []
    ref_source = {"advice_no": None, "connote": None}
    for idx, move_line in enumerate(
        move.invoice_line_ids.filtered(lambda l: l.display_type == "product"),
        start=1,
    ):
        sol = _first(move_line.sale_line_ids)
        if not sol:
            _logger.warning(
                "Nimbrel INVOIC: invoice %s line '%s' has no linked sale "
                "order line — skipped (nothing to reconcile against a "
                "DESADV shipment).", move.name, move_line.name,
            )
            continue

        qty_shipped = shipped.get(sol, 0.0)
        if qty_shipped <= 1e-6:
            _logger.info(
                "Nimbrel INVOIC: invoice %s line '%s' (SO line %s) has "
                "zero shipped qty — omitted from the INVOIC (scenario "
                "5A/5B partial-shipment rule).",
                move.name, move_line.name, sol.id,
            )
            continue

        # Qty invoiced is clamped to what was actually shipped, never the
        # invoice line's own (potentially SO-qty-derived) quantity — the
        # hard AN-03 contract.
        qty_invoiced = min(move_line.quantity, qty_shipped)

        ship_ref = _shipping_reference(sol)
        if not ref_source["advice_no"] and not ref_source["connote"]:
            ref_source = ship_ref

        line_no = sol.edi_line_number or idx
        # Buyer item code (PIA+5:IN) MUST be Nimbrel' ISC — never the LIN line
        # number and never MML's own code. Prefer the ISC from the re-parsed
        # ORDERS (isc_by_line), else the ISC persisted on the product
        # (x_articleno, where MML stores Nimbrel ISCs). Fail CLOSED rather than
        # put a wrong identifier on the wire that Nimbrel would reject/mis-post.
        isc = isc_by_line.get(sol) or _product_isc(move_line.product_id)
        if not isc:
            raise NimbrelInvoiceError(
                "No Nimbrel ISC (buyer item code) for product %s on move line "
                "%s — set the ISC in x_articleno or supply isc_by_line; refusing "
                "to send the LIN line number as the item code." % (
                    move_line.product_id.default_code
                    or move_line.product_id.display_name, move_line.id,
                )
            )
        vendor_code = move_line.product_id.default_code or isc

        moa = _line_moa(move_line)
        lines.append({
            "line_no": str(line_no),
            "buyer_item": isc,
            "supplier_item": vendor_code,
            "description": move_line.name or move_line.product_id.display_name,
            "qty_invoiced": _format_qty(qty_invoiced),
            "qty_unit": "EA",
            "qty_consumer_units": "1",
            "moa_128": moa["moa_128"],
            "moa_369": moa["moa_369"],
            "moa_203": moa["moa_203"],
            "price": "%.4f" % move_line.price_unit,
            "tax_rate": _line_tax_rate(move_line),
            "tax_category": "GST",
        })

    if not lines:
        raise NimbrelInvoiceError(
            "Nimbrel INVOIC: invoice %s has no lines with shipped quantity "
            "— refusing to build an empty INVOIC (nothing has been "
            "despatched for this invoice yet)." % move.name
        )

    if not ref_source["advice_no"] and not ref_source["connote"]:
        raise NimbrelInvoiceError(
            "Nimbrel INVOIC: invoice %s — could not resolve a despatch "
            "advice number or carrier connote from any shipped line's "
            "picking (Nimbrel requires at least one, per "
            "Nimbrel_INVOIC.pdf RFF notes)." % move.name
        )

    partner_id = move.partner_id
    review = sale_order.edi_review_id if sale_order else None
    store_code = (
        (review.store_code if review else None)
        or partner_id.ref
        or ""
    )
    po_number = (
        (sale_order.client_order_ref or sale_order.name)
        if sale_order else move.ref or move.name
    )

    company = move.company_id or move.env.company
    total_ex = sum(float(l["moa_128"]) for l in lines)
    total_tax = sum(float(l["moa_369"]) for l in lines)
    total_incl = sum(float(l["moa_203"]) for l in lines)

    invoice_date = move.invoice_date or move.date or datetime.now(timezone.utc).date()
    now = datetime.now(timezone.utc)

    return {
        "date_yymmdd": now.strftime("%y%m%d"),
        "time_hhmm": now.strftime("%H%M"),
        "invoice_number": move.name,
        "message_function": "9",
        "invoice_date": invoice_date.strftime("%Y%m%d"),
        "ref_aak": ref_source["advice_no"] or "",
        "ref_cn": ref_source["connote"] or "",
        "ref_on": po_number,
        "buyer": {
            "code": None,  # filled by get_unb_recipient()-derived id at upload time
            "name": partner_id.name or "",
            "street": partner_id.street or "",
            "city": partner_id.city or "",
            "state": partner_id.state_id.name if partner_id.state_id else "",
            "postcode": partner_id.zip or "",
            "country": partner_id.country_id.code or "",
            "nzbn": _buyer_nzbn(move.env, partner_id),
        },
        "supplier": {
            "code": partner.nimbrel_vendor_code or partner.code,
            "name": company.name or "",
            "street": company.street or "",
            "city": company.city or "",
            "state": company.state_id.name if company.state_id else "",
            "postcode": company.zip or "",
            "country": company.country_id.code or "",
            "abn": _supplier_abn(move.env, company),
            "contact_name": _supplier_contact_name(move.env),
            "phone": company.phone or "",
            "email": company.email or "",
        },
        "ship_to": {
            "code": store_code,
            "name": move.partner_shipping_id.name or partner_id.name or "",
            "street": (move.partner_shipping_id.street or ""),
            "city": (move.partner_shipping_id.city or ""),
            "state": "",
            "postcode": (move.partner_shipping_id.zip or ""),
            "country": (
                move.partner_shipping_id.country_id.code
                if move.partner_shipping_id.country_id else ""
            ),
        },
        "currency": move.currency_id.name or "NZD",
        "lines": lines,
        "summary": {
            "moa_39": "%.2f" % total_incl,
            "moa_128": "%.2f" % total_ex,
            "moa_369": "%.2f" % total_tax,
        },
    }


def _buyer_nzbn(env, buyer_partner) -> str:
    """Nimbrel' NZBN for RFF+AMT under NAD+BY. Config-first (per-tenant
    override, matching the mml_edi.* ir.config_parameter idiom used
    elsewhere — e.g. mml_edi.gs1_company_prefix) with a fallback to the
    buyer res.partner's own VAT/company-number field."""
    configured = env["ir.config_parameter"].sudo().get_param("mml_edi.nimbrel_nzbn")
    if configured:
        return configured
    return buyer_partner.vat or ""


def _supplier_abn(env, company) -> str:
    """MML's own ABN/company number for RFF+AMT under NAD+SU. Config-first,
    falling back to the invoicing company's VAT field."""
    configured = env["ir.config_parameter"].sudo().get_param("mml_edi.supplier_abn")
    if configured:
        return configured
    return company.vat or ""


def _supplier_contact_name(env) -> str:
    """CTA OC contact name (C056 component 2). No res.company field for this
    exists in the module today (out of scope for this wave to add one — no
    manifest/model edits) so this is config-only; blank is a legal CTA OC
    value if left unset (see DECISIONS)."""
    return env["ir.config_parameter"].sudo().get_param("mml_edi.invoic_contact_name", "") or ""


# ── Odoo orchestration (build -> render -> upload -> log) ─────────────────

def generate_and_upload_invoic(env, move, partner) -> bytes:
    """Build, render, upload and log an Nimbrel INVOIC for a posted
    account.move. Returns the rendered interchange bytes.

    Mirrors services.edi_service.EDIService._generate_and_upload_desadv_
    nimbrel's shape exactly (real UNB identity via C1/C3, a fresh ctrl_ref
    from the C2 sequence, ir.attachment audit trail, edi.log event) so a
    future account.move-posted hook wiring this in is a straight call, not
    a redesign. Uses the existing 'ack_sent' event_type (no new
    edi.log.event_type value is introduced by this wave — see DECISIONS)
    with a filename prefix that mirrors DESADV's own
    'DESADV_NIMBREL_<po>_<date>' convention so the two are trivially
    distinguishable and searchable the same way
    (test_edi_service_nimbrel_odoo.py's own
    ``filename like 'DESADV_NIMBREL_%'`` pattern).
    """
    from ..parsers.nimbrel_invoic import build_invoic
    from ..models.edi_ftp import get_transport_handler

    payload = build_invoic_payload_from_move(move, partner)

    recipient_id, recipient_qual = partner.get_unb_recipient()
    sender_id, sender_qual = partner.get_unb_sender()
    ctrl_ref = env["ir.sequence"].sudo().next_by_code(
        "mml_edi.nimbrel.interchange.ref"
    )
    if not ctrl_ref:
        raise NimbrelInvoiceError(
            "mml_edi.nimbrel.interchange.ref sequence is not configured"
        )

    payload = dict(payload, buyer=dict(payload["buyer"], code=recipient_id))

    invoic_bytes = build_invoic(payload, supplier_gln=sender_id, ctrl_ref=int(ctrl_ref))

    po_for_filename = str(payload["ref_on"]).replace("/", "")
    filename = "INVOIC_NIMBREL_%s_%s.edi" % (po_for_filename, payload["invoice_date"])

    handler = get_transport_handler(partner)
    with handler.connection():
        handler.upload_file(filename, invoic_bytes)

    env["ir.attachment"].create({
        "name": filename,
        "datas": base64.b64encode(invoic_bytes).decode(),
        "res_model": "account.move",
        "res_id": move.id,
        "mimetype": "text/plain",
    })
    move.message_post(body="INVOIC sent to Nimbrel: %s" % filename)

    sale_order = _resolve_sale_order(move)
    env["edi.log"].log(
        partner, "outbound", "ack_sent", "success",
        "INVOIC uploaded for %s: %s (%d bytes)" % (
            payload["ref_on"], filename, len(invoic_bytes),
        ),
        filename=filename,
        sale_order=sale_order or None,
    )
    _logger.info(
        "EDI INVOIC: uploaded Nimbrel INVOIC %s (%d bytes)",
        filename, len(invoic_bytes),
    )
    return invoic_bytes


__all__ = [
    "NimbrelInvoiceError",
    "shipped_qty_by_sale_line",
    "build_invoic_payload_from_move",
    "generate_and_upload_invoic",
]
