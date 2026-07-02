# mml.edi/models/edi_order_review.py
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .edi_processor import _commit_suppressed

_logger = logging.getLogger(__name__)


def _ack_filename(partner_code, po_number, exchange_key, attempt=1):
    """Deterministic per-exchange ACK filename (module-level so the pure
    pytest suite can cover the attempt rules without an Odoo env).

    Attempt 1 keeps the historical ``ACK_<partner>_<po>_<key>.edi`` shape so
    every ack_sent row already in production still matches its exchange.
    Attempt >= 2 — a manager reset AFTER the ACK was sent (IDEM-4) — appends
    ``_a<n>`` so the corrected ORDRSP goes out as a FRESH exchange instead of
    being silently suppressed by the previous attempt's success row.
    """
    base = "ACK_%s_%s_%s" % (partner_code, po_number, exchange_key)
    if attempt and attempt > 1:
        return "%s_a%d.edi" % (base, attempt)
    return "%s.edi" % base


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
    ack_attempt = fields.Integer(
        default=1, copy=False, string="ACK Attempt",
        help="Send attempt for this exchange's ORDRSP. Bumped (on every "
             "sibling store-review of the exchange) when a manager resets a "
             "review whose ACK was already sent, so the corrected re-approval "
             "goes out under a fresh filename instead of being suppressed by "
             "the earlier attempt's sent log (IDEM-4).",
    )

    def _ack_exchange_filename(self):
        """The deterministic ACK filename for this review's exchange.

        Single source of truth shared by _queue_ack, _compute_ack_status and
        edi.processor.retry_pending_acks — the send-once guard, the status
        display and the retry cron must all agree on the exchange identity.
        All store-reviews of one inbound file share the file's hash AND the
        attempt counter (see _supersede_sent_ack), so every sibling computes
        the same name.
        """
        self.ensure_one()
        exchange_key = (self.edi_file_hash or str(self.id))[:8]
        return _ack_filename(
            self.trading_partner_id.code, self.customer_po_number,
            exchange_key, self.ack_attempt or 1,
        )

    def _compute_ack_status(self):
        Log = self.env["edi.log"]
        for rec in self:
            if rec.state == "pending_review":
                rec.ack_status = "pending"
                rec.ack_date = False
                continue
            # The ACK filename is deterministic per (partner, PO, inbound-file,
            # attempt). All store-reviews of one inbound file share that file's
            # hash + attempt, so every sibling computes the same filename — and
            # thus the same status. A reset-after-sent bumps the attempt, so
            # this reads the LATEST attempt (the superseded sent log no longer
            # matches).
            filename = rec._ack_exchange_filename()
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
                # SS-1: corrected quantities still must not promise stock that
                # moved while the review sat open — re-clamp to live DC
                # availability immediately before confirm (no-op for
                # backorder partners). cap_to_current: the operator's manual
                # corrections are deliberate — availability may only pull a
                # line further DOWN, never restore it back up to the
                # customer's ordered qty (that would ship the exact quantity
                # the human just rejected).
                rec._reclamp_before_confirm(cap_to_current=True)
                rec.sale_order_id.action_confirm()
                # SS-3: reservations must actually exist after confirm.
                self.env["edi.processor"].verify_order_reservation(
                    rec.sale_order_id, rec.trading_partner_id)
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
            # IDEM-4: if this exchange's ORDRSP already went out, the old
            # ack_sent/success row would match the identical filename forever
            # and a corrected re-approve would silently never send. Bump the
            # attempt counter so the next send uses a fresh '_aN' filename
            # (the sent log is thereby superseded). No-op if nothing sent.
            rec._supersede_sent_ack()
            rec.write({
                "state": "pending_review",
                "reviewed_by": False,
                "reviewed_date": False,
            })

    # ── Internal helpers ──────────────────────────────────────────────────

    def _commit_ack_progress(self, partner):
        """Durably commit ACK bookkeeping (the pre-upload claim row and the
        post-upload sent row) — IDEM-3.

        Both rows must survive a later rollback of the enclosing request
        (e.g. a multi-select approve where a subsequent record raises):
        a claim without its sent row forces the verify-before-reupload path,
        and a sent row prevents any re-send at all. Guarded so a test
        harness's transaction is never really committed. Inside a poll the
        commit releases the per-partner advisory lock (transaction-scoped) —
        re-take it, exactly like _poll_commit; the poll marks itself via the
        edi_reservation_warned set it places in context.
        """
        if _commit_suppressed(self.env):
            return
        self.env.cr.commit()
        if isinstance(self.env.context.get("edi_reservation_warned"), set):
            self.env["edi.processor"]._acquire_poll_lock(partner)

    def _reclamp_before_confirm(self, cap_to_current=False):
        """Approve-time availability re-check (SS-1).

        Re-clamps every EDI line of the linked draft SO to live DC
        availability via edi.processor.reclamp_order_lines (no-op for
        backorder partners; never raises on lookup failures — availability
        degrades to 0.0, fail-closed). Any change is posted on the review's
        chatter, and a line clamped to ZERO raises the standard
        qty_shortfall issue so the operator sees exactly what was cut.
        ``cap_to_current=True`` (Approve-with-Corrections): operator qty
        reductions are respected — lines only move DOWN, never back up.
        Returns the reclamp change list.
        """
        self.ensure_one()
        so = self.sale_order_id
        if not so or so.state != "draft":
            return []
        changes = self.env["edi.processor"].reclamp_order_lines(
            so, self.trading_partner_id, cap_to_current=cap_to_current)
        if not changes:
            return []
        clamp_summary = "; ".join(
            "%s: %.0f -> %.0f (short %.0f)" % (
                c["line"].product_id.display_name or c["line"].name,
                c["old_qty"], c["new_qty"], c["shortfall"],
            ) for c in changes
        )
        # Audit trail on the review itself so the approver sees exactly what
        # diverged from the customer's requested quantities (mirrors the
        # ORDCHG path in edi.processor.apply_change_order).
        self.message_post(
            body=_("Quantities re-clamped to DC availability at approve: "
                   "%s") % clamp_summary,
            subtype_xmlid="mail.mt_note",
        )
        for c in changes:
            if c["new_qty"] == 0:
                self.env["edi.order.issue"].create({
                    "review_id": self.id,
                    "issue_type": "qty_shortfall",
                    "severity": "warning",
                    "description": "%s — re-clamped to ZERO at approve "
                                   "(was %.0f, ordered %.0f): no DC stock "
                                   "available (acknowledged in ORDRSP)" % (
                        c["line"].product_id.display_name or c["line"].name,
                        c["old_qty"], c["line"].edi_ordered_qty,
                    ),
                    "sale_order_line_id": c["line"].id,
                })
        return changes

    def _supersede_sent_ack(self):
        """Move the exchange to a fresh ACK attempt if its ORDRSP was sent.

        No-op when nothing was sent yet — the exchange key stays stable so a
        merely queued/deferred ACK is unaffected. All sibling store-reviews
        share the exchange filename, so they move to the new attempt
        together; legacy hash-less reviews (fallback key = review id) are
        each their own exchange and are bumped alone.
        """
        self.ensure_one()
        filename = self._ack_exchange_filename()
        Log = self.env["edi.log"]
        if not Log.search_count([
            ("trading_partner_id", "=", self.trading_partner_id.id),
            ("event_type", "=", "ack_sent"),
            ("status", "=", "success"),
            ("filename", "=", filename),
        ]):
            return
        records = self
        if self.edi_file_hash:
            records = self.search([
                ("trading_partner_id", "=", self.trading_partner_id.id),
                ("customer_po_number", "=", self.customer_po_number),
                ("edi_file_hash", "=", self.edi_file_hash),
            ])
        new_attempt = (self.ack_attempt or 1) + 1
        records.write({"ack_attempt": new_attempt})
        Log.log(
            self.trading_partner_id, "internal", "info", "warning",
            "Sent ACK %s superseded by reset of %s — the next approval sends "
            "attempt %d as a fresh exchange" % (filename, self.name, new_attempt),
            filename=filename,
            review=self,
        )

    def _approve_new_order(self):
        """Accept all pending issues and confirm the SO."""
        self.ensure_one()
        # SS-1: pending_review SOs hold no reservation, so stock can move
        # between parse and approve. Re-run the availability gate NOW so the
        # confirmed qty — and the ORDRSP, which reads the live line qty —
        # reflect stock at approve time, not parse time. Runs before the
        # issue-accept below so a line re-clamped to zero raises its issue in
        # time to be resolved with the rest.
        self._reclamp_before_confirm()
        pending_issues = self.issue_ids.filtered(lambda i: i.resolution == "pending")
        pending_issues.action_accept()
        if self.sale_order_id and self.sale_order_id.state == "draft":
            self.sale_order_id.action_confirm()
            # SS-3: reservations must actually exist after confirm — force +
            # verify (free_qty for every subsequent order depends on it).
            self.env["edi.processor"].verify_order_reservation(
                self.sale_order_id, self.trading_partner_id)
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
          idempotent no matter which store-review triggers it; a COMMITTED
          'ack_sending' claim row is written before the upload, and any retry
          after a claim verifies the outbound listing before re-uploading
          (IDEM-3 — FTP ghost-success must not double-send);
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
        # The exchange identity (the triggering review's inbound file hash +
        # ACK attempt) is part of the key so that each DISTINCT exchange for the
        # same PO — an approved ORDCHG, a re-order after cancellation, a
        # correction arriving as a new file, a corrected re-approve after reset
        # — gets its own ORDRSP. A PO-only key would skip every ACK after the
        # first one. Every store-review of one inbound file shares that file's
        # hash, so a multi-store PO still yields exactly one ACK per file.
        Log = self.env["edi.log"]
        filename = self._ack_exchange_filename()
        if Log.search_count([
            ("trading_partner_id", "=", partner.id),
            ("event_type", "=", "ack_sent"),
            ("status", "=", "success"),
            ("filename", "=", filename),
        ]):
            _logger.info("[EDI] Per-PO ACK already sent for %s (%s) — skipping", po, filename)
            return

        # IDEM-3: has a previous attempt already CLAIMED this exact filename?
        # If so, its upload may have been STORED even though we logged an error
        # (FTP ghost-success: data written, final 226 lost) — and the VAN may
        # have swept that copy already. After a claim, never blind-re-upload:
        # verify against the outbound listing and re-send only when the file is
        # provably absent.
        prior_claim = bool(Log.search_count([
            ("trading_partner_id", "=", partner.id),
            ("event_type", "=", "ack_sending"),
            ("filename", "=", filename),
        ]))

        try:
            parser = partner.get_parser_instance()
            ack_bytes = parser.generate_ack(self)

            from .edi_ftp import EDIFTPHandler
            handler = EDIFTPHandler(partner)

            if not prior_claim:
                # Committed send-claim (IDEM-3): must be durable BEFORE any
                # byte hits the wire so a worker death / ghost-success during
                # the upload leaves the evidence that forces the verify path
                # above on retry. Commit guarded like edi.processor's
                # _poll_commit — the test runner's transaction never commits.
                Log.log(
                    partner, "outbound", "ack_sending", "success",
                    "ACK upload claimed (pre-upload durability marker): %s" % filename,
                    filename=filename,
                    review=self,
                )
                self._commit_ack_progress(partner)

            with handler.connection():
                if prior_claim and filename in handler.list_outbox_files():
                    # Ghost-success recovery: the file already sits in the
                    # outbound dir, so the earlier upload DID land. Mark the
                    # exchange sent WITHOUT re-uploading — a second copy
                    # would double-respond to the partner.
                    Log.log(
                        partner, "outbound", "ack_sent", "success",
                        "ACK verified already delivered — ghost-success "
                        "recovery, NOT re-uploaded: %s" % filename,
                        filename=filename,
                        review=self,
                    )
                    self._commit_ack_progress(partner)
                    return
                handler.upload_file(filename, ack_bytes)

            Log.log(
                partner, "outbound", "ack_sent", "success",
                "ACK sent (per-PO, %d store-order(s)): %s" % (len(siblings), filename),
                filename=filename,
                review=self,
            )
            # The sent row must be as durable as the claim row: if this
            # request later rolls back (e.g. a multi-select approve where a
            # subsequent record raises), the exchange would be left
            # claim-only and the retry could re-deliver an ORDRSP the VAN
            # already swept — the exact double-respond IDEM-3 prevents.
            self._commit_ack_progress(partner)
        except Exception as e:
            _logger.exception("Failed to send per-PO ACK for PO %s", po)
            # filename included so the failure is attributable to its exchange
            # — _compute_ack_status's 'failed' branch matches on it.
            self.env["edi.log"].log(
                partner, "outbound", "ack_sent", "error",
                "ACK generation/upload failed: %s" % str(e),
                filename=filename,
                review=self,
                detail=str(e),
            )
