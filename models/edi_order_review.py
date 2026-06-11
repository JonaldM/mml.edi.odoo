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
        default=lambda self: self.env["ir.sequence"].sudo().next_by_code("edi.order.review"),
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
    warning_count = fields.Integer(
        compute="_compute_issue_counts",
        store=True,
        string="Warnings",
    )

    # File metadata
    edi_file_hash = fields.Char(string="File Hash (SHA-256)")
    edi_filename = fields.Char(string="EDI Filename")
    edi_raw_data = fields.Text(string="Raw EDI Data", groups="mml_edi.group_edi_manager")
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

    # ── ORDRSP / ACK status ───────────────────────────────────────────────

    ack_status = fields.Selection(
        [
            ("pending", "Review Pending"),
            ("queued", "Queued"),
            ("sent", "Sent"),
            ("failed", "Failed"),
        ],
        compute="_compute_ack_status", string="ORDRSP",
        help="Status of the outbound order response (ORDRSP/ACK) for this PO. "
             "One response covers all store-orders of a PO and is only sent once "
             "every store-review is resolved.",
    )
    ack_date = fields.Datetime(compute="_compute_ack_status", string="ORDRSP Sent")

    def _compute_ack_status(self):
        Log = self.env["edi.log"]
        for rec in self:
            if rec.state == "pending_review":
                rec.ack_status = "pending"
                rec.ack_date = False
                continue
            # The ACK filename is deterministic per (partner, PO, inbound-file).
            # All store-reviews of one inbound file share that file's hash, so
            # every sibling computes the same filename — and thus the same status.
            exchange_key = (rec.edi_file_hash or str(rec.id))[:8]
            filename = "ACK_%s_%s_%s.edi" % (
                rec.trading_partner_id.code, rec.customer_po_number, exchange_key,
            )
            logs = Log.search([
                ("trading_partner_id", "=", rec.trading_partner_id.id),
                ("event_type", "=", "ack_sent"),
                ("filename", "=", filename),
            ], order="timestamp desc")
            success = logs.filtered(lambda l: l.status == "success")[:1]
            if success:
                rec.ack_status = "sent"
                rec.ack_date = success.timestamp
            elif logs:  # only error rows so far
                rec.ack_status = "failed"
                rec.ack_date = False
            else:
                rec.ack_status = "queued"
                rec.ack_date = False

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
            rec.warning_count = len(
                rec.issue_ids.filtered(lambda i: i.severity == "warning")
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
            # State is now final → send the per-PO ACK (no-op until all stores done).
            rec._queue_ack()

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
            rec.write({
                "state": "approved",
                "reviewed_by": self.env.user.id,
                "reviewed_date": fields.Datetime.now(),
            })
            rec._queue_ack()
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
            rec.write({
                "state": "rejected",
                "reviewed_by": self.env.user.id,
                "reviewed_date": fields.Datetime.now(),
            })
            rec._queue_ack(rejected=True)
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
        # ACK is sent per-PO by the caller after the review state is written.
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
        # ACK is sent per-PO by the caller after the review state is written.
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
        Send ONE acknowledgement for the WHOLE PO, once every store-review for it
        is resolved.

        Briscoes (and EDI partners generally) expect a single per-PO response —
        verified against real Briscoes ORDRSPs, where one response covered all 47
        stores of a PO. Because we split a PO into one review per store, this is:

        - **per-PO**: the parser's generate_ack echoes the full PO (all stores);
        - **send-once**: a PO-keyed filename + an edi.log check make the upload
          idempotent no matter which store-review triggers it;
        - **deferred**: nothing is sent until every store-review of the PO has
          left 'pending_review', so the response reflects final accept/reject/qty
          for all stores.

        Callers must write the review's final state BEFORE calling this. The
        `rejected` arg is retained for call-compatibility only — per-line
        accept/reject is derived by the parser from each store-review's state.
        """
        self.ensure_one()
        partner = self.trading_partner_id
        po = self.customer_po_number

        siblings = self.search([
            ("trading_partner_id", "=", partner.id),
            ("customer_po_number", "=", po),
        ])
        pending = siblings.filtered(lambda r: r.state == "pending_review")
        if pending:
            _logger.info(
                "[EDI] Per-PO ACK for %s deferred — %d of %d store-review(s) still pending",
                po, len(pending), len(siblings),
            )
            return

        # Idempotency: one ACK file per PO *per exchange*, uploaded at most once.
        # The exchange identity (the triggering review's inbound file hash) is
        # part of the key so that each DISTINCT exchange for the same PO — an
        # approved ORDCHG, a re-order after cancellation, a correction arriving as
        # a new file — gets its own ORDRSP. A PO-only key would skip every ACK
        # after the first one. Every store-review of one inbound file shares that
        # file's hash, so a multi-store PO still yields exactly one ACK per file.
        exchange_key = (self.edi_file_hash or str(self.id))[:8]
        filename = "ACK_%s_%s_%s.edi" % (partner.code, po, exchange_key)
        if self.env["edi.log"].search_count([
            ("trading_partner_id", "=", partner.id),
            ("event_type", "=", "ack_sent"),
            ("status", "=", "success"),
            ("filename", "=", filename),
        ]):
            _logger.info("[EDI] Per-PO ACK already sent for %s (%s) — skipping", po, filename)
            return

        try:
            parser = partner.get_parser_instance()
            ack_bytes = parser.generate_ack(self)

            from .edi_ftp import EDIFTPHandler
            handler = EDIFTPHandler(partner)
            with handler.connection():
                handler.upload_file(filename, ack_bytes)

            self.env["edi.log"].log(
                partner, "outbound", "ack_sent", "success",
                "ACK sent (per-PO, %d store-order(s)): %s" % (len(siblings), filename),
                filename=filename,
                review=self,
            )
        except Exception as e:
            _logger.exception("Failed to send per-PO ACK for PO %s", po)
            self.env["edi.log"].log(
                partner, "outbound", "ack_sent", "error",
                "ACK generation/upload failed: %s" % str(e),
                review=self,
                detail=str(e),
            )
