# mml.edi/models/sscc_register.py
"""SSCC-18 (Serial Shipping Container Code) register for Nimbrel DESADV/labels.

Mints and persists one row per logistic unit (pallet or carton) on a
stock.picking, per AN-03/OPS-6. Idempotency is the whole point of this model:
re-generating a DESADV or re-printing a label for the same picking/unit_key
must NEVER mint a second SSCC — Nimbrel rejects a re-used SSCC within a
12-month window (Nimbrel_DESADV.pdf p.9 "Validations"), and re-minting on
every retry would burn through the serial range for nothing.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..parsers.gs1_sscc import GS1SSCCError, build_sscc18

_logger = logging.getLogger(__name__)

# DB constraint name Odoo generates for the models.Constraint attribute
# `_picking_unit_unique` below (<table>_<attribute_name>, same convention
# models/edi_processor.py relies on for SO_EDI_CLIENT_REF_INDEX). Matched
# by name (not a generic substring) so get_or_create()'s race-recovery path
# never accidentally swallows an unrelated DB error.
SSCC_PICKING_UNIT_UNIQUE_CONSTRAINT = "sscc_register_picking_unit_unique"


def _is_picking_unit_race_violation(exc) -> bool:
    """True when ``exc`` is the unique-violation raised by the
    (picking_id, unit_key) idempotency constraint — i.e. a concurrent
    request minted the SAME unit first and committed before us."""
    return SSCC_PICKING_UNIT_UNIQUE_CONSTRAINT in str(exc)


# GS1 Company Prefix — ACCOUNT-SPECIFIC, no built-in default.
#
# A company prefix is allocated by GS1 to ONE company; baking a literal in
# would mint SSCCs that claim another company's prefix on every deployment that
# never configured its own — undetectable locally, rejected or misattributed by
# the receiving partner. The value lives in ir.config_parameter
# 'mml_edi.gs1_company_prefix' (seeded from the deployment's existing value by
# migrations/19.0.1.3.0/post-migration.py); a fresh install starts blank and
# _get_gs1_prefix() fails closed. 7 digits -> 9 digits of serial headroom per
# extension digit (see parsers/gs1_sscc.py for the full SSCC-18 layout).
DEFAULT_GS1_PREFIX = ""

# GS1 requires SSCCs to be unique for at least 12 months per company prefix
# (Nimbrel_DESADV.pdf p.9). This model never reuses a (gs1_prefix,
# extension_digit) serial — the ir.sequence backing it is monotonic-forever
# (see data/ir_sequence.xml comment on mml_edi.sscc.serial) — so uniqueness
# holds indefinitely, comfortably exceeding the 12-month floor.
SSCC_UNIQUENESS_MONTHS = 12


class SSCCRegister(models.Model):
    _name = "sscc.register"
    _rec_name = "sscc"
    _description = "SSCC-18 Register (GS1 Serial Shipping Container Code)"
    _order = "id"

    sscc = fields.Char(
        required=True,
        readonly=True,
        copy=False,
        string="SSCC-18",
        help="Full 18-digit Serial Shipping Container Code "
             "(extension digit + GS1 prefix + serial + mod-10 check digit).",
    )
    picking_id = fields.Many2one(
        "stock.picking",
        required=True,
        index=True,
        ondelete="restrict",
        string="Delivery",
    )
    unit_key = fields.Char(
        required=True,
        string="Unit Key",
        help="Caller-supplied identifier for the logistic unit within the "
             "picking (e.g. 'pallet-1', 'carton-3'). Combined with "
             "picking_id this is the idempotency key — re-requesting the "
             "SAME unit_key on the SAME picking always returns the SAME "
             "SSCC, never mints a new one.",
    )
    unit_type = fields.Selection(
        [("pallet", "Pallet"), ("carton", "Carton")],
        required=True,
        default="carton",
        string="Unit Type",
    )
    gs1_prefix = fields.Char(
        required=True,
        readonly=True,
        string="GS1 Company Prefix",
    )
    extension_digit = fields.Char(
        required=True,
        readonly=True,
        default="0",
        string="Extension Digit",
    )
    serial = fields.Integer(
        required=True,
        readonly=True,
        string="Serial (raw)",
        help="Raw integer value from the mml_edi.sscc.serial sequence — the "
             "zero-padded component embedded in the SSCC.",
    )
    active = fields.Boolean(default=True)

    _sscc_unique = models.Constraint(
        "UNIQUE(sscc)",
        "An SSCC must never be minted twice (GS1 12-month uniqueness "
        "requirement — see Nimbrel_DESADV.pdf p.9).",
    )
    _picking_unit_unique = models.Constraint(
        "UNIQUE(picking_id, unit_key)",
        "This picking already has an SSCC registered for this unit_key — "
        "re-printing a label or DESADV must reuse it, never mint a new one.",
    )

    @api.model
    def _get_gs1_prefix(self) -> str:
        """Return the configured GS1 company prefix, or fail closed.

        There is deliberately no code default (see DEFAULT_GS1_PREFIX): an
        unconfigured install must not mint SSCCs under someone else's prefix.
        """
        prefix = (
            self.env["ir.config_parameter"].sudo().get_param(
                "mml_edi.gs1_company_prefix", DEFAULT_GS1_PREFIX
            )
            or DEFAULT_GS1_PREFIX
        ).strip()
        if not prefix:
            raise UserError(_(
                "SSCC register: no GS1 company prefix is configured. Set the "
                "system parameter 'mml_edi.gs1_company_prefix' to the GS1 "
                "company prefix allocated to this company before generating "
                "SSCC labels or DESADV messages."
            ))
        return prefix

    @api.model
    def get_or_create(self, picking, unit_key: str, *, unit_type: str = "carton"):
        """Idempotently return the SSCC register row for (picking, unit_key).

        This is the ONLY sanctioned entry point for minting — callers (the
        DESADV builder wiring, the label report) must go through here rather
        than calling create() directly, so re-print/re-generate can never
        double-mint. Concurrency-safe: a UNIQUE(picking_id, unit_key)
        constraint backs the idempotency (mirrors the sale_order EDI dedup
        pattern in models/sale_order.py) — a losing race falls back to a
        fresh search rather than raising.

        Args:
            picking: stock.picking record this unit belongs to.
            unit_key: caller-stable identifier for the logistic unit (e.g.
                'pallet-1', 'carton-3'). Must be stable across retries.
            unit_type: 'pallet' or 'carton' (informational; not part of the
                idempotency key — the SAME physical unit is never both).

        Returns:
            sscc.register recordset (existing or newly minted).
        """
        if not unit_key:
            raise UserError(_("SSCC register: unit_key is required."))

        existing = self.search([
            ("picking_id", "=", picking.id),
            ("unit_key", "=", unit_key),
        ], limit=1)
        if existing:
            return existing

        gs1_prefix = self._get_gs1_prefix()
        serial = self.env["ir.sequence"].sudo().next_by_code("mml_edi.sscc.serial")
        if not serial:
            raise UserError(
                _("SSCC register: sequence 'mml_edi.sscc.serial' is not "
                  "configured — cannot mint a new SSCC.")
            )
        serial_int = int(serial)
        try:
            sscc = build_sscc18(gs1_prefix, serial_int)
        except GS1SSCCError as exc:
            raise UserError(
                _("SSCC register: cannot build SSCC-18 for prefix %s, "
                  "serial %s: %s") % (gs1_prefix, serial_int, exc)
            ) from exc

        try:
            with self.env.cr.savepoint():
                return self.create({
                    "sscc": sscc,
                    "picking_id": picking.id,
                    "unit_key": unit_key,
                    "unit_type": unit_type,
                    "gs1_prefix": gs1_prefix,
                    "extension_digit": "0",
                    "serial": serial_int,
                })
        except Exception as exc:
            # Lost a concurrent create race on the (picking_id, unit_key)
            # unique constraint — the winning row already exists; use it
            # rather than surfacing a DB error to the caller. The minted
            # serial/SSCC from THIS attempt is simply discarded (the
            # sequence never rewinds, so no uniqueness is compromised).
            if not _is_picking_unit_race_violation(exc):
                raise
            _logger.info(
                "[EDI] SSCC register: lost create race for picking=%s "
                "unit_key=%s, reusing winning row", picking.id, unit_key,
            )
            existing = self.search([
                ("picking_id", "=", picking.id),
                ("unit_key", "=", unit_key),
            ], limit=1)
            if not existing:
                raise
            return existing

    def name_get(self):
        return [(rec.id, rec.sscc) for rec in self]

    # ── Label rendering support ──────────────────────────────────────────

    def get_label_data(self) -> dict:
        """Return the field values the SSCC label QWeb template needs, per
        Nimbrel_SSCC_Label_Specification.pdf Zones A-G.

        Kept as a plain dict builder here (not in the QWeb template) so the
        label-data logic is unit-testable without rendering PDF/HTML, and so
        a future label redesign only touches layout, not data sourcing.

        Zone data mapped from this row + its picking:
            A (Ship From) — MML company address
            B (Ship To)   — picking's shipping partner (store) name/address
            C (Postal code barcode, AI 420) — ship-to postal code
            D (Carrier / connote / unit count) — picking carrier + connote
            E (SSCC / PO# / ISC / Carton Qty)  — this row's SSCC + PO + line data
            F (Vendor part# / description / batch / best-before) — product data
            G (SSCC barcode, AI 00)            — this row's SSCC again, barcoded
        """
        self.ensure_one()
        picking = self.picking_id
        company = picking.company_id or self.env.company
        ship_to = picking.partner_id

        sale_order = picking.sale_id
        po_number = (
            (sale_order.client_order_ref or sale_order.name) if sale_order else ""
        )

        # Locate the move(s) this unit_key represents (mirrors the
        # 'carton-<sale_line_id-or-move-id>' convention EDIService mints
        # under — see services/edi_service.py::_pack_units_for_nimbrel).
        move = None
        for m in picking.move_ids:
            candidate_key = "carton-%s" % (
                m.sale_line_id.id if m.sale_line_id else m.id
            )
            if candidate_key == self.unit_key:
                move = m
                break

        product = move.product_id if move else None
        sol = move.sale_line_id if move and move.sale_line_id else None
        isc = sol.edi_line_number if sol else ""
        vendor_part = product.default_code if product else ""
        description = product.display_name if product else ""
        carton_qty = move.quantity if move else 0.0

        return {
            "sscc": self.sscc,
            "unit_type": self.unit_type,
            "ship_from_name": company.name or "",
            "ship_from_address": ", ".join(filter(None, [
                company.street, company.street2, company.city,
                company.state_id.name if company.state_id else "",
                company.zip, company.country_id.name if company.country_id else "",
            ])),
            "ship_to_code": (
                sale_order.edi_review_id.store_code
                if sale_order and sale_order.edi_review_id else (ship_to.ref or "")
            ),
            "ship_to_name": ship_to.name or "",
            "ship_to_address": ", ".join(filter(None, [
                ship_to.street, ship_to.street2, ship_to.city,
                ship_to.zip, ship_to.country_id.name if ship_to.country_id else "",
            ])),
            "ship_to_postal_code": ship_to.zip or "",
            "carrier_name": (
                picking.carrier_id.name if hasattr(picking, "carrier_id") and picking.carrier_id else ""
            ),
            "connote": getattr(picking, "carrier_tracking_ref", None) or picking.name,
            "po_number": po_number,
            "isc": isc or "",
            "carton_qty": carton_qty,
            "vendor_part_no": vendor_part or "",
            "description": description or "",
        }
