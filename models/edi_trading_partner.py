# mml.edi/models/edi_trading_partner.py
import importlib
import logging
import string

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

_ALLOWED_PARSER_CLASSES = frozenset({
    'mml_edi.parsers.briscoes.BriscoesParser',
    'mml_edi.parsers.briscoes_idoc.BriscoesIDOCParser',
})


class EDITradingPartner(models.Model):
    _name = "edi.trading.partner"
    _description = "EDI Trading Partner"
    _order = "name"

    # ── Core ──────────────────────────────────────────────────────────────

    name = fields.Char(required=True, string="Partner Name")
    code = fields.Char(
        required=True,
        string="Partner Code",
        help="Unique short code used in references and file naming (e.g., BRISCOES)",
    )
    partner_id = fields.Many2one(
        "res.partner",
        required=True,
        string="Odoo Customer",
        domain=[("customer_rank", ">", 0)],
    )
    active = fields.Boolean(default=True)
    edi_format = fields.Selection(
        [
            ("edifact_d96a", "EDIFACT D96A"),
            ("idoc_xml", "SAP iDOC XML (ORDERSEXT)"),
            ("edifact_d01b", "EDIFACT D01B"),
            ("csv", "CSV"),
            ("custom", "Custom"),
        ],
        required=True,
        string="EDI Format",
    )
    parser_class = fields.Char(
        required=True,
        string="Parser Class",
        help="Python dotted path to the parser class (e.g., mml_edi.parsers.briscoes.BriscoesParser)",
    )

    # ── FTP Configuration ─────────────────────────────────────────────────

    ftp_protocol = fields.Selection(
        [("ftp", "FTP"), ("sftp", "SFTP")],
        required=True,
        default="ftp",
        string="FTP Protocol",
    )
    ftp_host = fields.Char(string="FTP Host")
    ftp_port = fields.Integer(string="FTP Port", default=21)
    ftp_user = fields.Char(
        string="FTP Username",
        groups='base.group_system',
    )
    ftp_password = fields.Char(
        string="FTP Password",
        password=True,
        groups='base.group_system',
        help="NOTE: Migrate to ir.config_parameter for multi-tenant deployments. "
             "Key pattern: mml_edi.{partner_code}.ftp_password",
    )
    ftp_inbox_path = fields.Char(string="Inbox Path")
    ftp_outbox_path = fields.Char(string="Outbox Path")
    ftp_test_inbox_path = fields.Char(string="Test Inbox Path")
    ftp_test_outbox_path = fields.Char(string="Test Outbox Path")
    sftp_host_key = fields.Char(
        string='SFTP Host Key (base64)',
        groups='base.group_system',
        help=(
            'Base64-encoded RSA server public key. '
            'Obtain with: ssh-keyscan -t rsa <host> | awk \'{print $3}\'. '
            'Required when ftp_protocol = sftp. '
            'Leave blank to REJECT all SFTP connections (fail-safe).'
        ),
    )
    environment = fields.Selection(
        [("production", "Production"), ("test", "Test")],
        required=True,
        default="production",
        string="Environment",
    )

    # ── Processing Rules ──────────────────────────────────────────────────

    pricelist_id = fields.Many2one(
        "product.pricelist",
        string="Pricelist",
        help="Used for price comparison on inbound orders",
    )
    price_tolerance_pct = fields.Float(
        default=0.0,
        string="Price Tolerance (%)",
        help="Auto-accept price discrepancies within this percentage (0.0 = exact match required)",
    )
    auto_confirm_clean = fields.Boolean(
        default=False,
        string="Auto-Confirm Clean Orders",
        help="Automatically confirm new orders with no blocking issues",
    )
    poll_interval_minutes = fields.Integer(
        default=15,
        string="Poll Interval (minutes)",
        help="How often to check FTP for new files (reflected in cron)",
    )
    order_split_mode = fields.Selection(
        [("per_store", "Per Store (one SO per store code)"), ("single", "Single (one PO = one SO)")],
        required=True,
        default="single",
        string="Order Split Mode",
    )
    product_match_field = fields.Selection(
        [
            ("barcode", "Barcode (EAN-13)"),
            ("default_code", "Internal Reference"),
            ("supplier_sku", "Supplier SKU (supplierinfo)"),
        ],
        required=True,
        default="barcode",
        string="Product Match Field",
    )
    client_ref_template = fields.Char(
        default="{po_number}",
        string="Client Reference Template",
        help="Python format string for SO client reference. Variables: {po_number}, {store_code}",
    )

    # ── Notifications ─────────────────────────────────────────────────────

    alert_email_ids = fields.Many2many(
        "res.partner",
        string="Alert Email Recipients",
    )
    alert_on_issues = fields.Boolean(
        default=True,
        string="Alert on Review Required",
        help="Send email when orders are routed to manual review",
    )

    # ── Computed ──────────────────────────────────────────────────────────

    def get_active_inbox_path(self):
        """Return inbox path based on current environment."""
        self.ensure_one()
        if self.environment == "test":
            return self.ftp_test_inbox_path
        return self.ftp_inbox_path

    def get_active_outbox_path(self):
        """Return outbox path based on current environment."""
        self.ensure_one()
        if self.environment == "test":
            return self.ftp_test_outbox_path
        return self.ftp_outbox_path

    # ── Constraints ───────────────────────────────────────────────────────

    _sql_constraints = [
        ("code_unique", "UNIQUE(code)", "Trading partner code must be unique."),
    ]

    # ── Actions ───────────────────────────────────────────────────────────

    def action_test_ftp_connection(self):
        """Test FTP connectivity. Called from form view button."""
        self.ensure_one()
        from .edi_ftp import EDIFTPHandler
        from ..parsers.base_parser import EDIFTPError

        try:
            handler = EDIFTPHandler(self)
            with handler.connection():
                files = handler.list_files()
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("FTP Connection Successful"),
                    "message": _("Connected to %s. Found %d file(s) in inbox.") % (self.ftp_host, len(files)),
                    "type": "success",
                },
            }
        except (EDIFTPError, Exception) as e:
            _logger.warning('[EDI] FTP connection test failed for %s: %s', self.code, e)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("FTP Connection Failed"),
                    "message": _("Could not connect to the FTP server. Check server logs for details."),
                    "type": "danger",
                    "sticky": True,
                },
            }

    def action_run_poll_now(self):
        """Trigger immediate FTP poll, bypassing cron schedule."""
        self.ensure_one()
        self.env["edi.processor"].poll_trading_partner(self)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Poll Complete"),
                "message": _("FTP poll completed for %s. Check logs for details.") % self.name,
                "type": "info",
            },
        }

    def get_parser_instance(self):
        """Dynamically load and instantiate the parser class."""
        self.ensure_one()
        if self.parser_class not in _ALLOWED_PARSER_CLASSES:
            raise UserError(
                _("Parser class '%s' is not in the approved list. "
                  "Contact your system administrator.") % self.parser_class
            )
        try:
            module_path, class_name = self.parser_class.rsplit(".", 1)
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
            return cls()
        except (ImportError, AttributeError, ValueError) as e:
            raise UserError(
                _("Cannot load parser class '%s': %s") % (self.parser_class, str(e))
            )

    def render_client_ref(self, po_number: str, store_code: str | None = None) -> str:
        """Render SO client reference from template."""
        self.ensure_one()
        template_str = self.client_ref_template or '$po_number'
        # Support both {po_number} and $po_number style templates
        template_str = template_str.replace('{po_number}', '$po_number').replace('{store_code}', '$store_code')
        t = string.Template(template_str)
        return t.safe_substitute(po_number=po_number, store_code=store_code or '')
