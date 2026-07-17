# mml.edi/models/edi_trading_partner.py
import importlib
import logging
import re
import string
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..utils.credential_store import decrypt_credential, encrypt_credential

_logger = logging.getLogger(__name__)

_ALLOWED_TEMPLATE_VARS = frozenset({'po_number', 'store_code'})
_TEMPLATE_VAR_RE = re.compile(r'\$\{?(\w+)\}?')

_ALLOWED_PARSER_CLASSES = frozenset({
    # NOTE: 'mml_edi.parsers.briscoes.BriscoesParser' (EDIFACT D96A) is
    # deliberately NOT allow-listed. It ships carton quantities as eaches — the
    # CT->EA conversion that was applied to the iDOC path (the production path)
    # was never ported to EDIFACT, and the processor ignores parsed_line.uom /
    # carton_qty — so an EDIFACT order would under/over-state quantities to the
    # customer. Re-enable ONLY after the EDIFACT quantity (CT->EA) logic is fixed
    # and verified against real samples. Briscoes currently sends SAP iDOC XML.
    'mml_edi.parsers.briscoes_idoc.BriscoesIDOCParser',
    # Animates (EDIFACT D.01B / EANCOM, via SPS Commerce). Orders are in eaches
    # (QTY+21:<n>:EA), so the processor using parsed_line.quantity directly is
    # correct here (no CT->EA conversion needed, unlike the Briscoes EDIFACT path).
    # ISC (PIA+5:IN)->buyer_article_no; MML code (PIA+1:SA)->product_code/vendor_code.
    'mml_edi.parsers.animates.AnimatesParser',
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
        [("ftp", "FTP"), ("sftp", "SFTP"), ("localdir", "Local Directory")],
        required=True,
        default="ftp",
        string="Transport",
        help="Transport for this partner's exchanges. 'Local Directory' polls "
             "and writes plain directories on this server (inbox/outbox paths "
             "are absolute local paths) — used for drill rigs and for bridging "
             "gateways (e.g. a future AS2 sidecar) that deliver files to disk.",
    )
    ftp_host = fields.Char(string="FTP Host")
    ftp_port = fields.Integer(string="FTP Port", default=21)
    ftp_user = fields.Char(
        string="FTP Username",
        groups='base.group_system',
    )
    ftp_password = fields.Char(
        string="FTP Password",
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
    pricelist_gst_warning = fields.Char(
        compute="_compute_pricelist_gst_warning",
        string="Pricelist GST Notice",
        help="Non-blocking advisory shown when the assigned pricelist references "
        "products carrying a GST-inclusive tax. EDI prices are ex-GST.",
    )
    warehouse_id = fields.Many2one(
        "stock.warehouse",
        string="Fulfilment Warehouse",
        help="Warehouse new orders for this trading partner are created on "
        "(overrides the EDI user default). Blank = company default.",
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
    oos_policy = fields.Selection(
        [("backorder", "Accept in full (backorder shortfall)"),
         ("short_ship", "Short-ship available qty (ACK shortfall)")],
        required=True,
        default="backorder",
        string="Out-of-Stock Policy",
        help="backorder: accept the order in full and let the short qty backorder "
             "(legacy behaviour). short_ship: reduce each line to the qty available "
             "at the fulfilment DC and acknowledge the shortfall in the ORDRSP, so the "
             "WMS only ever receives stock we can actually ship.",
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

    # ── EDI Identity (EDIFACT interchange envelope) ─────────────────────────
    # Used by build_unb() to populate the UNB sender/recipient identity for
    # partners exchanging UN/EDIFACT (e.g. Animates via SPS Commerce). Not
    # meaningful for iDOC partners (Briscoes).

    edi_sender_id = fields.Char(
        string="EDI Sender ID",
        help="Our identity in the UNB interchange header sent to this partner "
             "(e.g. our GLN: '9419416000008'). Portal test-mailbox convention "
             "appends a literal 'T' suffix for the TEST environment "
             "(e.g. '9419416000008T', qualifier ZZZ) — some partners issue a "
             "distinct test sender id rather than deriving it; set the exact "
             "value the partner's onboarding/portal assigned for each "
             "environment. See get_unb_sender().",
    )
    edi_sender_qualifier = fields.Char(
        string="EDI Sender Qualifier",
        default="ZZZ",
        help="UNB S002 qualifier for edi_sender_id (DE0007), e.g. 'ZZZ' "
             "(mutually defined) or '14' (GLN). Animates uses ZZZ for the "
             "supplier side per the MIG worked examples.",
    )
    supplier_gln = fields.Char(
        string="Supplier GLN",
        help="Our GS1 Global Location Number (GLN), qualifier '14'. Used where "
             "the partner's MIG specifically requires a GLN-qualified identity "
             "(as opposed to edi_sender_id/edi_sender_qualifier, which may be "
             "ZZZ-qualified for the interchange envelope itself).",
    )
    animates_vendor_code = fields.Char(
        string="Animates Vendor Code",
        help="The supplier code Animates assigned us (e.g. 'V1058'), used for "
             "NAD+SU and PIA+1:SA identity in outbound Animates messages "
             "(ORDRSP/DESADV/INVOIC). Blank on non-Animates partners.",
    )

    def get_unb_sender(self):
        """Return (id, qualifier) for OUR identity in an outbound UNB.

        Prefers the explicit edi_sender_id/edi_sender_qualifier pair; falls
        back to supplier_gln (qualifier '14') if only the GLN is configured.
        Raises UserError if neither is set — callers building a PRODUCTION
        payload must not silently fall back to a placeholder identity
        (see AN-01 / OPS-identity).
        """
        self.ensure_one()
        if self.edi_sender_id:
            return self.edi_sender_id, (self.edi_sender_qualifier or "ZZZ")
        if self.supplier_gln:
            return self.supplier_gln, "14"
        raise UserError(
            _("Trading partner '%s' has no EDI sender identity configured "
              "(edi_sender_id or supplier_gln). Set one before sending "
              "EDIFACT interchanges.") % self.name
        )

    def get_unb_recipient(self):
        """Return (id, qualifier) for the Animates side of an outbound UNB.

        Switches on ``environment``: the TEST portal mailbox is a DISTINCT
        recipient identity 'TST1ANIMATES' (per the ORDRSP/ORDERS MIG worked
        examples), not the production 'ANIMATES' id with a flag — sending to
        the wrong one silently misroutes the interchange in SPS Commerce.
        """
        self.ensure_one()
        recipient = "TST1ANIMATES" if self.environment == "test" else "ANIMATES"
        return recipient, "ZZZ"

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

    # ── Circuit Breaker ───────────────────────────────────────────────────

    circuit_failure_count = fields.Integer(
        default=0,
        string="Consecutive Poll Failures",
        readonly=True,
        help="Number of consecutive FTP poll failures since last success.",
    )
    circuit_open_since = fields.Datetime(
        string="Circuit Open Since",
        readonly=True,
        help="Timestamp when the circuit breaker tripped. Cleared on successful poll.",
    )
    circuit_failure_threshold = fields.Integer(
        default=5,
        string="Failure Threshold",
        help="Number of consecutive failures before the circuit opens and polling pauses.",
    )
    circuit_cooldown_minutes = fields.Integer(
        default=60,
        string="Cooldown (minutes)",
        help="Minutes to wait before retrying after the circuit trips.",
    )

    # ── EDI Interchange Identity ──────────────────────────────────────────
    # Used to build the EDIFACT UNB interchange envelope for VAN partners
    # (e.g. Animates via SPS Commerce). iDOC partners (Briscoes) route by GLN
    # in the iDOC header rather than a UNB envelope, so these may be blank for
    # them. Stored config consumed when the EDIFACT sender path is enabled.

    edi_sender_id = fields.Char(
        string="EDI Sender ID",
        help="Interchange sender identification (typically the supplier GLN) "
             "placed in the EDIFACT UNB segment.",
    )
    edi_sender_qualifier = fields.Char(
        string="Sender Qualifier",
        default="14",
        help="UNB sender qualifier (14 = GLN / GS1 Global Location Number).",
    )
    supplier_gln = fields.Char(
        string="Supplier GLN",
        help="MML's GS1 Global Location Number for this trading relationship.",
    )
    vendor_code = fields.Char(
        string="Vendor Code",
        help="The supplier/vendor account code this partner identifies MML by "
             "in their system (echoed on acknowledgements where required).",
    )

    # ── Store map (read-only view of the customer's delivery children) ────
    # The per-store split routes each store-order to its mapped delivery
    # contact — the child partners of the Odoo customer. Exposed read-only here
    # so the Stores tab can show the map; the Seed Store Partners wizard is the
    # write path.
    store_partner_ids = fields.One2many(
        related="partner_id.child_ids",
        string="Store / Delivery Contacts",
        readonly=True,
    )

    # ── Dashboard / health (computed, non-stored) ─────────────────────────

    last_poll_date = fields.Datetime(
        compute="_compute_health", string="Last Inbound Activity",
        help="Timestamp of the most recent inbound EDI log entry for this partner.",
    )
    pending_review_count = fields.Integer(
        compute="_compute_health", string="Orders Awaiting Review",
    )
    error_recent_count = fields.Integer(
        compute="_compute_health", string="Errors (24h)",
    )
    health_state = fields.Selection(
        [
            ("never", "Never Polled"),
            ("circuit_open", "Circuit Open"),
            ("error", "Recent Errors"),
            ("review", "Awaiting Review"),
            ("ok", "Healthy"),
        ],
        compute="_compute_health", string="Health",
        help="At-a-glance connection health: circuit-breaker state, recent "
             "errors, and pending reviews, worst-first.",
    )

    # ── Setup checklist (computed, non-stored) ────────────────────────────

    setup_credentials_ok = fields.Boolean(compute="_compute_setup")
    setup_pricelist_ok = fields.Boolean(compute="_compute_setup")
    setup_stores_ok = fields.Boolean(compute="_compute_setup")
    setup_tested_ok = fields.Boolean(compute="_compute_setup")
    setup_complete = fields.Boolean(compute="_compute_setup", string="Setup Complete")

    def _compute_health(self):
        Log = self.env["edi.log"]
        Review = self.env["edi.order.review"]
        now = fields.Datetime.now()
        cutoff = now - timedelta(hours=24)
        for partner in self:
            last_in = Log.search(
                [("trading_partner_id", "=", partner.id), ("direction", "=", "inbound")],
                order="timestamp desc", limit=1,
            )
            partner.last_poll_date = last_in.timestamp if last_in else False
            partner.pending_review_count = Review.search_count([
                ("trading_partner_id", "=", partner.id),
                ("state", "=", "pending_review"),
            ])
            partner.error_recent_count = Log.search_count([
                ("trading_partner_id", "=", partner.id),
                ("status", "=", "error"),
                ("timestamp", ">=", cutoff),
            ])
            if partner.circuit_open_since:
                partner.health_state = "circuit_open"
            elif not partner.last_poll_date:
                partner.health_state = "never"
            elif partner.error_recent_count:
                partner.health_state = "error"
            elif partner.pending_review_count:
                partner.health_state = "review"
            else:
                partner.health_state = "ok"

    def _compute_setup(self):
        for partner in self:
            sudo = partner.sudo()  # credential fields are group-restricted
            partner.setup_credentials_ok = bool(
                partner.ftp_host and sudo.ftp_user and sudo.ftp_password
            )
            partner.setup_pricelist_ok = bool(partner.pricelist_id)
            if partner.order_split_mode == "per_store":
                partner.setup_stores_ok = bool(
                    partner.partner_id and partner.partner_id.child_ids
                )
            else:
                partner.setup_stores_ok = True
            partner.setup_tested_ok = bool(self.env["edi.log"].search_count([
                ("trading_partner_id", "=", partner.id),
                ("event_type", "in", ("ftp_connection", "file_download")),
                ("status", "=", "success"),
            ]))
            partner.setup_complete = (
                partner.setup_credentials_ok
                and partner.setup_pricelist_ok
                and partner.setup_stores_ok
                and partner.setup_tested_ok
            )

    def action_open_pending_reviews(self):
        """Smart-button: open this partner's pending review queue."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Pending Reviews — %s") % self.name,
            "res_model": "edi.order.review",
            "view_mode": "list,form",
            "domain": [
                ("trading_partner_id", "=", self.id),
                ("state", "=", "pending_review"),
            ],
            "context": {"search_default_trading_partner_id": self.id},
        }

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

    _code_unique = models.Constraint(
        'UNIQUE(code)',
        'Trading partner code must be unique.',
    )

    # ── Field validators ──────────────────────────────────────────────────

    @api.constrains('client_ref_template')
    def _validate_client_ref_template(self):
        for rec in self:
            if not rec.client_ref_template:
                continue
            found_vars = set(_TEMPLATE_VAR_RE.findall(rec.client_ref_template))
            unknown = found_vars - _ALLOWED_TEMPLATE_VARS
            if unknown:
                raise ValidationError(
                    "client_ref_template contains unknown variable(s): "
                    "%s. Allowed: $po_number, $store_code"
                    % ', '.join(sorted(unknown))
                )

    def _pricelist_gst_inclusive_message(self):
        """Return an advisory message if the assigned pricelist references
        products carrying a GST-inclusive (``price_include``) tax, else False.

        EDI prices are ex-GST. This used to be a hard ``@api.constrains`` that
        blocked the save, but ``price_include`` is only a *proxy*: a product can
        legitimately carry both an ex-GST and an inc-GST tax (retail vs
        wholesale / multi-company) while its EDI pricelist value is ex-GST — e.g.
        the live Animates pricelist ($8.10 ex-GST on products that also hold an
        inc-GST retail tax). The price comparison uses the *raw* pricelist value,
        and the per-line ``price_discrepancy`` check at order time is the
        authoritative guard, so this is now a non-blocking notice rather than a
        hard fail (which would wrongly block onboarding a valid ex-GST pricelist).
        """
        self.ensure_one()
        pricelist = self.pricelist_id
        if not pricelist:
            return False
        flagged = []
        for item in pricelist.item_ids or ():
            target = item.product_id or item.product_tmpl_id
            if not target:
                continue
            if any(getattr(tax, 'price_include', False) for tax in target.taxes_id or ()):
                flagged.append(target.display_name)
        if not flagged:
            return False
        sample = ', '.join(flagged[:3])
        more = '' if len(flagged) <= 3 else ' (+%d more)' % (len(flagged) - 3)
        return (
            "Heads up: pricelist '%s' has %d product(s) carrying a GST-inclusive "
            "tax (%s%s). EDI prices are ex-GST — confirm this pricelist's prices "
            "are ex-GST. The order-time price-discrepancy check remains the "
            "authoritative guard." % (pricelist.display_name, len(flagged), sample, more)
        )

    @api.depends('pricelist_id')
    def _compute_pricelist_gst_warning(self):
        for rec in self:
            rec.pricelist_gst_warning = rec._pricelist_gst_inclusive_message()

    # ── ORM overrides (credential encryption) ────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('ftp_password'):
                vals['ftp_password'] = encrypt_credential(vals['ftp_password'], self.env)
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('ftp_password'):
            vals['ftp_password'] = encrypt_credential(vals['ftp_password'], self.env)
        return super().write(vals)

    def get_ftp_password(self) -> str:
        """Return the decrypted FTP password for this trading partner.

        Use this instead of accessing partner.ftp_password directly to ensure
        Fernet-encrypted values are transparently decrypted.
        """
        self.ensure_one()
        return decrypt_credential(self.ftp_password, self.env)

    # ── Actions ───────────────────────────────────────────────────────────

    def action_test_ftp_connection(self):
        """Test FTP connectivity. Called from form view button."""
        self.ensure_one()
        from .edi_ftp import get_transport_handler
        from ..parsers.base_parser import EDIFTPError

        try:
            handler = get_transport_handler(self)
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
            try:
                module = importlib.import_module(module_path)
            except ImportError:
                # Inside Odoo, modules live under odoo.addons.* namespace
                module = importlib.import_module("odoo.addons." + module_path)
            cls = getattr(module, class_name)
            return cls()
        except (ImportError, AttributeError, ValueError) as e:
            raise UserError(
                _("Cannot load parser class '%s': %s") % (self.parser_class, str(e))
            )

    def render_client_ref(self, po_number: str, store_code: str | None = None) -> str:
        """Render SO client reference from template."""
        self.ensure_one()
        template_str = self.client_ref_template or '${po_number}'
        # Support both {po_number} and $po_number style templates. Use BRACED
        # placeholders (${po_number}) so a following separator char does not get
        # absorbed into the identifier — e.g. "{po_number}_{store_code}" must not
        # become "$po_number_..." (string.Template reads "po_number_" as the name,
        # leaving "$po_number_" unsubstituted).
        template_str = (
            template_str
            .replace('{po_number}', '${po_number}')
            .replace('{store_code}', '${store_code}')
            .replace('$po_number', '${po_number}')
            .replace('$store_code', '${store_code}')
            .replace('${{po_number}}', '${po_number}')      # guard double-brace
            .replace('${{store_code}}', '${store_code}')
        )
        t = string.Template(template_str)
        return t.safe_substitute(po_number=po_number, store_code=store_code or '')


def circuit_is_open(partner) -> bool:
    """
    Return True if the circuit breaker is open (polling should be skipped).

    States:
      CLOSED:    failure_count < threshold — poll normally
      OPEN:      failure_count >= threshold AND within cooldown — skip
      HALF-OPEN: failure_count >= threshold AND cooldown expired — allow one attempt
    """
    if partner.circuit_failure_count < partner.circuit_failure_threshold:
        return False
    if not partner.circuit_open_since:
        return False
    from datetime import datetime, timezone, timedelta
    cooldown = timedelta(minutes=partner.circuit_cooldown_minutes)
    open_since = partner.circuit_open_since
    if not open_since.tzinfo:
        open_since = open_since.replace(tzinfo=timezone.utc)
    elapsed = datetime.now(timezone.utc) - open_since
    return elapsed < cooldown
