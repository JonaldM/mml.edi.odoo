# mml.edi/models/edi_order_issue.py
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EDIOrderIssue(models.Model):
    _name = "edi.order.issue"
    _description = "EDI Order Issue"
    _order = "severity desc, issue_type"

    review_id = fields.Many2one(
        "edi.order.review",
        required=True,
        ondelete="cascade",
        index=True,
        string="Review",
    )
    issue_type = fields.Selection(
        [
            ("price_discrepancy", "Price Discrepancy"),
            ("product_not_found", "Product Not Found"),
            ("qty_shortfall", "Stock Shortfall"),
            ("unknown_store", "Unknown Store"),
            ("uom_mismatch", "UOM Mismatch"),
            ("product_matched_by_fallback", "Product Matched by Fallback"),
            ("other", "Other"),
        ],
        required=True,
        string="Issue Type",
    )
    severity = fields.Selection(
        [("blocking", "Blocking"), ("warning", "Warning"), ("info", "Info")],
        required=True,
        string="Severity",
    )
    description = fields.Text(required=True, string="Description")
    edi_line_data = fields.Text(string="EDI Line Data")
    sale_order_line_id = fields.Many2one(
        "sale.order.line",
        ondelete="set null",
        string="SO Line",
    )

    # Price discrepancy fields
    edi_price = fields.Float(string="EDI Price", digits="Product Price")
    system_price = fields.Float(string="System Price", digits="Product Price")
    price_difference_pct = fields.Float(
        compute="_compute_price_difference_pct",
        store=True,
        string="Price Diff (%)",
    )

    # Resolution
    resolution = fields.Selection(
        [
            ("pending", "Pending"),
            ("accepted", "Accepted"),
            ("corrected", "Corrected"),
            ("rejected", "Rejected"),
        ],
        default="pending",
        required=True,
        string="Resolution",
    )
    resolved_by = fields.Many2one("res.users", string="Resolved By")
    resolved_date = fields.Datetime(string="Resolved Date")
    resolution_notes = fields.Text(string="Resolution Notes")

    # ── Computed ──────────────────────────────────────────────────────────

    @api.depends("edi_price", "system_price")
    def _compute_price_difference_pct(self):
        for rec in self:
            if rec.system_price:
                rec.price_difference_pct = abs(
                    (rec.edi_price - rec.system_price) / rec.system_price * 100
                )
            else:
                rec.price_difference_pct = 0.0

    # ── Resolution Actions ────────────────────────────────────────────────

    def action_accept(self):
        """Accept the EDI value as-is — SO line stays at EDI price."""
        for rec in self:
            rec.write({
                "resolution": "accepted",
                "resolved_by": self.env.user.id,
                "resolved_date": fields.Datetime.now(),
            })

    def action_correct(self, new_price: float = None):
        """Correct to system price or a manually entered price."""
        self.ensure_one()
        target_price = new_price if new_price is not None else self.system_price
        if self.sale_order_line_id:
            self.sale_order_line_id.price_unit = target_price
        self.write({
            "resolution": "corrected",
            "resolved_by": self.env.user.id,
            "resolved_date": fields.Datetime.now(),
            "resolution_notes": (self.resolution_notes or "") +
                f"\nCorrected to {target_price:.4f}",
        })

    def action_reject_issue(self):
        """Reject this line."""
        for rec in self:
            rec.write({
                "resolution": "rejected",
                "resolved_by": self.env.user.id,
                "resolved_date": fields.Datetime.now(),
            })

    def action_flag_for_pricelist_update(self):
        """Create an activity on the pricelist to remind team to update it."""
        self.ensure_one()
        if not self.review_id.trading_partner_id.pricelist_id:
            raise UserError(_("No pricelist configured for this trading partner."))
        pricelist = self.review_id.trading_partner_id.pricelist_id
        pricelist.activity_schedule(
            "mail.mail_activity_data_todo",
            summary=_("Review pricelist price for EDI discrepancy"),
            note=_("EDI order %s had a price discrepancy for SO line: %s. "
                   "EDI price: %.4f, System price: %.4f") % (
                self.review_id.customer_po_number,
                self.sale_order_line_id.name if self.sale_order_line_id else "N/A",
                self.edi_price,
                self.system_price,
            ),
            user_id=self.env.user.id,
        )
