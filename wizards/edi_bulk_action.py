import logging

from odoo import api, fields, models

from ..models.edi_processor import _commit_suppressed

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

    def _commit_record(self):
        """Make an approved record durable before the next one is attempted.

        Without this, the rollback that isolates a LATER failing record would
        take every record approved since the last commit with it. Guarded
        exactly like edi.order.review._commit_ack_progress so a test
        harness's transaction is never really committed.
        """
        if _commit_suppressed(self.env):
            return
        self.env.cr.commit()

    def _rollback_record(self):
        """Discard the partial writes of a record whose approve raised.

        Python unwinding does NOT undo the ORM writes action_approve already
        made (re-clamped line quantities, chatter, an ORDCHG diff written
        over an existing SO). Left in the transaction they are made permanent
        by the next record's commit - or by _queue_ack's own
        _commit_ack_progress - while the operator is told the record was
        merely 'skipped'. Under a test harness nothing is really committed,
        so there is nothing to protect and a rollback would destroy the
        harness's own fixtures: skip it there, same guard as _commit_record.
        """
        if _commit_suppressed(self.env):
            return
        self.env.cr.rollback()

    def action_approve_all(self):
        self.ensure_one()
        approved = 0
        skipped = 0
        failed = []
        for review in self.review_ids:
            if review.blocking_issue_count > 0:
                skipped += 1
                continue
            label = review.display_name or str(review.id)
            try:
                # action_approve runs the full approve pipeline, including the
                # SS-1 approve-time availability re-clamp and the SS-3
                # post-confirm reservation verify — bulk approval gets the
                # same stock gating as a one-by-one approval.
                review.action_approve()
            except Exception:
                _logger.exception(
                    "EDI bulk approve: failed to approve edi.order.review id=%s",
                    review.id,
                )
                self._rollback_record()
                failed.append(label)
            else:
                approved += 1
                self._commit_record()

        message = f"{approved} record(s) approved."
        if skipped:
            message += f" {skipped} record(s) skipped (blocking issues)."
        if failed:
            message += " %d record(s) failed and were rolled back: %s." % (
                len(failed), ", ".join(failed),
            )

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Bulk Approve Complete",
                "message": message,
                "type": "warning" if failed else "success",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
