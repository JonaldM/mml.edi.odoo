import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class EDIBulkAction(models.TransientModel):
    _name = "edi.bulk.action"
    _description = "EDI Bulk Approve"

    review_ids = fields.Many2many(
        comodel_name="edi.order.review",
        string="Reviews",
    )
    pending_count = fields.Integer(
        string="Pending Reviews",
        compute="_compute_counts",
    )
    new_order_count = fields.Integer(
        string="New Orders",
        compute="_compute_counts",
    )
    change_order_count = fields.Integer(
        string="Change Orders",
        compute="_compute_counts",
    )
    blocking_count = fields.Integer(
        string="With Blocking Issues",
        compute="_compute_counts",
    )
    approvable_count = fields.Integer(
        string="Will Be Approved",
        compute="_compute_counts",
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        active_ids = self.env.context.get("active_ids", [])
        reviews = self.env["edi.order.review"].browse(active_ids).filtered(
            lambda r: r.state == "pending_review"
        )
        res["review_ids"] = [(6, 0, reviews.ids)]
        return res

    @api.depends("review_ids")
    def _compute_counts(self):
        for wiz in self:
            reviews = wiz.review_ids
            wiz.pending_count = len(reviews)
            wiz.new_order_count = len(
                reviews.filtered(lambda r: r.document_type == "new_order")
            )
            wiz.change_order_count = len(
                reviews.filtered(lambda r: r.document_type == "change_order")
            )
            wiz.blocking_count = len(
                reviews.filtered(lambda r: r.blocking_issue_count > 0)
            )
            wiz.approvable_count = wiz.pending_count - wiz.blocking_count

    def action_approve_all(self):
        self.ensure_one()
        approved = 0
        skipped = 0
        for review in self.review_ids:
            if review.blocking_issue_count > 0:
                skipped += 1
                continue
            try:
                review.action_approve()
                approved += 1
            except Exception:
                _logger.exception(
                    "EDI bulk approve: failed to approve edi.order.review id=%s",
                    review.id,
                )
                skipped += 1

        message = f"{approved} record(s) approved."
        if skipped:
            message += f" {skipped} record(s) skipped (blocking issues or errors)."

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Bulk Approve Complete",
                "message": message,
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
