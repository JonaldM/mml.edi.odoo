# mml.edi/wizards/edi_seed_stores.py
"""
Wizard to seed store/customer child partners under a trading partner's
Odoo customer record.

Two modes, auto-detected from the trading partner's parser_class:
  - Kestrelby (KestrelbyIDOCParser): flat store list, numeric refs.
  - Nimbrel (NimbrelParser): store list embedded from the Nimbrel
    Clinic/Store Master File (wizards/nimbrel_store_master_data.py),
    2-digit + "R-xx" refs. See that module's docstring for the
    clinic/retail collision handling (gate review AN-13/OPS-5).

Used for fresh installs / dev environments. In production, Kestrelby store
partners already exist from the legacy .NET system — running this wizard
is safe (idempotent: skips/updates by res.partner.ref scoped to the parent
customer, never creates a duplicate).

Ref-collision safety: lookups/creates are always scoped to
("parent_id", "=", parent_partner.id) so Nimbrel' 2-digit refs (e.g. "08")
can never collide with Kestrelby' numeric refs (e.g. "1017") even though
both trading partners' customers may exist in the same database — they sit
under different parents.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .nimbrel_store_master_data import get_nimbrel_stores

# Kestrelby store master data — update when new stores are added
# Format: (store_code, store_name)
_KESTRELBY_STORES = [
    ("1017", "Kestrelby - Auckland City"),
    ("1042", "Kestrelby - Penrose"),
    ("1043", "Kestrelby - Manukau"),
    ("1044", "Kestrelby - Albany"),
    ("1045", "Kestrelby - Westgate"),
    ("1046", "Kestrelby - Henderson"),
    ("1050", "Kestrelby - Hamilton"),
    ("1060", "Kestrelby - Tauranga"),
    ("1070", "Kestrelby - Wellington City"),
    ("1071", "Kestrelby - Petone"),
    ("1072", "Kestrelby - Porirua"),
    ("1080", "Kestrelby - Christchurch"),
    ("1081", "Kestrelby - Riccarton"),
    ("1082", "Kestrelby - Papanui"),
    ("1090", "Kestrelby - Dunedin"),
    ("2017", "Vantekka Sports - Auckland City"),
    ("2042", "Vantekka Sports - Penrose"),
    ("2050", "Vantekka Sports - Hamilton"),
    ("2070", "Vantekka Sports - Wellington"),
    ("2080", "Vantekka Sports - Christchurch"),
    ("3017", "Larkbury Living - Auckland City"),
    ("3070", "Larkbury Living - Wellington"),
    ("3080", "Larkbury Living - Christchurch"),
]


# parser_class values (see models/edi_trading_partner.py _ALLOWED_PARSER_CLASSES)
# that select Nimbrel seed mode. Anything else falls back to Kestrelby mode —
# the wizard predates multi-partner support and Kestrelby has no marker of its
# own, so Nimbrel is the ONLY mode that opts in explicitly.
_NIMBREL_PARSER_CLASS = "mml_edi.parsers.nimbrel.NimbrelParser"


class EDISeedStoresWizard(models.TransientModel):
    _name = "edi.seed.stores.wizard"
    _description = "Seed Store Partners"

    trading_partner_id = fields.Many2one(
        "edi.trading.partner",
        required=True,
        readonly=True,
        string="Trading Partner",
    )

    # Result summary (populated after execution)
    result_created = fields.Integer(string="Partners Created", readonly=True)
    result_updated = fields.Integer(string="Partners Updated (Name Sync)", readonly=True)
    result_skipped = fields.Integer(string="Already Up To Date (Skipped)", readonly=True)
    result_message = fields.Text(string="Result", readonly=True)
    state = fields.Selection(
        [("draft", "Ready"), ("done", "Complete")],
        default="draft",
        string="State",
    )

    def _seed_store_rows(self):
        """Return the (store_code, store_name) rows to seed for this wizard's
        trading partner. Nimbrel partners (by parser_class) get the embedded
        Clinic/Store Master table; everything else gets the legacy Kestrelby
        list — this wizard predates multi-partner support and Kestrelby has no
        explicit opt-in marker of its own."""
        self.ensure_one()
        if self.trading_partner_id.parser_class == _NIMBREL_PARSER_CLASS:
            return [(code, name) for code, name, _region in get_nimbrel_stores()]
        return _KESTRELBY_STORES

    def action_seed_stores(self):
        """Create/update store partners. Idempotent: re-running never creates
        a duplicate for a (parent, ref) pair already seeded — an existing
        match is left alone unless its name has drifted from the master
        data, in which case the name is synced (not the ref, never a new
        record)."""
        self.ensure_one()
        parent_partner = self.trading_partner_id.partner_id
        if not parent_partner:
            raise UserError(
                _(
                    "The trading partner '%s' has no Odoo Customer set. "
                    "Set the customer on the trading partner record before seeding stores."
                )
                % self.trading_partner_id.name
            )
        store_rows = self._seed_store_rows()
        created = 0
        updated = 0
        skipped = 0

        for store_code, store_name in store_rows:
            # Scoped to parent_id so Nimbrel' 2-digit/"R-xx" refs can never
            # collide with Kestrelby' numeric refs even if both customers
            # exist in the same database (see module docstring).
            existing = self.env["res.partner"].search([
                ("parent_id", "=", parent_partner.id),
                ("ref", "=", store_code),
            ], limit=1)

            if existing:
                if existing.name != store_name:
                    existing.write({"name": store_name})
                    updated += 1
                else:
                    skipped += 1
            else:
                self.env["res.partner"].create({
                    "name": store_name,
                    "parent_id": parent_partner.id,
                    "ref": store_code,
                    "type": "delivery",
                    "customer_rank": 1,
                })
                created += 1

        self.write({
            "result_created": created,
            "result_updated": updated,
            "result_skipped": skipped,
            "result_message": (
                "Created %d store partner(s). %d updated (name sync). "
                "%d already up to date and were skipped."
            ) % (created, updated, skipped),
            "state": "done",
        })

        return {
            "type": "ir.actions.act_window",
            "res_model": "edi.seed.stores.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
