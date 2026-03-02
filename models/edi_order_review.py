# mml.edi/models/edi_order_review.py
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class EDIOrderReview(models.Model):
    _name = "edi.order.review"
    _description = "EDI Order Review"
    _order = "received_date desc"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    name = fields.Char(
        default=lambda self: self.env["ir.sequence"].next_by_code("edi.order.review"),
        copy=False,
        readonly=True,
        string="Reference",
    )
    trading_partner_id = fields.Many2one(
        "edi.trading.partner",
        required=True,
        ondelete="restrict",
        index=True,
        string="Trading Partner",
    )
    customer_po_number = fields.Char(required=True, index=True, string="Customer PO Number")
    store_code = fields.Char(index=True, string="Store Code")
    sale_order_id = fields.Many2one(
        "sale.order",
        ondelete="set null",
        index=True,
        string="Sales Order",
    )
    state = fields.Selection(
        [
            ("pending_review", "Pending Review"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("auto_approved", "Auto-Approved"),
        ],
        default="pending_review",
        required=True,
        tracking=True,
        string="State",
    )

    # Issues
    issue_ids = fields.One2many("edi.order.issue", "review_id", string="Issues")
    issue_count = fields.Integer(
        compute="_compute_issue_counts",
        store=True,
        string="Issues",
    )
    blocking_issue_count = fields.Integer(
        compute="_compute_issue_counts",
        store=True,
        string="Blocking Issues",
    )

    # File metadata
    edi_file_hash = fields.Char(string="File Hash (SHA-256)")
    edi_filename = fields.Char(string="EDI Filename")
    edi_raw_data = fields.Text(string="Raw EDI Data")
    received_date = fields.Datetime(
        default=fields.Datetime.now,
        required=True,
        string="Received",
    )

    # Review metadata
    reviewed_by = fields.Many2one("res.users", string="Reviewed By")
    reviewed_date = fields.Datetime(string="Reviewed Date")
    notes = fields.Text(string="Notes")

    # SO lines (computed for safe inline display — dot-notation traversal unsupported in views)
    sale_order_line_ids = fields.One2many(
        "sale.order.line",
        compute="_compute_sale_order_line_ids",
        string="Sales Order Lines",
    )

    # PO change order support
    document_type = fields.Selection(
        [("new_order", "New Order"), ("change_order", "Change Order")],
        default="new_order",
        required=True,
        string="Document Type",
    )
    original_review_id = fields.Many2one(
        "edi.order.review",
        ondelete="set null",
        string="Original Review",
        help="For change orders: links back to the original new_order review",
    )
    change_summary = fields.Text(
        string="Change Summary",
        help="Human-readable diff of what changed in this change order",
    )
    change_order_ids = fields.One2many(
        "edi.order.review",
        "original_review_id",
        string="Change Orders",
    )

    # ── Computed ──────────────────────────────────────────────────────────

    @api.depends("sale_order_id.order_line")
    def _compute_sale_order_line_ids(self):
        for rec in self:
            rec.sale_order_line_ids = rec.sale_order_id.order_line if rec.sale_order_id else self.env["sale.order.line"]

    @api.depends("issue_ids", "issue_ids.severity")
    def _compute_issue_counts(self):
        for rec in self:
            rec.issue_count = len(rec.issue_ids)
            rec.blocking_issue_count = len(
                rec.issue_ids.filtered(lambda i: i.severity == "blocking")
            )

    # ── State Machine Actions ─────────────────────────────────────────────

    def action_approve(self):
        """
        Approve this review.

        For new orders: resolve all issues as 'accepted', confirm SO, queue ACK.
        For change orders: apply the diff to the existing SO, queue ACK.
        """
        for rec in self:
            if rec.state not in ("pending_review",):
                raise UserError(_("Only 'Pending Review' records can be approved."))

            if rec.document_type == "new_order":
                rec._approve_new_order()
            else:
                rec._approve_change_order()

            rec.write({
                "state": "approved",
                "reviewed_by": self.env.user.id,
                "reviewed_date": fields.Datetime.now(),
            })

    def action_approve_corrected(self):
        """
        Confirm SO at current (possibly manually corrected) line values.
        Issues already corrected inline — just confirm and queue ACK.
        """
        for rec in self:
            if rec.state != "pending_review":
                raise UserError(_("Only 'Pending Review' records can be approved."))
            if rec.sale_order_id and rec.sale_order_id.state == "draft":
                rec.sale_order_id.action_confirm()
            rec._queue_ack()
            rec.write({
                "state": "approved",
                "reviewed_by": self.env.user.id,
                "reviewed_date": fields.Datetime.now(),
            })
            self.env["edi.log"].log(
                rec.trading_partner_id, "internal", "review_approved", "success",
                "Review approved with corrections: %s" % rec.name,
                review=rec,
                sale_order=rec.sale_order_id,
            )

    def action_reject(self):
        """Cancel SO, queue rejection ACK."""
        for rec in self:
            if rec.state not in ("pending_review",):
                raise UserError(_("Only 'Pending Review' records can be rejected."))
            if rec.sale_order_id and rec.sale_order_id.state in ("draft", "sent"):
                rec.sale_order_id.action_cancel()
            rec._queue_ack(rejected=True)
            rec.write({
                "state": "rejected",
                "reviewed_by": self.env.user.id,
                "reviewed_date": fields.Datetime.now(),
            })
            self.env["edi.log"].log(
                rec.trading_partner_id, "internal", "review_rejected", "success",
                "Review rejected: %s" % rec.name,
                review=rec,
            )

    def action_reset_to_review(self):
        """Reset to pending_review. edi_manager group only."""
        for rec in self:
            if not self.env.user.has_group("mml_edi.group_edi_manager"):
                raise UserError(_("Only EDI Managers can reset a review."))
            rec.write({
                "state": "pending_review",
                "reviewed_by": False,
                "reviewed_date": False,
            })

    # ── Internal helpers ──────────────────────────────────────────────────

    def _approve_new_order(self):
        """Accept all pending issues and confirm the SO."""
        self.ensure_one()
        pending_issues = self.issue_ids.filtered(lambda i: i.resolution == "pending")
        pending_issues.action_accept()
        if self.sale_order_id and self.sale_order_id.state == "draft":
            self.sale_order_id.action_confirm()
        self._queue_ack()
        self.env["edi.log"].log(
            self.trading_partner_id, "internal", "review_approved", "success",
            "Review approved: %s" % self.name,
            review=self,
            sale_order=self.sale_order_id,
        )

    def _approve_change_order(self):
        """Apply the parsed change diff to the existing SO."""
        self.ensure_one()
        self.env["edi.processor"].apply_change_order(self)
        self._queue_ack()
        self.env["edi.log"].log(
            self.trading_partner_id, "internal", "po_change_applied", "success",
            "Change order applied to SO: %s — %s" % (
                self.sale_order_id.name if self.sale_order_id else "N/A",
                self.change_summary or "see change summary",
            ),
            review=self,
            sale_order=self.sale_order_id,
        )

    def _queue_ack(self, rejected: bool = False):
        """
        Queue ACK generation for this review.
        Currently implemented synchronously; swap for queue_job if needed.
        """
        self.ensure_one()
        try:
            parser = self.trading_partner_id.get_parser_instance()
            ack_bytes = parser.generate_ack(self)
            filename = "ACK_%s_%s_%s.edi" % (
                self.trading_partner_id.code,
                self.customer_po_number,
                self.id,
            )

            from .edi_ftp import EDIFTPHandler
            handler = EDIFTPHandler(self.trading_partner_id)
            with handler.connection():
                handler.upload_file(filename, ack_bytes)

            self.env["edi.log"].log(
                self.trading_partner_id, "outbound", "ack_sent", "success",
                "ACK sent: %s" % filename,
                filename=filename,
                review=self,
            )
        except Exception as e:
            _logger.exception("Failed to send ACK for review %s", self.name)
            self.env["edi.log"].log(
                self.trading_partner_id, "outbound", "ack_sent", "error",
                "ACK generation/upload failed: %s" % str(e),
                review=self,
                detail=str(e),
            )
