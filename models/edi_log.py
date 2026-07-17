# mml.edi/models/edi_log.py
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class EDILog(models.Model):
    _name = "edi.log"
    _description = "EDI Audit Log"
    _order = "timestamp desc"
    _log_access = False  # Don't add create_uid/write_uid overhead

    name = fields.Char(
        default=lambda self: self.env["ir.sequence"].sudo().next_by_code("edi.log"),
        copy=False,
        readonly=True,
        string="Reference",
    )
    trading_partner_id = fields.Many2one(
        "edi.trading.partner",
        index=True,
        ondelete="restrict",
        string="Trading Partner",
    )
    timestamp = fields.Datetime(
        default=fields.Datetime.now,
        required=True,
        index=True,
        string="Timestamp",
    )
    direction = fields.Selection(
        [("inbound", "Inbound"), ("outbound", "Outbound"), ("internal", "Internal")],
        required=True,
        string="Direction",
    )
    event_type = fields.Selection(
        [
            ("file_download", "File Downloaded"),
            ("file_parse", "File Parsed"),
            ("order_created", "Order Created"),
            ("duplicate_skipped", "Duplicate Skipped"),
            ("ack_sending", "ACK Sending (upload claim)"),
            ("ack_sent", "ACK Sent"),
            ("contrl_sent", "CONTRL Sent"),
            ("contrl_received", "CONTRL Received"),
            ("review_approved", "Review Approved"),
            ("review_rejected", "Review Rejected"),
            ("po_change_applied", "PO Change Applied"),
            ("error", "Error"),
            ("ftp_connection", "FTP Connection"),
            ("info", "Info"),
        ],
        required=True,
        string="Event Type",
    )
    filename = fields.Char(string="Filename")
    file_hash = fields.Char(string="File Hash (SHA-256)", index=True)
    sale_order_id = fields.Many2one("sale.order", index=True, ondelete="set null", string="Sales Order")
    review_id = fields.Many2one("edi.order.review", index=True, ondelete="set null", string="Review")
    user_id = fields.Many2one(
        "res.users",
        default=lambda self: self.env.user,
        string="User",
    )
    status = fields.Selection(
        [("success", "Success"), ("warning", "Warning"), ("error", "Error")],
        required=True,
        string="Status",
    )
    message = fields.Text(required=True, string="Message")
    detail = fields.Text(string="Technical Detail", groups='mml_edi.group_edi_manager')

    # ── Helpers ───────────────────────────────────────────────────────────

    @api.model
    def log(
        self,
        trading_partner,
        direction: str,
        event_type: str,
        status: str,
        message: str,
        *,
        filename: str = None,
        file_hash: str = None,
        sale_order=None,
        review=None,
        detail: str = None,
    ) -> "EDILog":
        """
        Create a log entry. Use this helper everywhere instead of create()
        directly so signature stays consistent.

        Example:
            self.env['edi.log'].log(
                partner, 'inbound', 'file_download', 'success',
                'Downloaded orders.edi', filename='orders.edi'
            )
        """
        vals = {
            "trading_partner_id": trading_partner.id if trading_partner else False,
            "direction": direction,
            "event_type": event_type,
            "status": status,
            "message": message,
        }
        if filename:
            vals["filename"] = filename
        if file_hash:
            vals["file_hash"] = file_hash
        if sale_order:
            vals["sale_order_id"] = sale_order.id
        if review:
            vals["review_id"] = review.id
        if detail:
            vals["detail"] = detail

        # Log critical errors to Python logger too
        if status == "error":
            _logger.error("[EDI] %s | %s | %s", trading_partner and trading_partner.code or "N/A", event_type, message)

        return self.create(vals)
