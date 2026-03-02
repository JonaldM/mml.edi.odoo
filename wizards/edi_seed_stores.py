# mml.edi/wizards/edi_seed_stores.py
"""
Wizard to seed Briscoes store partners as child contacts of the Briscoes Group partner.

Used for fresh installs / dev environments. In production, store partners
already exist from the legacy .NET system — running this wizard is safe
(idempotent: skips partners where res.partner.ref already exists as a child).
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

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


class EDISeedStoresWizard(models.TransientModel):
    _name = "edi.seed.stores.wizard"
    _description = "Seed Briscoes Store Partners"

    trading_partner_id = fields.Many2one(
        "edi.trading.partner",
        required=True,
        readonly=True,
        string="Trading Partner",
    )

    # Result summary (populated after execution)
    result_created = fields.Integer(string="Partners Created", readonly=True)
    result_skipped = fields.Integer(string="Already Existed (Skipped)", readonly=True)
    result_message = fields.Text(string="Result", readonly=True)
    state = fields.Selection(
        [("draft", "Ready"), ("done", "Complete")],
        default="draft",
        string="State",
    )

    def action_seed_stores(self):
        """Create missing store partners. Idempotent — skips existing."""
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
        created = 0
        skipped = 0

        for store_code, store_name in _BRISCOES_STORES:
            existing = self.env["res.partner"].search([
                ("parent_id", "=", parent_partner.id),
                ("ref", "=", store_code),
            ], limit=1)

            if existing:
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
            "result_skipped": skipped,
            "result_message": "Created %d store partner(s). %d already existed and were skipped." % (
                created, skipped
            ),
            "state": "done",
        })

        return {
            "type": "ir.actions.act_window",
            "res_model": "edi.seed.stores.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
