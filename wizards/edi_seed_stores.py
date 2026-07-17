# mml.edi/wizards/edi_seed_stores.py
"""
Wizard to seed store/customer child partners under a trading partner's
Odoo customer record.

Two modes, auto-detected from the trading partner's parser_class:
  - Briscoes (BriscoesIDOCParser): flat store list, numeric refs.
  - Animates (AnimatesParser): store list embedded from the Animates
    Clinic/Store Master File (wizards/animates_store_master_data.py),
    2-digit + "R-xx" refs. See that module's docstring for the
    clinic/retail collision handling (gate review AN-13/OPS-5).

Used for fresh installs / dev environments. In production, Briscoes store
partners already exist from the legacy .NET system — running this wizard
is safe (idempotent: skips/updates by res.partner.ref scoped to the parent
customer, never creates a duplicate).

Ref-collision safety: lookups/creates are always scoped to
("parent_id", "=", parent_partner.id) so Animates' 2-digit refs (e.g. "08")
can never collide with Briscoes' numeric refs (e.g. "1017") even though
both trading partners' customers may exist in the same database — they sit
under different parents.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .animates_store_master_data import get_animates_stores

# Briscoes store master data — update when new stores are added
# Format: (store_code, store_name)
_BRISCOES_STORES = [
    ("1017", "Briscoes - Auckland City"),
    ("1042", "Briscoes - Penrose"),
    ("1043", "Briscoes - Manukau"),
    ("1044", "Briscoes - Albany"),
    ("1045", "Briscoes - Westgate"),
    ("1046", "Briscoes - Henderson"),
    ("1050", "Briscoes - Hamilton"),
    ("1060", "Briscoes - Tauranga"),
    ("1070", "Briscoes - Wellington City"),
    ("1071", "Briscoes - Petone"),
    ("1072", "Briscoes - Porirua"),
    ("1080", "Briscoes - Christchurch"),
    ("1081", "Briscoes - Riccarton"),
    ("1082", "Briscoes - Papanui"),
    ("1090", "Briscoes - Dunedin"),
    ("2017", "Rebel Sport - Auckland City"),
    ("2042", "Rebel Sport - Penrose"),
    ("2050", "Rebel Sport - Hamilton"),
    ("2070", "Rebel Sport - Wellington"),
    ("2080", "Rebel Sport - Christchurch"),
    ("3017", "Living & Giving - Auckland City"),
    ("3070", "Living & Giving - Wellington"),
    ("3080", "Living & Giving - Christchurch"),
]


# parser_class values (see models/edi_trading_partner.py _ALLOWED_PARSER_CLASSES)
# that select Animates seed mode. Anything else falls back to Briscoes mode —
# the wizard predates multi-partner support and Briscoes has no marker of its
# own, so Animates is the ONLY mode that opts in explicitly.
_ANIMATES_PARSER_CLASS = "mml_edi.parsers.animates.AnimatesParser"


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
        trading partner. Animates partners (by parser_class) get the embedded
        Clinic/Store Master table; everything else gets the legacy Briscoes
        list — this wizard predates multi-partner support and Briscoes has no
        explicit opt-in marker of its own."""
        self.ensure_one()
        if self.trading_partner_id.parser_class == _ANIMATES_PARSER_CLASS:
            return [(code, name) for code, name, _region in get_animates_stores()]
        return _BRISCOES_STORES

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
            # Scoped to parent_id so Animates' 2-digit/"R-xx" refs can never
            # collide with Briscoes' numeric refs even if both customers
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
