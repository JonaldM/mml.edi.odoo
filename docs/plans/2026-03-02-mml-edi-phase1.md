# mml_edi Phase 1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a complete, installable Odoo 19 EDI module (`mml_edi`) that replaces the Briscoes .NET service, supports multiple trading partners via configuration, and handles both new POs and PO change orders through a review dashboard.

**Architecture:** Customer-agnostic processing engine driven by a `ParsedOrder` intermediate representation. Per-partner parser classes (pluggable). Four parallel build tracks: data layer (models), logic layer (engine + FTP), UI/config layer (views + security + data), quality layer (tests + wiring). Track 0 (base contracts) must complete before any parallel tracks begin.

**Tech Stack:** Odoo 19, Python 3.12, `ftplib` (FTP), `paramiko` (SFTP), Odoo ORM, OWL views, `ir.cron` for scheduling, standard Odoo test framework (`TransactionCase`).

**Working directory:** `E:\ClaudeCode\projects\mml.odoo.apps\briscoes.edi\mml.edi\`

**Design doc:** `docs/plans/2026-03-02-mml-edi-phase1-design.md`

---

## Execution Notes

- Each file is built test-first where applicable (models/engine/FTP)
- Views and data files have no unit tests — verify by Odoo install
- Run Odoo tests with: `odoo-bin -d ODOOTEST --test-enable --stop-after-init -i mml_edi`
- Commit after every task
- **Track dependencies:** Task 1 → Tasks 3–10 (parallel) → Tasks 11–18 (parallel with 8–10) → Tasks 19–26

---

## Track 0 — Foundation (Do First, Everything Depends On This)

### Task 1: Create module skeleton

**Files:**
- Create: `mml.edi/__init__.py`
- Create: `mml.edi/models/__init__.py`
- Create: `mml.edi/parsers/__init__.py`
- Create: `mml.edi/wizards/__init__.py`
- Create: `mml.edi/tests/__init__.py`
- Create: `mml.edi/data/` (empty dir placeholder)
- Create: `mml.edi/views/` (empty dir placeholder)
- Create: `mml.edi/security/` (empty dir placeholder)
- Create: `mml.edi/static/description/` (empty dir placeholder)

**Step 1: Create all `__init__.py` files**

`mml.edi/__init__.py`:
```python
from . import models
from . import parsers
from . import wizards
```

`mml.edi/models/__init__.py`:
```python
# Populated as models are added
```

`mml.edi/parsers/__init__.py`:
```python
# Populated as parsers are added
```

`mml.edi/wizards/__init__.py`:
```python
# Populated as wizards are added
```

`mml.edi/tests/__init__.py`:
```python
# Populated as tests are added
```

**Step 2: Create placeholder dirs** (just create them — Odoo needs them to exist)
```bash
mkdir -p mml.edi/data mml.edi/views mml.edi/security mml.edi/static/description
```

**Step 3: Commit**
```bash
git add mml.edi/
git commit -m "feat(mml_edi): scaffold module directory structure"
```

---

### Task 2: Write base parser contracts (`parsers/base_parser.py`)

This file defines the interface contracts all tracks depend on. Write it first and treat it as immutable for the rest of the sprint.

**Files:**
- Create: `mml.edi/parsers/base_parser.py`

**Step 1: Write the file**

```python
# mml.edi/parsers/base_parser.py
"""
Base EDI parser contracts.

All parser implementations must subclass BaseEDIParser.
ParsedOrder and ParsedOrderLine are the intermediate representation
passed between parsers and the processing engine.

These interfaces are stable — do not change field names or method
signatures without updating all parsers and the processor.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # Odoo model types referenced by string only to avoid circular imports


@dataclass
class ParsedOrderLine:
    """Represents a single line item from an EDI order document."""

    product_code: str          # Matched against trading_partner.product_match_field
    description: str
    quantity: float
    unit_price: float          # Price from EDI — what the customer expects to pay
    line_number: int           # EDI line number for ACK reference
    uom: str | None = None     # Unit of measure from EDI (may differ from Odoo UOM)


@dataclass
class ParsedOrder:
    """
    Standardised intermediate representation.

    Parser output → Processing engine input.
    One ParsedOrder per store/SO that will be created.
    """

    po_number: str
    order_date: date
    lines: list[ParsedOrderLine]

    # None for single-order customers (order_split_mode == 'single')
    store_code: str | None = None

    requested_delivery_date: date | None = None

    # Delivery address GLN/code — looked up against res.partner.ref
    delivery_address_code: str | None = None

    # 'new_order' or 'change_order'. Parsers set this from EDI message type.
    # If the format doesn't distinguish, detect by matching PO number to existing SO.
    document_type: str = "new_order"

    # Optional reason code / description from the EDI change order message
    change_reason: str | None = None

    # Raw EDI content stored for audit trail and debugging (set by processor)
    raw_data: str | None = None

    def content_hash(self) -> str:
        """SHA-256 of raw_data for deduplication. Must be set before calling."""
        if not self.raw_data:
            raise ValueError("raw_data must be set before computing content_hash")
        return hashlib.sha256(self.raw_data.encode()).hexdigest()


class BaseEDIParser(ABC):
    """
    Abstract base class for EDI parsers.

    One subclass per trading partner (or per EDI format if multiple
    partners share the same format).

    The parser is stateless — all configuration comes via the
    trading_partner argument.
    """

    @abstractmethod
    def parse_file(
        self, raw_content: bytes, trading_partner
    ) -> list[ParsedOrder]:
        """
        Parse raw file bytes into a list of ParsedOrder objects.

        One file may contain multiple orders (e.g., one per store).
        Returns an empty list if the file contains no processable orders.

        Args:
            raw_content: Raw bytes downloaded from FTP
            trading_partner: edi.trading.partner record (Odoo model instance)

        Raises:
            EDIParseError: If the file is structurally invalid and cannot
                           be partially parsed. For line-level errors, create
                           a ParsedOrderLine with quantity=0 and flag in issues.
        """
        raise NotImplementedError

    @abstractmethod
    def generate_ack(self, review_record) -> bytes:
        """
        Generate acknowledgement file bytes for a processed order.

        Args:
            review_record: edi.order.review record

        Returns:
            Raw bytes to upload to the FTP outbox
        """
        raise NotImplementedError


class EDIParseError(Exception):
    """Raised when an EDI file is structurally invalid."""
    pass


class EDIFTPError(Exception):
    """Raised on FTP connection or transfer failures."""
    pass
```

**Step 2: Update `parsers/__init__.py`**

```python
from .base_parser import BaseEDIParser, ParsedOrder, ParsedOrderLine, EDIParseError, EDIFTPError
```

**Step 3: Commit**
```bash
git add mml.edi/parsers/
git commit -m "feat(mml_edi): add base parser contracts (ParsedOrder, BaseEDIParser)"
```

---

## Track 1 — Data Layer (Parallel after Task 2)

### Task 3: `edi.trading.partner` model

**Files:**
- Create: `mml.edi/models/edi_trading_partner.py`
- Modify: `mml.edi/models/__init__.py`

**Step 1: Write the model**

```python
# mml.edi/models/edi_trading_partner.py
import importlib
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


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
    ftp_user = fields.Char(string="FTP Username")
    ftp_password = fields.Char(string="FTP Password")  # Stored encrypted in Odoo's DB
    ftp_inbox_path = fields.Char(string="Inbox Path")
    ftp_outbox_path = fields.Char(string="Outbox Path")
    ftp_test_inbox_path = fields.Char(string="Test Inbox Path")
    ftp_test_outbox_path = fields.Char(string="Test Outbox Path")
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

    @property
    def active_inbox_path(self):
        """Return inbox path based on current environment."""
        if self.environment == "test":
            return self.ftp_test_inbox_path
        return self.ftp_inbox_path

    @property
    def active_outbox_path(self):
        """Return outbox path based on current environment."""
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
        from ..models.edi_ftp import EDIFTPHandler
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
        except EDIFTPError as e:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("FTP Connection Failed"),
                    "message": str(e),
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
        template = self.client_ref_template or "{po_number}"
        return template.format(po_number=po_number, store_code=store_code or "")
```

**Step 2: Update `models/__init__.py`**

```python
from . import edi_trading_partner
```

**Step 3: Commit**
```bash
git add mml.edi/models/edi_trading_partner.py mml.edi/models/__init__.py
git commit -m "feat(mml_edi): add edi.trading.partner model"
```

---

### Task 4: `edi.log` model

**Files:**
- Create: `mml.edi/models/edi_log.py`
- Modify: `mml.edi/models/__init__.py`

**Step 1: Write the model**

```python
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
        default=lambda self: self.env["ir.sequence"].next_by_code("edi.log"),
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
            ("ack_sent", "ACK Sent"),
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
    file_hash = fields.Char(string="File Hash (SHA-256)")
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
    detail = fields.Text(string="Technical Detail")

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
```

**Step 2: Update `models/__init__.py`** — append:
```python
from . import edi_log
```

**Step 3: Commit**
```bash
git add mml.edi/models/edi_log.py mml.edi/models/__init__.py
git commit -m "feat(mml_edi): add edi.log audit model"
```

---

### Task 5: `edi.order.issue` model

**Files:**
- Create: `mml.edi/models/edi_order_issue.py`
- Modify: `mml.edi/models/__init__.py`

**Step 1: Write the model**

```python
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
    edi_line_data = fields.Text(string="EDI Line Data")  # Raw EDI line for reference
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
```

**Step 2: Update `models/__init__.py`** — append:
```python
from . import edi_order_issue
```

**Step 3: Commit**
```bash
git add mml.edi/models/edi_order_issue.py mml.edi/models/__init__.py
git commit -m "feat(mml_edi): add edi.order.issue model with resolution workflow"
```

---

### Task 6: `edi.order.review` model

**Files:**
- Create: `mml.edi/models/edi_order_review.py`
- Modify: `mml.edi/models/__init__.py`

**Step 1: Write the model**

```python
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
        # The processor stores the pending changes as a structured note.
        # This method applies them. Implementation delegates to the processor
        # to keep model layer thin.
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
            filename = f"ACK_{self.trading_partner_id.code}_{self.customer_po_number}_{self.id}.edi"

            from ..models.edi_ftp import EDIFTPHandler
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
```

**Step 2: Update `models/__init__.py`** — append:
```python
from . import edi_order_review
```

**Step 3: Commit**
```bash
git add mml.edi/models/edi_order_review.py mml.edi/models/__init__.py
git commit -m "feat(mml_edi): add edi.order.review model with state machine and change order support"
```

---

### Task 7: `sale.order` and `sale.order.line` inherits

**Files:**
- Create: `mml.edi/models/sale_order.py`
- Modify: `mml.edi/models/__init__.py`

**Step 1: Write the file**

```python
# mml.edi/models/sale_order.py
from odoo import api, fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    edi_trading_partner_id = fields.Many2one(
        "edi.trading.partner",
        ondelete="set null",
        index=True,
        string="EDI Trading Partner",
        help="Set when this SO was created from an EDI order",
    )
    edi_review_id = fields.Many2one(
        "edi.order.review",
        ondelete="set null",
        index=True,
        string="EDI Review",
    )
    is_edi_order = fields.Boolean(
        compute="_compute_is_edi_order",
        store=True,
        string="EDI Order",
    )

    @api.depends("edi_trading_partner_id")
    def _compute_is_edi_order(self):
        for order in self:
            order.is_edi_order = bool(order.edi_trading_partner_id)


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    edi_line_number = fields.Integer(string="EDI Line #")
    edi_price = fields.Float(
        string="EDI Price",
        digits="Product Price",
        help="Price as received in the EDI file",
    )
    edi_system_price = fields.Float(
        string="System Price at EDI",
        digits="Product Price",
        help="Pricelist price at time of EDI processing",
    )
    edi_price_discrepancy = fields.Boolean(
        compute="_compute_edi_price_discrepancy",
        store=True,
        string="Price Discrepancy",
    )
    edi_qty_shortfall = fields.Float(
        string="Stock Shortfall",
        digits="Product Unit of Measure",
        help="Qty requested minus qty available at time of EDI processing (0 if sufficient)",
    )

    @api.depends("edi_price", "edi_system_price")
    def _compute_edi_price_discrepancy(self):
        for line in self:
            line.edi_price_discrepancy = (
                line.edi_price > 0
                and line.edi_system_price > 0
                and round(line.edi_price, 4) != round(line.edi_system_price, 4)
            )
```

**Step 2: Update `models/__init__.py`** — append:
```python
from . import sale_order
```

**Step 3: Commit**
```bash
git add mml.edi/models/sale_order.py mml.edi/models/__init__.py
git commit -m "feat(mml_edi): add EDI fields to sale.order and sale.order.line"
```

---

## Track 2 — Logic Layer (Parallel with Track 1 after Task 2)

### Task 8: FTP Handler (`edi_ftp.py`)

**Files:**
- Create: `mml.edi/models/edi_ftp.py`

**Step 1: Write failing test first**

Create `mml.edi/tests/test_ftp_handler.py`:
```python
# mml.edi/tests/test_ftp_handler.py
"""
FTP handler unit tests — all FTP calls are mocked.
These do not require a live FTP server.
"""
from unittest.mock import MagicMock, patch, call
import pytest


def make_mock_partner(protocol="ftp", host="ftp.test.com", port=21,
                      user="user", password="pass",
                      inbox="/inbox", outbox="/outbox",
                      environment="production"):
    partner = MagicMock()
    partner.ftp_protocol = protocol
    partner.ftp_host = host
    partner.ftp_port = port
    partner.ftp_user = user
    partner.ftp_password = password
    partner.active_inbox_path = inbox
    partner.active_outbox_path = outbox
    partner.environment = environment
    partner.code = "TEST"
    return partner


class TestEDIFTPHandlerFTP:
    def test_connect_ftp_calls_login(self):
        from mml_edi.models.edi_ftp import EDIFTPHandler
        partner = make_mock_partner()
        handler = EDIFTPHandler(partner)
        with patch("ftplib.FTP") as mock_ftp_cls:
            mock_ftp = MagicMock()
            mock_ftp_cls.return_value.__enter__ = MagicMock(return_value=mock_ftp)
            handler.connect()
            # Should call FTP() and login
            mock_ftp_cls.assert_called()

    def test_list_files_returns_list(self):
        from mml_edi.models.edi_ftp import EDIFTPHandler
        partner = make_mock_partner()
        handler = EDIFTPHandler(partner)
        with patch("ftplib.FTP") as mock_ftp_cls:
            mock_ftp = MagicMock()
            mock_ftp.nlst.return_value = ["order1.edi", "order2.edi"]
            mock_ftp_cls.return_value = mock_ftp
            handler._ftp = mock_ftp
            files = handler.list_files()
            assert files == ["order1.edi", "order2.edi"]

    def test_retry_on_connection_failure(self):
        from mml_edi.models.edi_ftp import EDIFTPHandler, EDIFTPError
        partner = make_mock_partner()
        handler = EDIFTPHandler(partner)
        with patch("ftplib.FTP") as mock_ftp_cls, \
             patch("time.sleep"):  # Don't actually sleep in tests
            mock_ftp_cls.side_effect = Exception("Connection refused")
            with pytest.raises(EDIFTPError):
                handler.connect()
            # Should have tried 3 times
            assert mock_ftp_cls.call_count == 3

    def test_move_to_processed_renames_file(self):
        from mml_edi.models.edi_ftp import EDIFTPHandler
        partner = make_mock_partner()
        handler = EDIFTPHandler(partner)
        mock_ftp = MagicMock()
        handler._ftp = mock_ftp
        handler.move_to_processed("order1.edi")
        mock_ftp.rename.assert_called_once()
        # New name should contain 'processed'
        new_name = mock_ftp.rename.call_args[0][1]
        assert "processed" in new_name.lower() or ".done" in new_name.lower()
```

**Step 2: Run test — confirm it fails**
```bash
python -m pytest mml.edi/tests/test_ftp_handler.py -v
# Expected: ImportError or ModuleNotFoundError
```

**Step 3: Implement `edi_ftp.py`**

```python
# mml.edi/models/edi_ftp.py
"""
FTP/SFTP connection handler for EDI trading partners.

Supports both plain FTP (ftplib) and SFTP (paramiko).
All operations are logged to edi.log via the trading partner.
"""

import ftplib
import io
import logging
import time
from contextlib import contextmanager
from datetime import datetime

from ..parsers.base_parser import EDIFTPError

_logger = logging.getLogger(__name__)

_RETRY_DELAYS = [2, 4, 8]  # seconds, exponential backoff
_CONNECT_TIMEOUT = 30  # seconds
_TRANSFER_TIMEOUT = 60  # seconds


class EDIFTPHandler:
    """
    Manages FTP/SFTP connections for a single trading partner.

    Usage:
        handler = EDIFTPHandler(trading_partner)
        with handler.connection():
            files = handler.list_files()
            content = handler.download_file(files[0])
            handler.move_to_processed(files[0])
    """

    def __init__(self, trading_partner):
        self.partner = trading_partner
        self._ftp = None  # ftplib.FTP or paramiko.SFTPClient

    # ── Connection lifecycle ──────────────────────────────────────────────

    def connect(self) -> None:
        """
        Establish connection. Retries 3 times with exponential backoff.
        Raises EDIFTPError on final failure.
        """
        last_exc = None
        for attempt, delay in enumerate([0] + _RETRY_DELAYS, start=1):
            if delay:
                time.sleep(delay)
            try:
                if self.partner.ftp_protocol == "sftp":
                    self._connect_sftp()
                else:
                    self._connect_ftp()
                _logger.info("[EDI FTP] Connected to %s (%s)", self.partner.ftp_host, self.partner.code)
                return
            except Exception as exc:
                last_exc = exc
                _logger.warning(
                    "[EDI FTP] Connection attempt %d/%d failed for %s: %s",
                    attempt, len(_RETRY_DELAYS) + 1, self.partner.code, exc,
                )

        raise EDIFTPError(
            "Failed to connect to %s after %d attempts: %s"
            % (self.partner.ftp_host, len(_RETRY_DELAYS) + 1, last_exc)
        )

    def disconnect(self) -> None:
        """Clean disconnect."""
        if self._ftp is None:
            return
        try:
            if self.partner.ftp_protocol == "sftp":
                self._ftp.close()
            else:
                self._ftp.quit()
        except Exception:
            pass  # Best-effort disconnect
        finally:
            self._ftp = None

    @contextmanager
    def connection(self):
        """Context manager — auto connect/disconnect."""
        self.connect()
        try:
            yield self
        finally:
            self.disconnect()

    # ── File operations ───────────────────────────────────────────────────

    def list_files(self) -> list[str]:
        """List files in the active inbox directory. Returns filenames only."""
        inbox = self.partner.active_inbox_path
        try:
            if self.partner.ftp_protocol == "sftp":
                return [f.filename for f in self._ftp.listdir_attr(inbox)
                        if not f.filename.startswith(".")]
            else:
                return self._ftp.nlst(inbox)
        except Exception as exc:
            raise EDIFTPError("list_files failed on %s: %s" % (inbox, exc)) from exc

    def download_file(self, filename: str) -> bytes:
        """Download a single file. Returns raw bytes."""
        inbox = self.partner.active_inbox_path
        filepath = f"{inbox}/{filename}"
        buf = io.BytesIO()
        try:
            if self.partner.ftp_protocol == "sftp":
                self._ftp.getfo(filepath, buf)
            else:
                self._ftp.retrbinary(f"RETR {filepath}", buf.write)
            return buf.getvalue()
        except Exception as exc:
            raise EDIFTPError("download_file failed for %s: %s" % (filepath, exc)) from exc

    def upload_file(self, filename: str, content: bytes) -> None:
        """Upload a file to the active outbox directory."""
        outbox = self.partner.active_outbox_path
        filepath = f"{outbox}/{filename}"
        buf = io.BytesIO(content)
        try:
            if self.partner.ftp_protocol == "sftp":
                self._ftp.putfo(buf, filepath)
            else:
                self._ftp.storbinary(f"STOR {filepath}", buf)
        except Exception as exc:
            raise EDIFTPError("upload_file failed for %s: %s" % (filepath, exc)) from exc

    def move_to_processed(self, filename: str) -> None:
        """
        Rename a processed file in the inbox to prevent re-processing.
        New name: {original}.processed.{timestamp}
        """
        inbox = self.partner.active_inbox_path
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        old_path = f"{inbox}/{filename}"
        new_path = f"{inbox}/{filename}.processed.{timestamp}"
        try:
            if self.partner.ftp_protocol == "sftp":
                self._ftp.rename(old_path, new_path)
            else:
                self._ftp.rename(old_path, new_path)
        except Exception as exc:
            raise EDIFTPError("move_to_processed failed for %s: %s" % (old_path, exc)) from exc

    # ── Internal ──────────────────────────────────────────────────────────

    def _connect_ftp(self):
        ftp = ftplib.FTP(timeout=_CONNECT_TIMEOUT)
        ftp.connect(self.partner.ftp_host, self.partner.ftp_port)
        ftp.login(self.partner.ftp_user, self.partner.ftp_password)
        ftp.set_pasv(True)
        self._ftp = ftp

    def _connect_sftp(self):
        try:
            import paramiko
        except ImportError:
            raise EDIFTPError(
                "paramiko is required for SFTP connections. Install it with: pip install paramiko"
            )
        transport = paramiko.Transport((self.partner.ftp_host, self.partner.ftp_port))
        transport.connect(username=self.partner.ftp_user, password=self.partner.ftp_password)
        self._ftp = paramiko.SFTPClient.from_transport(transport)
```

**Step 4: Run tests — should pass**
```bash
python -m pytest mml.edi/tests/test_ftp_handler.py -v
# Expected: 4 PASS
```

**Step 5: Update `models/__init__.py`** — append:
```python
from . import edi_ftp
```

**Step 6: Commit**
```bash
git add mml.edi/models/edi_ftp.py mml.edi/tests/test_ftp_handler.py mml.edi/models/__init__.py
git commit -m "feat(mml_edi): add FTP/SFTP handler with retry and context manager"
```

---

### Task 9: Processing Engine (`edi_processor.py`)

This is the core — it ties everything together. Build in two parts: new order flow, then change order flow.

**Files:**
- Create: `mml.edi/models/edi_processor.py`
- Modify: `mml.edi/models/__init__.py`

**Step 1: Write failing processor tests**

Create `mml.edi/tests/test_processor.py`:
```python
# mml.edi/tests/test_processor.py
"""
Processor pipeline tests using mock ParsedOrder data.
No FTP or real Odoo DB required — uses common.py fixtures.

These are Odoo TransactionCase tests.
"""
from odoo.tests.common import TransactionCase
from .common import (
    make_clean_parsed_order,
    make_price_discrepancy_parsed_order,
    make_product_not_found_parsed_order,
    make_change_order_parsed_order,
    EDITestSetup,
)


class TestNewOrderFlow(TransactionCase, EDITestSetup):
    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()

    def test_clean_order_auto_approved(self):
        """A clean order with auto_confirm_clean=True is confirmed automatically."""
        self.trading_partner.auto_confirm_clean = True
        order = make_clean_parsed_order()
        self.processor.process_parsed_order(order, self.trading_partner, "test.edi", "hash123")
        review = self.env["edi.order.review"].search([
            ("customer_po_number", "=", order.po_number)
        ])
        self.assertEqual(review.state, "auto_approved")
        self.assertEqual(review.sale_order_id.state, "sale")

    def test_price_discrepancy_routes_to_review(self):
        """An order with price discrepancy goes to pending_review."""
        order = make_price_discrepancy_parsed_order()
        self.processor.process_parsed_order(order, self.trading_partner, "test.edi", "hash456")
        review = self.env["edi.order.review"].search([
            ("customer_po_number", "=", order.po_number)
        ])
        self.assertEqual(review.state, "pending_review")
        self.assertGreater(review.blocking_issue_count, 0)

    def test_duplicate_file_hash_skipped(self):
        """Re-processing a file with the same hash is skipped."""
        order = make_clean_parsed_order()
        self.processor.process_parsed_order(order, self.trading_partner, "test.edi", "samehash")
        # Process again with same hash
        self.processor.process_parsed_order(order, self.trading_partner, "test.edi", "samehash")
        reviews = self.env["edi.order.review"].search([
            ("customer_po_number", "=", order.po_number)
        ])
        self.assertEqual(len(reviews), 1)  # Only one created

    def test_duplicate_so_ref_skipped(self):
        """Re-processing a PO that already has a confirmed SO is skipped."""
        order = make_clean_parsed_order()
        self.trading_partner.auto_confirm_clean = True
        self.processor.process_parsed_order(order, self.trading_partner, "test.edi", "hash1")
        # Process again with different hash (same PO number)
        self.processor.process_parsed_order(order, self.trading_partner, "test2.edi", "hash2")
        reviews = self.env["edi.order.review"].search([
            ("customer_po_number", "=", order.po_number)
        ])
        self.assertEqual(len(reviews), 1)  # Second was skipped


class TestChangeOrderFlow(TransactionCase, EDITestSetup):
    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()

    def test_change_order_always_routes_to_review(self):
        """Change orders always go to pending_review, even with auto_confirm_clean."""
        self.trading_partner.auto_confirm_clean = True
        # First create the original order
        new_order = make_clean_parsed_order(po_number="PO001")
        self.processor.process_parsed_order(new_order, self.trading_partner, "new.edi", "hash_new")
        # Now send a change order
        change = make_change_order_parsed_order(po_number="PO001")
        self.processor.process_parsed_order(change, self.trading_partner, "change.edi", "hash_chg")
        change_review = self.env["edi.order.review"].search([
            ("customer_po_number", "=", "PO001"),
            ("document_type", "=", "change_order"),
        ])
        self.assertEqual(change_review.state, "pending_review")

    def test_change_order_approval_updates_so(self):
        """Approving a change order updates the existing SO."""
        # Create original order
        new_order = make_clean_parsed_order(po_number="PO002", qty=10)
        self.trading_partner.auto_confirm_clean = True
        self.processor.process_parsed_order(new_order, self.trading_partner, "new.edi", "hash_n2")
        original_review = self.env["edi.order.review"].search([
            ("customer_po_number", "=", "PO002"),
            ("document_type", "=", "new_order"),
        ])
        # Change order: qty changed to 20
        change = make_change_order_parsed_order(po_number="PO002", qty=20)
        self.processor.process_parsed_order(change, self.trading_partner, "chg.edi", "hash_c2")
        change_review = self.env["edi.order.review"].search([
            ("customer_po_number", "=", "PO002"),
            ("document_type", "=", "change_order"),
        ])
        change_review.action_approve()
        so_line = original_review.sale_order_id.order_line[0]
        self.assertEqual(so_line.product_uom_qty, 20)
```

**Step 2: Write `common.py` test fixtures**

```python
# mml.edi/tests/common.py
"""Shared test fixtures for mml_edi tests."""
from datetime import date, timedelta
from mml_edi.parsers.base_parser import ParsedOrder, ParsedOrderLine


def make_parsed_line(
    product_code="TEST001",
    description="Test Product",
    quantity=10.0,
    unit_price=9.99,
    line_number=1,
):
    return ParsedOrderLine(
        product_code=product_code,
        description=description,
        quantity=quantity,
        unit_price=unit_price,
        line_number=line_number,
    )


def make_clean_parsed_order(po_number="TESTPO001", store_code=None, qty=10.0):
    """Returns a ParsedOrder with one valid line — no issues expected."""
    return ParsedOrder(
        po_number=po_number,
        store_code=store_code,
        order_date=date.today(),
        requested_delivery_date=date.today() + timedelta(days=7),
        lines=[make_parsed_line(quantity=qty, unit_price=9.99)],
        document_type="new_order",
        raw_data="MOCK_EDI_CONTENT",
    )


def make_price_discrepancy_parsed_order(po_number="TESTPO_PRICE"):
    """Returns a ParsedOrder where the EDI price does not match the pricelist."""
    return ParsedOrder(
        po_number=po_number,
        order_date=date.today(),
        lines=[make_parsed_line(unit_price=999.99)],  # Very high price
        document_type="new_order",
        raw_data="MOCK_PRICE_DISCREPANCY_EDI",
    )


def make_product_not_found_parsed_order(po_number="TESTPO_NOTFOUND"):
    """Returns a ParsedOrder with a product code that doesn't exist in Odoo."""
    return ParsedOrder(
        po_number=po_number,
        order_date=date.today(),
        lines=[make_parsed_line(product_code="NONEXISTENT_SKU_99999")],
        document_type="new_order",
        raw_data="MOCK_NOTFOUND_EDI",
    )


def make_change_order_parsed_order(po_number="TESTPO001", qty=20.0):
    """Returns a change order for an existing PO."""
    return ParsedOrder(
        po_number=po_number,
        order_date=date.today(),
        requested_delivery_date=date.today() + timedelta(days=14),
        lines=[make_parsed_line(quantity=qty, unit_price=9.99)],
        document_type="change_order",
        change_reason="Quantity update",
        raw_data="MOCK_CHANGE_ORDER_EDI",
    )


class EDITestSetup:
    """
    Mixin for Odoo TransactionCase tests.

    Provides setup_edi_test_data() which creates a minimal trading partner
    and references to the processor model.

    Subclasses must call self.setup_edi_test_data() in setUp().
    """

    def setup_edi_test_data(self):
        """
        Create test trading partner and a test product.

        NOTE: This requires a real Odoo TransactionCase DB.
        Tests will be rolled back after each test method.
        """
        # Create or find a test customer
        test_partner = self.env["res.partner"].create({
            "name": "EDI Test Customer",
            "customer_rank": 1,
        })

        # Create a test pricelist
        pricelist = self.env["product.pricelist"].create({
            "name": "EDI Test Pricelist",
            "currency_id": self.env.company.currency_id.id,
        })

        # Create a test product (barcode matches mock ParsedOrderLine)
        self.test_product = self.env["product.product"].create({
            "name": "EDI Test Product",
            "barcode": "TEST001",
            "list_price": 9.99,
            "type": "product",
        })

        # Create pricelist item for this product
        self.env["product.pricelist.item"].create({
            "pricelist_id": pricelist.id,
            "product_id": self.test_product.id,
            "compute_price": "fixed",
            "fixed_price": 9.99,
        })

        # Create trading partner
        self.trading_partner = self.env["edi.trading.partner"].create({
            "name": "EDI Test Partner",
            "code": "TESTPARTNER",
            "partner_id": test_partner.id,
            "edi_format": "csv",
            "parser_class": "mml_edi.parsers.briscoes.BriscoesParser",
            "ftp_protocol": "ftp",
            "ftp_host": "ftp.test.local",
            "ftp_port": 21,
            "environment": "test",
            "pricelist_id": pricelist.id,
            "price_tolerance_pct": 0.0,
            "auto_confirm_clean": False,
            "order_split_mode": "single",
            "product_match_field": "barcode",
            "client_ref_template": "{po_number}",
        })

        self.processor = self.env["edi.processor"]
```

**Step 3: Write `edi_processor.py`**

```python
# mml.edi/models/edi_processor.py
"""
Customer-agnostic EDI processing engine.

Entry point: edi.processor.run_scheduled_poll() — called by ir.cron.
Also callable per-partner via trading_partner.action_run_poll_now().

This model has no database fields — it's a service model (transient).
"""
import hashlib
import json
import logging
from datetime import date

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class EDIProcessor(models.AbstractModel):
    """
    EDI processing engine. AbstractModel — no table, pure service.

    The public API used by cron and manual triggers:
        self.env['edi.processor'].run_scheduled_poll()
        self.env['edi.processor'].poll_trading_partner(partner)
        self.env['edi.processor'].process_parsed_order(order, partner, filename, file_hash)
        self.env['edi.processor'].apply_change_order(review)
    """

    _name = "edi.processor"
    _description = "EDI Processing Engine"

    # ── Cron entry point ──────────────────────────────────────────────────

    @api.model
    def run_scheduled_poll(self):
        """Called by ir.cron. Polls all active trading partners."""
        partners = self.env["edi.trading.partner"].search([("active", "=", True)])
        _logger.info("[EDI] Scheduled poll: %d active trading partners", len(partners))
        for partner in partners:
            try:
                self.poll_trading_partner(partner)
            except Exception as exc:
                _logger.exception("[EDI] Poll failed for partner %s", partner.code)
                self.env["edi.log"].log(
                    partner, "inbound", "error", "error",
                    "Scheduled poll failed: %s" % str(exc),
                    detail=str(exc),
                )

    # ── Per-partner poll ──────────────────────────────────────────────────

    @api.model
    def poll_trading_partner(self, partner):
        """Download and process all files from a trading partner's FTP inbox."""
        from ..models.edi_ftp import EDIFTPHandler
        from ..parsers.base_parser import EDIFTPError

        _logger.info("[EDI] Polling %s", partner.code)
        handler = EDIFTPHandler(partner)

        try:
            with handler.connection():
                files = handler.list_files()
                _logger.info("[EDI] %s: %d file(s) in inbox", partner.code, len(files))

                for filename in files:
                    try:
                        content = handler.download_file(filename)
                        file_hash = hashlib.sha256(content).hexdigest()

                        self.env["edi.log"].log(
                            partner, "inbound", "file_download", "success",
                            "Downloaded: %s (%d bytes)" % (filename, len(content)),
                            filename=filename, file_hash=file_hash,
                        )

                        with self.env.cr.savepoint():
                            self._process_file(content, file_hash, filename, partner)

                        handler.move_to_processed(filename)

                    except Exception as exc:
                        _logger.exception("[EDI] Error processing file %s for %s", filename, partner.code)
                        self.env["edi.log"].log(
                            partner, "inbound", "error", "error",
                            "Error processing %s: %s" % (filename, str(exc)),
                            filename=filename, detail=str(exc),
                        )

        except EDIFTPError as exc:
            self.env["edi.log"].log(
                partner, "inbound", "ftp_connection", "error",
                "FTP connection failed: %s" % str(exc),
                detail=str(exc),
            )
            raise

    # ── File processing ───────────────────────────────────────────────────

    def _process_file(self, content: bytes, file_hash: str, filename: str, partner):
        """Parse a downloaded file and dispatch each ParsedOrder."""
        # File-level dedup
        if self._is_file_duplicate(file_hash, partner):
            self.env["edi.log"].log(
                partner, "inbound", "duplicate_skipped", "warning",
                "Duplicate file skipped (same hash already processed): %s" % filename,
                filename=filename, file_hash=file_hash,
            )
            return

        parser = partner.get_parser_instance()
        raw_text = content.decode("utf-8", errors="replace")
        parsed_orders = parser.parse_file(content, partner)

        self.env["edi.log"].log(
            partner, "inbound", "file_parse", "success",
            "Parsed %d order(s) from %s" % (len(parsed_orders), filename),
            filename=filename, file_hash=file_hash,
        )

        for order in parsed_orders:
            order.raw_data = raw_text
            with self.env.cr.savepoint():
                self.process_parsed_order(order, partner, filename, file_hash)

    @api.model
    def process_parsed_order(
        self, order, partner, filename: str, file_hash: str
    ):
        """
        Process a single ParsedOrder through the full pipeline.
        Called per ParsedOrder — one FTP file may produce multiple calls.
        """
        client_ref = partner.render_client_ref(order.po_number, order.store_code)

        if order.document_type == "change_order":
            self._process_change_order(order, partner, client_ref, filename, file_hash)
        else:
            self._process_new_order(order, partner, client_ref, filename, file_hash)

    # ── New order flow ────────────────────────────────────────────────────

    def _process_new_order(self, order, partner, client_ref: str, filename: str, file_hash: str):
        """Full new order pipeline."""
        # SO-level dedup
        existing_so = self._find_existing_so(client_ref)
        if existing_so and existing_so.state in ("draft", "sent", "sale", "done"):
            self.env["edi.log"].log(
                partner, "inbound", "duplicate_skipped", "warning",
                "Duplicate PO skipped — SO %s already exists (state: %s)" % (
                    existing_so.name, existing_so.state),
                filename=filename, file_hash=file_hash,
                sale_order=existing_so,
            )
            return

        # Resolve delivery partner
        delivery_partner = self._resolve_delivery_partner(partner, order)

        # Create SO in draft
        so = self.env["sale.order"].create({
            "partner_id": delivery_partner.id,
            "partner_invoice_id": partner.partner_id.id,
            "pricelist_id": partner.pricelist_id.id if partner.pricelist_id else False,
            "client_order_ref": client_ref,
            "commitment_date": order.requested_delivery_date and
                               fields.Datetime.to_datetime(str(order.requested_delivery_date)),
            "edi_trading_partner_id": partner.id,
            "company_id": self.env.company.id,
        })

        blocking_issues = []
        so_lines_created = []

        for parsed_line in order.lines:
            issues, sol = self._process_order_line(parsed_line, so, partner)
            blocking_issues.extend([i for i in issues if i.get("severity") == "blocking"])
            if sol:
                so_lines_created.append(sol)

        # Create review record
        review = self.env["edi.order.review"].create({
            "trading_partner_id": partner.id,
            "customer_po_number": order.po_number,
            "store_code": order.store_code,
            "sale_order_id": so.id,
            "edi_file_hash": file_hash,
            "edi_filename": filename,
            "edi_raw_data": order.raw_data,
            "document_type": "new_order",
        })

        # Link SO to review
        so.edi_review_id = review.id

        # Route: auto-approve or pending
        if not blocking_issues and partner.auto_confirm_clean:
            so.action_confirm()
            review.write({"state": "auto_approved"})
            review._queue_ack()
            self.env["edi.log"].log(
                partner, "inbound", "order_created", "success",
                "Auto-approved: SO %s created from %s" % (so.name, filename),
                filename=filename, sale_order=so, review=review,
            )
        else:
            review.write({"state": "pending_review"})
            self.env["edi.log"].log(
                partner, "inbound", "order_created", "warning" if blocking_issues else "success",
                "Routed to review: SO %s — %d blocking issue(s)" % (so.name, len(blocking_issues)),
                filename=filename, sale_order=so, review=review,
            )
            if partner.alert_on_issues and blocking_issues:
                self._send_review_alert(partner, review)

    # ── Order line processing ─────────────────────────────────────────────

    def _process_order_line(self, parsed_line, so, partner) -> tuple:
        """
        Process one ParsedOrderLine. Creates SO line and any issues.
        Returns (list_of_issue_vals, sale_order_line_or_None).
        """
        issues = []

        # Product lookup
        product = self._find_product(parsed_line.product_code, partner)
        if not product:
            # Blocking: can't create SO line without a product
            self.env["edi.order.issue"].create({
                "review_id": so.edi_review_id.id if so.edi_review_id else False,
                "issue_type": "product_not_found",
                "severity": "blocking",
                "description": "Product not found: code '%s' (%s) — %s" % (
                    parsed_line.product_code,
                    partner.product_match_field,
                    parsed_line.description,
                ),
                "edi_line_data": json.dumps({
                    "product_code": parsed_line.product_code,
                    "description": parsed_line.description,
                    "quantity": parsed_line.quantity,
                    "unit_price": parsed_line.unit_price,
                }),
            })
            issues.append({"severity": "blocking", "type": "product_not_found"})
            return issues, None

        # Create SO line
        sol = self.env["sale.order.line"].create({
            "order_id": so.id,
            "product_id": product.id,
            "product_uom_qty": parsed_line.quantity,
            "price_unit": parsed_line.unit_price,  # EDI price
            "edi_line_number": parsed_line.line_number,
            "edi_price": parsed_line.unit_price,
        })

        # Stock check
        qty_available = product.with_context(warehouse=so.warehouse_id.id).qty_available
        if qty_available < parsed_line.quantity:
            shortfall = parsed_line.quantity - qty_available
            sol.edi_qty_shortfall = shortfall
            self.env["edi.order.issue"].create({
                "review_id": False,  # Set after review created
                "_pending_so_id": so.id,  # Temp — linked in _link_issues_to_review
                "issue_type": "qty_shortfall",
                "severity": "warning",
                "description": "Stock shortfall: %s — requested %.0f, available %.0f, shortfall %.0f" % (
                    product.name, parsed_line.quantity, qty_available, shortfall,
                ),
                "sale_order_line_id": sol.id,
            })
            issues.append({"severity": "warning", "type": "qty_shortfall"})

        # Price comparison
        system_price = self._get_pricelist_price(product, parsed_line.quantity, partner)
        sol.edi_system_price = system_price

        if system_price is not None:
            tolerance = partner.price_tolerance_pct / 100.0
            if system_price > 0:
                diff_pct = abs(parsed_line.unit_price - system_price) / system_price
            else:
                diff_pct = 1.0 if parsed_line.unit_price != 0 else 0.0

            if diff_pct > tolerance:
                self.env["edi.order.issue"].create({
                    "review_id": False,  # Set after review created
                    "issue_type": "price_discrepancy",
                    "severity": "blocking",
                    "description": (
                        "Price discrepancy on %s (code: %s): "
                        "EDI=%.4f, System=%.4f, Diff=%.2f%%" % (
                            product.name, parsed_line.product_code,
                            parsed_line.unit_price, system_price, diff_pct * 100,
                        )
                    ),
                    "edi_price": parsed_line.unit_price,
                    "system_price": system_price,
                    "sale_order_line_id": sol.id,
                })
                issues.append({"severity": "blocking", "type": "price_discrepancy"})

        return issues, sol

    # ── Change order flow ─────────────────────────────────────────────────

    def _process_change_order(
        self, order, partner, client_ref: str, filename: str, file_hash: str
    ):
        """Route a change order to pending review with a diff summary."""
        existing_so = self._find_existing_so(client_ref)
        if not existing_so:
            self.env["edi.log"].log(
                partner, "inbound", "error", "warning",
                "Change order received for PO '%s' but no matching SO found (client_ref: %s)" % (
                    order.po_number, client_ref),
                filename=filename,
            )
            return

        original_review = self.env["edi.order.review"].search([
            ("sale_order_id", "=", existing_so.id),
            ("document_type", "=", "new_order"),
        ], limit=1)

        change_summary = self._compute_change_summary(existing_so, order)

        review = self.env["edi.order.review"].create({
            "trading_partner_id": partner.id,
            "customer_po_number": order.po_number,
            "store_code": order.store_code,
            "sale_order_id": existing_so.id,
            "edi_file_hash": file_hash,
            "edi_filename": filename,
            "edi_raw_data": order.raw_data,
            "document_type": "change_order",
            "original_review_id": original_review.id if original_review else False,
            "change_summary": change_summary,
            "state": "pending_review",  # Always — change orders never auto-approve
        })

        # Store the pending changes as a JSON attachment for apply_change_order()
        self.env["ir.attachment"].create({
            "name": "pending_changes.json",
            "res_model": "edi.order.review",
            "res_id": review.id,
            "datas": self._encode_pending_changes(order, existing_so),
            "mimetype": "application/json",
        })

        self.env["edi.log"].log(
            partner, "inbound", "order_created", "warning",
            "Change order routed to review: SO %s — %s" % (existing_so.name, change_summary),
            filename=filename, sale_order=existing_so, review=review,
        )

        if partner.alert_on_issues:
            self._send_review_alert(partner, review)

    @api.model
    def apply_change_order(self, review):
        """
        Apply a change order's pending changes to its linked SO.
        Called by edi.order.review.action_approve() for change_order type.
        """
        if review.document_type != "change_order":
            raise UserError(_("apply_change_order called on non-change-order review"))

        so = review.sale_order_id
        if not so:
            raise UserError(_("No SO linked to this change order review"))

        # Load pending changes from attachment
        attachment = self.env["ir.attachment"].search([
            ("res_model", "=", "edi.order.review"),
            ("res_id", "=", review.id),
            ("name", "=", "pending_changes.json"),
        ], limit=1)

        if not attachment:
            _logger.warning("[EDI] No pending_changes.json found for review %s", review.name)
            return

        import base64
        changes = json.loads(base64.b64decode(attachment.datas).decode())

        # Update commitment date
        if changes.get("new_delivery_date"):
            new_date = date.fromisoformat(changes["new_delivery_date"])
            so.commitment_date = fields.Datetime.to_datetime(str(new_date))

        # Update line quantities
        for line_change in changes.get("line_changes", []):
            so_line = self.env["sale.order.line"].search([
                ("order_id", "=", so.id),
                ("edi_line_number", "=", line_change["line_number"]),
            ], limit=1)
            if so_line:
                so_line.product_uom_qty = line_change["new_qty"]

        # Post chatter message
        so.message_post(
            body=_("EDI change order approved: %s") % review.change_summary,
            subtype_xmlid="mail.mt_note",
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    def _is_file_duplicate(self, file_hash: str, partner) -> bool:
        """Check if this file hash was already processed for this partner."""
        return bool(self.env["edi.log"].search([
            ("trading_partner_id", "=", partner.id),
            ("file_hash", "=", file_hash),
            ("event_type", "=", "file_download"),
            ("status", "=", "success"),
        ], limit=1))

    def _find_existing_so(self, client_ref: str):
        """Find an existing SO by client reference."""
        return self.env["sale.order"].search([
            ("client_order_ref", "=", client_ref),
        ], limit=1)

    def _resolve_delivery_partner(self, partner, order):
        """
        Resolve the delivery partner for this order.
        For per_store mode: look up child contact by ref field = store_code.
        For single mode: use trading_partner.partner_id directly.
        """
        if partner.order_split_mode == "per_store" and order.store_code:
            store_partner = self.env["res.partner"].search([
                ("parent_id", "=", partner.partner_id.id),
                ("ref", "=", order.store_code),
            ], limit=1)
            if store_partner:
                return store_partner
            # Fallback: unknown store — use parent, log warning
            _logger.warning(
                "[EDI] Store code '%s' not found as child partner of %s",
                order.store_code, partner.partner_id.name,
            )
        return partner.partner_id

    def _find_product(self, product_code: str, partner):
        """Look up a product using the trading partner's configured match field."""
        field = partner.product_match_field
        if field == "barcode":
            return self.env["product.product"].search(
                [("barcode", "=", product_code)], limit=1
            ) or None
        elif field == "default_code":
            return self.env["product.product"].search(
                [("default_code", "=", product_code)], limit=1
            ) or None
        elif field == "supplier_sku":
            info = self.env["product.supplierinfo"].search([
                ("product_code", "=", product_code),
            ], limit=1)
            return info.product_id if info else None
        return None

    def _get_pricelist_price(self, product, quantity: float, partner) -> float | None:
        """Get the pricelist price for a product. Returns None if no pricelist configured."""
        if not partner.pricelist_id:
            return None
        try:
            price = partner.pricelist_id._get_product_price(
                product, quantity, partner.partner_id
            )
            return price
        except Exception as exc:
            _logger.warning("[EDI] Pricelist price lookup failed for %s: %s", product.name, exc)
            return None

    def _compute_change_summary(self, existing_so, order) -> str:
        """Generate a human-readable summary of what changed."""
        parts = []
        if order.requested_delivery_date:
            current_date = existing_so.commitment_date and existing_so.commitment_date.date()
            if current_date != order.requested_delivery_date:
                parts.append("Delivery date: %s → %s" % (current_date, order.requested_delivery_date))

        existing_qtys = {
            line.edi_line_number: line.product_uom_qty
            for line in existing_so.order_line
        }
        for parsed_line in order.lines:
            existing_qty = existing_qtys.get(parsed_line.line_number)
            if existing_qty is None:
                parts.append("New line %d: %s ×%.0f" % (
                    parsed_line.line_number, parsed_line.description, parsed_line.quantity))
            elif existing_qty != parsed_line.quantity:
                parts.append("Line %d qty: %.0f → %.0f" % (
                    parsed_line.line_number, existing_qty, parsed_line.quantity))

        removed_lines = set(existing_qtys.keys()) - {l.line_number for l in order.lines}
        for line_num in removed_lines:
            parts.append("Line %d removed" % line_num)

        return "; ".join(parts) if parts else "No changes detected"

    def _encode_pending_changes(self, order, existing_so) -> str:
        """Encode pending change order data as base64 JSON for ir.attachment."""
        import base64
        changes = {
            "new_delivery_date": order.requested_delivery_date.isoformat()
                if order.requested_delivery_date else None,
            "line_changes": [
                {"line_number": l.line_number, "new_qty": l.quantity}
                for l in order.lines
            ],
        }
        return base64.b64encode(json.dumps(changes).encode()).decode()

    def _send_review_alert(self, partner, review):
        """Send email alert to configured recipients."""
        if not partner.alert_email_ids:
            return
        try:
            template = self.env.ref("mml_edi.mail_template_edi_review_alert", raise_if_not_found=False)
            if template:
                template.send_mail(review.id, force_send=True)
        except Exception as exc:
            _logger.warning("[EDI] Failed to send review alert: %s", exc)
```

**Step 4: Update `models/__init__.py`** — append:
```python
from . import edi_processor
```

**Step 5: Run processor tests**
```bash
odoo-bin -d ODOOTEST --test-enable --stop-after-init -i mml_edi --test-tags mml_edi
# Expected: processor tests pass (some may need DB setup first)
```

**Step 6: Commit**
```bash
git add mml.edi/models/edi_processor.py mml.edi/tests/test_processor.py mml.edi/tests/common.py mml.edi/models/__init__.py
git commit -m "feat(mml_edi): add EDI processing engine with new_order and change_order flows"
```

---

### Task 10: Briscoes parser stub (`parsers/briscoes.py`)

**Files:**
- Create: `mml.edi/parsers/briscoes.py`
- Modify: `mml.edi/parsers/__init__.py`

**Step 1: Write the stub**

```python
# mml.edi/parsers/briscoes.py
"""
Briscoes EDI Parser — Phase 1 Stub.

Returns mock ParsedOrder data that exercises all pipeline code paths:
  1. Clean new order (no issues) → auto-approved if partner allows
  2. New order with price discrepancy + unknown product → pending_review
  3. Change order (modifies order #1) → pending_review
  4. Duplicate of order #1 → dedup engine skips it

PHASE 2: Replace parse_file() and generate_ack() with real EDIFACT D96A logic
when sample files are provided by Briscoes IT.
"""

import logging
from datetime import date, timedelta

from .base_parser import BaseEDIParser, ParsedOrder, ParsedOrderLine

_logger = logging.getLogger(__name__)

_MOCK_STORE_A = "1017"
_MOCK_STORE_B = "1042"


class BriscoesParser(BaseEDIParser):
    """
    Parser for Briscoes EDIFACT D96A purchase orders.

    Phase 1: Returns mock data for end-to-end pipeline testing.
    Phase 2: Implement real EDIFACT D96A parsing.
    """

    def parse_file(
        self, raw_content: bytes, trading_partner
    ) -> list[ParsedOrder]:
        """
        # PHASE 2: Replace this stub with real EDIFACT D96A parsing.
        #
        # The real implementation should:
        # 1. Decode raw_content as EDIFACT (ISO 9735)
        # 2. Extract UNH/UNT segment groups
        # 3. Identify message type: ORDERS (new) or ORDCHG (change)
        # 4. Extract BGM, DTM, NAD, LIN, QTY, PRI segments
        # 5. Map store code from NAD+DP GLN to res.partner.ref
        # 6. Return one ParsedOrder per store group
        #
        # Reference: EDIFACT D96A ORDERS standard
        # Sample files: provided by Briscoes IT in Phase 2

        For now, return 4 mock ParsedOrder objects that exercise all code paths.
        """
        _logger.info("[BriscoesParser] Phase 1 stub: returning mock parsed orders")

        today = date.today()
        delivery_date = today + timedelta(days=7)
        changed_delivery_date = today + timedelta(days=14)

        # Scenario 1: Clean new order for store 1017
        # Expected outcome: auto-approved if partner.auto_confirm_clean=True
        clean_order = ParsedOrder(
            po_number="4500999001",
            store_code=_MOCK_STORE_A,
            order_date=today,
            requested_delivery_date=delivery_date,
            lines=[
                ParsedOrderLine(
                    product_code="9300601234567",   # Valid EAN-13
                    description="Volere Sparkling Water 12pk",
                    quantity=24.0,
                    unit_price=18.99,
                    line_number=1,
                ),
                ParsedOrderLine(
                    product_code="9300601234568",   # Valid EAN-13
                    description="Volere Still Water 12pk",
                    quantity=12.0,
                    unit_price=18.99,
                    line_number=2,
                ),
                ParsedOrderLine(
                    product_code="9300601234569",   # Valid EAN-13
                    description="Enkel Sparkling 6pk",
                    quantity=6.0,
                    unit_price=11.99,
                    line_number=3,
                ),
            ],
            document_type="new_order",
            raw_data=raw_content.decode("utf-8", errors="replace"),
        )

        # Scenario 2: New order with issues for store 1042
        # Line 1: price discrepancy (EDI price != pricelist price)
        # Line 2: product code not in Odoo
        # Expected outcome: pending_review, 2 blocking issues
        problem_order = ParsedOrder(
            po_number="4500999002",
            store_code=_MOCK_STORE_B,
            order_date=today,
            requested_delivery_date=delivery_date,
            lines=[
                ParsedOrderLine(
                    product_code="9300601234567",   # Known product, wrong price
                    description="Volere Sparkling Water 12pk",
                    quantity=24.0,
                    unit_price=999.99,               # Deliberately wrong — triggers blocking issue
                    line_number=1,
                ),
                ParsedOrderLine(
                    product_code="UNKNOWN_SKU_00000",  # Not in Odoo
                    description="Mystery Product",
                    quantity=10.0,
                    unit_price=9.99,
                    line_number=2,
                ),
            ],
            document_type="new_order",
            raw_data=raw_content.decode("utf-8", errors="replace"),
        )

        # Scenario 3: Change order for PO 4500999001 (clean order from scenario 1)
        # Changes: qty on line 1 increased, delivery date changed
        # Expected outcome: pending_review, change_summary computed
        change_order = ParsedOrder(
            po_number="4500999001",         # Same as clean_order — triggers change_order path
            store_code=_MOCK_STORE_A,
            order_date=today,
            requested_delivery_date=changed_delivery_date,
            lines=[
                ParsedOrderLine(
                    product_code="9300601234567",
                    description="Volere Sparkling Water 12pk",
                    quantity=36.0,              # Was 24 — qty increased
                    unit_price=18.99,
                    line_number=1,
                ),
                ParsedOrderLine(
                    product_code="9300601234568",
                    description="Volere Still Water 12pk",
                    quantity=12.0,              # Unchanged
                    unit_price=18.99,
                    line_number=2,
                ),
                ParsedOrderLine(
                    product_code="9300601234569",
                    description="Enkel Sparkling 6pk",
                    quantity=6.0,               # Unchanged
                    unit_price=11.99,
                    line_number=3,
                ),
            ],
            document_type="change_order",
            change_reason="Customer increased order quantity",
            raw_data=raw_content.decode("utf-8", errors="replace"),
        )

        # Scenario 4: Duplicate of scenario 1 (same PO number, same store)
        # Expected outcome: skipped by dedup — no new SO or review created
        duplicate_order = ParsedOrder(
            po_number="4500999001",         # Same as clean_order — triggers dedup
            store_code=_MOCK_STORE_A,
            order_date=today,
            requested_delivery_date=delivery_date,
            lines=[
                ParsedOrderLine(
                    product_code="9300601234567",
                    description="Volere Sparkling Water 12pk",
                    quantity=24.0,
                    unit_price=18.99,
                    line_number=1,
                ),
            ],
            document_type="new_order",
            raw_data=raw_content.decode("utf-8", errors="replace"),
        )

        return [clean_order, problem_order, change_order, duplicate_order]

    def generate_ack(self, review_record) -> bytes:
        """
        # PHASE 2: Replace with real EDIFACT ORDRSP/APERAK ACK generation.
        #
        # The real implementation should:
        # 1. Generate EDIFACT ORDRSP (order response) or APERAK (application error)
        # 2. Include all accepted/rejected line details
        # 3. Follow Briscoes-specific segment requirements from their tech spec
        #
        # Sample ACK format: provided by Briscoes IT in Phase 2

        For now, return a placeholder that allows the pipeline to complete.
        """
        _logger.info(
            "[BriscoesParser] Phase 1 stub: generating placeholder ACK for %s",
            review_record.customer_po_number,
        )
        return (
            f"ACK|{review_record.customer_po_number}"
            f"|{review_record.state}"
            f"|PHASE2_PLACEHOLDER"
        ).encode("utf-8")
```

**Step 2: Update `parsers/__init__.py`**:
```python
from .base_parser import BaseEDIParser, ParsedOrder, ParsedOrderLine, EDIParseError, EDIFTPError
from .briscoes import BriscoesParser
```

**Step 3: Commit**
```bash
git add mml.edi/parsers/briscoes.py mml.edi/parsers/__init__.py
git commit -m "feat(mml_edi): add Briscoes parser stub with mock data for all pipeline scenarios"
```

---

## Track 3 — UI/Config Layer (Parallel after Track 1 models complete)

### Task 11: Security groups and access rules

**Files:**
- Create: `mml.edi/security/edi_security.xml`
- Create: `mml.edi/security/ir.model.access.csv`

**Step 1: Write `edi_security.xml`**

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <!-- Security groups -->
    <record id="group_edi_user" model="res.groups">
        <field name="name">EDI User</field>
        <field name="category_id" ref="base.module_category_hidden"/>
        <field name="implied_ids" eval="[(4, ref('base.group_user'))]"/>
    </record>

    <record id="group_edi_manager" model="res.groups">
        <field name="name">EDI Manager</field>
        <field name="category_id" ref="base.module_category_hidden"/>
        <field name="implied_ids" eval="[(4, ref('mml_edi.group_edi_user'))]"/>
    </record>
</odoo>
```

**Step 2: Write `ir.model.access.csv`**

```csv
id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
access_edi_trading_partner_user,edi.trading.partner user,model_edi_trading_partner,group_edi_user,1,0,0,0
access_edi_trading_partner_manager,edi.trading.partner manager,model_edi_trading_partner,group_edi_manager,1,1,1,1
access_edi_log_user,edi.log user,model_edi_log,group_edi_user,1,0,0,0
access_edi_log_manager,edi.log manager,model_edi_log,group_edi_manager,1,1,1,0
access_edi_order_review_user,edi.order.review user,model_edi_order_review,group_edi_user,1,1,0,0
access_edi_order_review_manager,edi.order.review manager,model_edi_order_review,group_edi_manager,1,1,1,1
access_edi_order_issue_user,edi.order.issue user,model_edi_order_issue,group_edi_user,1,1,0,0
access_edi_order_issue_manager,edi.order.issue manager,model_edi_order_issue,group_edi_manager,1,1,1,1
access_edi_processor_user,edi.processor user,model_edi_processor,group_edi_user,1,0,0,0
access_edi_processor_manager,edi.processor manager,model_edi_processor,group_edi_manager,1,1,1,1
```

**Step 3: Commit**
```bash
git add mml.edi/security/
git commit -m "feat(mml_edi): add security groups (edi_user, edi_manager) and model access rules"
```

---

### Task 12: Trading partner views

**Files:**
- Create: `mml.edi/views/edi_trading_partner_views.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <!-- Trading Partner List -->
    <record id="view_edi_trading_partner_tree" model="ir.ui.view">
        <field name="name">edi.trading.partner.tree</field>
        <field name="model">edi.trading.partner</field>
        <field name="arch" type="xml">
            <tree>
                <field name="name"/>
                <field name="code"/>
                <field name="edi_format"/>
                <field name="environment" widget="badge"
                       decoration-warning="environment=='test'"
                       decoration-success="environment=='production'"/>
                <field name="partner_id"/>
                <field name="active" widget="toggle_button"/>
            </tree>
        </field>
    </record>

    <!-- Trading Partner Form -->
    <record id="view_edi_trading_partner_form" model="ir.ui.view">
        <field name="name">edi.trading.partner.form</field>
        <field name="model">edi.trading.partner</field>
        <field name="arch" type="xml">
            <form>
                <header>
                    <button name="action_test_ftp_connection" type="object"
                            string="Test FTP Connection" class="btn-secondary"/>
                    <button name="action_run_poll_now" type="object"
                            string="Poll Now" class="btn-primary"
                            groups="mml_edi.group_edi_manager"/>
                </header>
                <sheet>
                    <div class="oe_title">
                        <h1><field name="name"/></h1>
                    </div>
                    <group>
                        <group string="Identity">
                            <field name="code"/>
                            <field name="partner_id"/>
                            <field name="edi_format"/>
                            <field name="parser_class"/>
                            <field name="active"/>
                        </group>
                        <group string="Environment">
                            <field name="environment" widget="radio"/>
                        </group>
                    </group>
                    <notebook>
                        <page string="FTP Configuration">
                            <group>
                                <group string="Connection">
                                    <field name="ftp_protocol"/>
                                    <field name="ftp_host"/>
                                    <field name="ftp_port"/>
                                    <field name="ftp_user"/>
                                    <field name="ftp_password" password="True"/>
                                </group>
                                <group string="Production Paths">
                                    <field name="ftp_inbox_path"/>
                                    <field name="ftp_outbox_path"/>
                                </group>
                                <group string="Test Paths">
                                    <field name="ftp_test_inbox_path"/>
                                    <field name="ftp_test_outbox_path"/>
                                </group>
                            </group>
                        </page>
                        <page string="Processing Rules">
                            <group>
                                <group>
                                    <field name="pricelist_id"/>
                                    <field name="price_tolerance_pct" string="Price Tolerance (%)" widget="percentage"/>
                                    <field name="auto_confirm_clean"/>
                                    <field name="poll_interval_minutes"/>
                                </group>
                                <group>
                                    <field name="order_split_mode"/>
                                    <field name="product_match_field"/>
                                    <field name="client_ref_template"/>
                                </group>
                            </group>
                        </page>
                        <page string="Notifications">
                            <group>
                                <field name="alert_on_issues"/>
                                <field name="alert_email_ids" widget="many2many_tags"/>
                            </group>
                        </page>
                    </notebook>
                </sheet>
            </form>
        </field>
    </record>

    <!-- Action -->
    <record id="action_edi_trading_partner" model="ir.actions.act_window">
        <field name="name">Trading Partners</field>
        <field name="res_model">edi.trading.partner</field>
        <field name="view_mode">tree,form</field>
    </record>
</odoo>
```

**Commit:**
```bash
git add mml.edi/views/edi_trading_partner_views.xml
git commit -m "feat(mml_edi): add trading partner list and form views"
```

---

### Task 13: Review dashboard views

**Files:**
- Create: `mml.edi/views/edi_order_review_views.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <!-- Kanban -->
    <record id="view_edi_order_review_kanban" model="ir.ui.view">
        <field name="name">edi.order.review.kanban</field>
        <field name="model">edi.order.review</field>
        <field name="arch" type="xml">
            <kanban default_group_by="state" class="o_kanban_small_column">
                <field name="state"/>
                <field name="trading_partner_id"/>
                <field name="customer_po_number"/>
                <field name="document_type"/>
                <field name="blocking_issue_count"/>
                <field name="issue_count"/>
                <field name="received_date"/>
                <field name="sale_order_id"/>
                <templates>
                    <t t-name="kanban-box">
                        <div class="oe_kanban_card oe_kanban_global_click">
                            <div class="o_kanban_record_top">
                                <div class="o_kanban_record_headings">
                                    <strong class="o_kanban_record_title">
                                        <field name="customer_po_number"/>
                                    </strong>
                                    <span class="badge ms-1"
                                          t-att-class="record.document_type.raw_value == 'change_order' ? 'bg-warning' : 'bg-primary'">
                                        <t t-esc="record.document_type.value"/>
                                    </span>
                                </div>
                            </div>
                            <div class="o_kanban_record_body">
                                <div><field name="trading_partner_id"/></div>
                                <div t-if="record.blocking_issue_count.raw_value > 0" class="text-danger">
                                    <i class="fa fa-exclamation-circle"/> <field name="blocking_issue_count"/> blocking
                                </div>
                                <div t-if="record.issue_count.raw_value > 0 and record.blocking_issue_count.raw_value == 0" class="text-warning">
                                    <i class="fa fa-warning"/> <field name="issue_count"/> warning(s)
                                </div>
                                <div class="text-muted small"><field name="received_date"/></div>
                            </div>
                        </div>
                    </t>
                </templates>
            </kanban>
        </field>
    </record>

    <!-- List -->
    <record id="view_edi_order_review_tree" model="ir.ui.view">
        <field name="name">edi.order.review.tree</field>
        <field name="model">edi.order.review</field>
        <field name="arch" type="xml">
            <tree decoration-danger="blocking_issue_count > 0 and state == 'pending_review'"
                  decoration-warning="issue_count > 0 and state == 'pending_review' and blocking_issue_count == 0"
                  decoration-muted="state in ('approved', 'auto_approved', 'rejected')">
                <field name="name"/>
                <field name="received_date"/>
                <field name="trading_partner_id"/>
                <field name="document_type" widget="badge"
                       decoration-warning="document_type == 'change_order'"/>
                <field name="customer_po_number"/>
                <field name="store_code" optional="hide"/>
                <field name="sale_order_id"/>
                <field name="blocking_issue_count" string="Blocking"/>
                <field name="issue_count" string="Warnings"/>
                <field name="state" widget="badge"
                       decoration-warning="state == 'pending_review'"
                       decoration-success="state in ('approved', 'auto_approved')"
                       decoration-danger="state == 'rejected'"/>
                <button name="action_approve" type="object" string="Approve"
                        attrs="{'invisible': [('state', '!=', 'pending_review')]}"
                        class="btn-sm btn-success"/>
                <button name="action_reject" type="object" string="Reject"
                        attrs="{'invisible': [('state', '!=', 'pending_review')]}"
                        class="btn-sm btn-danger"/>
            </tree>
        </field>
    </record>

    <!-- Form -->
    <record id="view_edi_order_review_form" model="ir.ui.view">
        <field name="name">edi.order.review.form</field>
        <field name="model">edi.order.review</field>
        <field name="arch" type="xml">
            <form>
                <header>
                    <button name="action_approve" type="object" string="Approve"
                            class="btn-success"
                            attrs="{'invisible': [('state', '!=', 'pending_review')]}"/>
                    <button name="action_approve_corrected" type="object"
                            string="Approve with Corrections"
                            attrs="{'invisible': [('state', '!=', 'pending_review')]}"/>
                    <button name="action_reject" type="object" string="Reject"
                            class="btn-danger"
                            attrs="{'invisible': [('state', '!=', 'pending_review')]}"/>
                    <button name="action_reset_to_review" type="object"
                            string="Reset to Review"
                            attrs="{'invisible': [('state', 'in', ('pending_review',))]}"
                            groups="mml_edi.group_edi_manager"/>
                    <field name="state" widget="statusbar"
                           statusbar_visible="pending_review,approved,rejected"/>
                </header>
                <sheet>
                    <div class="oe_title">
                        <h1><field name="name"/></h1>
                        <span class="badge fs-6"
                              t-att-class="document_type == 'change_order' ? 'bg-warning' : 'bg-primary'">
                            <field name="document_type"/>
                        </span>
                    </div>
                    <group>
                        <group>
                            <field name="trading_partner_id"/>
                            <field name="customer_po_number"/>
                            <field name="store_code"/>
                            <field name="sale_order_id"/>
                        </group>
                        <group>
                            <field name="received_date"/>
                            <field name="reviewed_by"/>
                            <field name="reviewed_date"/>
                            <field name="blocking_issue_count"/>
                        </group>
                    </group>
                    <notebook>
                        <page string="Issues" attrs="{'invisible': [('issue_count', '=', 0)]}">
                            <field name="issue_ids">
                                <tree editable="bottom"
                                      decoration-danger="severity == 'blocking'"
                                      decoration-warning="severity == 'warning'">
                                    <field name="issue_type"/>
                                    <field name="severity" widget="badge"/>
                                    <field name="description"/>
                                    <field name="edi_price" optional="hide"/>
                                    <field name="system_price" optional="hide"/>
                                    <field name="price_difference_pct" optional="hide" string="Diff %"/>
                                    <field name="sale_order_line_id" optional="show"/>
                                    <field name="resolution" widget="badge"
                                           decoration-warning="resolution == 'pending'"
                                           decoration-success="resolution in ('accepted', 'corrected')"
                                           decoration-danger="resolution == 'rejected'"/>
                                    <button name="action_accept" type="object" string="Accept"
                                            attrs="{'invisible': [('resolution', '!=', 'pending')]}"
                                            class="btn-sm btn-outline-success"/>
                                    <button name="action_reject_issue" type="object" string="Reject"
                                            attrs="{'invisible': [('resolution', '!=', 'pending')]}"
                                            class="btn-sm btn-outline-danger"/>
                                </tree>
                            </field>
                        </page>
                        <page string="Change Summary"
                              attrs="{'invisible': [('document_type', '!=', 'change_order')]}">
                            <group>
                                <field name="original_review_id"/>
                                <field name="change_summary"/>
                            </group>
                            <field name="change_order_ids" readonly="1">
                                <tree>
                                    <field name="name"/>
                                    <field name="received_date"/>
                                    <field name="state"/>
                                </tree>
                            </field>
                        </page>
                        <page string="SO Lines">
                            <field name="sale_order_id" invisible="1"/>
                            <!-- Read-only SO line view via related sale_order_id -->
                            <div class="text-muted" attrs="{'invisible': [('sale_order_id', '!=', False)]}">
                                No SO linked.
                            </div>
                        </page>
                        <page string="Notes">
                            <field name="notes" placeholder="Add notes..."/>
                        </page>
                        <page string="Raw EDI Data">
                            <field name="edi_raw_data" widget="code" readonly="1"/>
                            <group>
                                <field name="edi_filename"/>
                                <field name="edi_file_hash"/>
                            </group>
                        </page>
                    </notebook>
                </sheet>
                <div class="oe_chatter">
                    <field name="message_follower_ids"/>
                    <field name="activity_ids"/>
                    <field name="message_ids"/>
                </div>
            </form>
        </field>
    </record>

    <!-- Search -->
    <record id="view_edi_order_review_search" model="ir.ui.view">
        <field name="name">edi.order.review.search</field>
        <field name="model">edi.order.review</field>
        <field name="arch" type="xml">
            <search>
                <field name="customer_po_number"/>
                <field name="trading_partner_id"/>
                <field name="store_code"/>
                <filter name="needs_review" string="Needs Review"
                        domain="[('state', '=', 'pending_review')]" />
                <filter name="price_issues" string="Price Issues"
                        domain="[('issue_ids.issue_type', '=', 'price_discrepancy')]"/>
                <filter name="product_issues" string="Product Issues"
                        domain="[('issue_ids.issue_type', '=', 'product_not_found')]"/>
                <filter name="change_orders" string="Change Orders"
                        domain="[('document_type', '=', 'change_order')]"/>
                <filter name="auto_approved" string="Auto-Approved"
                        domain="[('state', '=', 'auto_approved')]"/>
                <filter name="today" string="Today"
                        domain="[('received_date', '>=', datetime.datetime.combine(datetime.date.today(), datetime.time(0,0,0)))]"/>
                <separator/>
                <group expand="0" string="Group By">
                    <filter name="group_by_partner" string="Trading Partner"
                            context="{'group_by': 'trading_partner_id'}"/>
                    <filter name="group_by_state" string="State"
                            context="{'group_by': 'state'}"/>
                </group>
            </search>
        </field>
    </record>

    <!-- Actions -->
    <record id="action_edi_order_review_dashboard" model="ir.actions.act_window">
        <field name="name">EDI Dashboard</field>
        <field name="res_model">edi.order.review</field>
        <field name="view_mode">kanban,tree,form</field>
    </record>

    <record id="action_edi_order_review_pending" model="ir.actions.act_window">
        <field name="name">Pending Review</field>
        <field name="res_model">edi.order.review</field>
        <field name="view_mode">tree,form</field>
        <field name="domain">[('state', '=', 'pending_review')]</field>
        <field name="context">{'search_default_needs_review': 1}</field>
    </record>

    <record id="action_edi_order_review_all" model="ir.actions.act_window">
        <field name="name">All Reviews</field>
        <field name="res_model">edi.order.review</field>
        <field name="view_mode">tree,form</field>
    </record>
</odoo>
```

**Commit:**
```bash
git add mml.edi/views/edi_order_review_views.xml
git commit -m "feat(mml_edi): add review dashboard kanban, list, form and search views"
```

---

### Task 14: Log, issue, sale order views and menus

**Files:**
- Create: `mml.edi/views/edi_log_views.xml`
- Create: `mml.edi/views/edi_order_issue_views.xml`
- Create: `mml.edi/views/sale_order_views.xml`
- Create: `mml.edi/views/menuitems.xml`

**`edi_log_views.xml`:**
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="view_edi_log_tree" model="ir.ui.view">
        <field name="name">edi.log.tree</field>
        <field name="model">edi.log</field>
        <field name="arch" type="xml">
            <tree decoration-danger="status == 'error'" decoration-warning="status == 'warning'"
                  default_order="timestamp desc">
                <field name="timestamp"/>
                <field name="trading_partner_id"/>
                <field name="direction" widget="badge"/>
                <field name="event_type"/>
                <field name="filename" optional="hide"/>
                <field name="sale_order_id" optional="show"/>
                <field name="status" widget="badge"
                       decoration-success="status=='success'"
                       decoration-warning="status=='warning'"
                       decoration-danger="status=='error'"/>
                <field name="message"/>
            </tree>
        </field>
    </record>

    <record id="view_edi_log_search" model="ir.ui.view">
        <field name="name">edi.log.search</field>
        <field name="model">edi.log</field>
        <field name="arch" type="xml">
            <search>
                <field name="trading_partner_id"/>
                <field name="filename"/>
                <filter name="errors" string="Errors" domain="[('status', '=', 'error')]"/>
                <filter name="today" string="Today"
                        domain="[('timestamp', '>=', datetime.datetime.combine(datetime.date.today(), datetime.time(0,0,0)))]"/>
                <group string="Group By">
                    <filter name="by_partner" string="Trading Partner"
                            context="{'group_by': 'trading_partner_id'}"/>
                    <filter name="by_event" string="Event Type"
                            context="{'group_by': 'event_type'}"/>
                </group>
            </search>
        </field>
    </record>

    <record id="action_edi_log" model="ir.actions.act_window">
        <field name="name">EDI Logs</field>
        <field name="res_model">edi.log</field>
        <field name="view_mode">tree</field>
    </record>
</odoo>
```

**`sale_order_views.xml`:**
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="view_sale_order_edi_fields" model="ir.ui.view">
        <field name="name">sale.order.edi.fields</field>
        <field name="model">sale.order</field>
        <field name="inherit_id" ref="sale.view_order_form"/>
        <field name="arch" type="xml">
            <xpath expr="//field[@name='client_order_ref']" position="after">
                <field name="edi_trading_partner_id" readonly="1"
                       attrs="{'invisible': [('is_edi_order', '=', False)]}"/>
                <field name="edi_review_id" readonly="1"
                       attrs="{'invisible': [('is_edi_order', '=', False)]}"/>
                <field name="is_edi_order" invisible="1"/>
            </xpath>
        </field>
    </record>

    <record id="action_sale_order_edi" model="ir.actions.act_window">
        <field name="name">Sales Orders (EDI)</field>
        <field name="res_model">sale.order</field>
        <field name="view_mode">tree,form</field>
        <field name="domain">[('is_edi_order', '=', True)]</field>
    </record>
</odoo>
```

**`menuitems.xml`:**
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <!-- Top-level menu -->
    <menuitem id="menu_edi_root" name="EDI" sequence="80"
              groups="mml_edi.group_edi_user"/>

    <!-- Dashboard -->
    <menuitem id="menu_edi_dashboard" name="Dashboard"
              parent="menu_edi_root"
              action="action_edi_order_review_dashboard"
              sequence="10"/>

    <!-- Orders submenu -->
    <menuitem id="menu_edi_orders" name="Orders"
              parent="menu_edi_root" sequence="20"/>

    <menuitem id="menu_edi_orders_pending" name="Pending Review"
              parent="menu_edi_orders"
              action="action_edi_order_review_pending"
              sequence="10"/>

    <menuitem id="menu_edi_orders_all" name="All Reviews"
              parent="menu_edi_orders"
              action="action_edi_order_review_all"
              sequence="20"/>

    <menuitem id="menu_edi_orders_so" name="Sales Orders (EDI)"
              parent="menu_edi_orders"
              action="action_sale_order_edi"
              sequence="30"/>

    <!-- Logs -->
    <menuitem id="menu_edi_logs" name="Logs"
              parent="menu_edi_root"
              action="action_edi_log"
              sequence="30"
              groups="mml_edi.group_edi_user"/>

    <!-- Configuration -->
    <menuitem id="menu_edi_config" name="Configuration"
              parent="menu_edi_root" sequence="100"
              groups="mml_edi.group_edi_manager"/>

    <menuitem id="menu_edi_trading_partners" name="Trading Partners"
              parent="menu_edi_config"
              action="action_edi_trading_partner"
              sequence="10"/>
</odoo>
```

**Commit:**
```bash
git add mml.edi/views/
git commit -m "feat(mml_edi): add log, issue, SO views and EDI menu structure"
```

---

### Task 15: Data files (sequences, cron, Briscoes record, mail template)

**Files:**
- Create: `mml.edi/data/ir_sequence.xml`
- Create: `mml.edi/data/ir_cron.xml`
- Create: `mml.edi/data/edi_trading_partner_briscoes.xml`
- Create: `mml.edi/data/mail_template.xml`

**`ir_sequence.xml`:**
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="seq_edi_order_review" model="ir.sequence">
        <field name="name">EDI Order Review</field>
        <field name="code">edi.order.review</field>
        <field name="prefix">EDI/%(trading_partner_id.code)s/%(year)s/</field>
        <field name="padding">4</field>
    </record>

    <record id="seq_edi_log" model="ir.sequence">
        <field name="name">EDI Log</field>
        <field name="code">edi.log</field>
        <field name="prefix">EDILOG/%(year)s/</field>
        <field name="padding">6</field>
    </record>
</odoo>
```

**`ir_cron.xml`:**
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="cron_edi_poll" model="ir.cron">
        <field name="name">EDI: Poll Trading Partners</field>
        <field name="model_id" ref="model_edi_processor"/>
        <field name="state">code</field>
        <field name="code">model.run_scheduled_poll()</field>
        <field name="interval_number">15</field>
        <field name="interval_type">minutes</field>
        <field name="numbercall">-1</field>
        <field name="active">True</field>
        <field name="user_id" ref="base.user_admin"/>
    </record>
</odoo>
```

**`edi_trading_partner_briscoes.xml`:**
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo noupdate="1">
    <!--
        Default Briscoes Group trading partner.
        noupdate="1" — this record is not overwritten on module upgrade.
        Set ftp_user, ftp_password, alert_email_ids manually after install.
    -->
    <record id="edi_trading_partner_briscoes" model="edi.trading.partner">
        <field name="name">Briscoes Group</field>
        <field name="code">BRISCOES</field>
        <field name="partner_id" ref="base.res_partner_3324" /><!-- Set to actual Briscoes partner -->
        <field name="edi_format">edifact_d96a</field>
        <field name="parser_class">mml_edi.parsers.briscoes.BriscoesParser</field>
        <field name="ftp_protocol">ftp</field>
        <field name="ftp_host">post.edis.co.nz</field>
        <field name="ftp_port">21</field>
        <field name="ftp_inbox_path">/FromEDIS</field>
        <field name="ftp_outbox_path">/ToEDIS</field>
        <field name="ftp_test_inbox_path">/Test/FromEDIS</field>
        <field name="ftp_test_outbox_path">/Test/ToEDIS</field>
        <field name="environment">production</field>
        <field name="price_tolerance_pct">0.0</field>
        <field name="auto_confirm_clean">True</field>
        <field name="poll_interval_minutes">15</field>
        <field name="order_split_mode">per_store</field>
        <field name="product_match_field">barcode</field>
        <field name="client_ref_template">{po_number}_{store_code}</field>
        <field name="alert_on_issues">True</field>
        <!-- pricelist_id: set to "Briscoes Products" pricelist after install -->
        <!-- ftp_user, ftp_password: set manually -->
        <!-- alert_email_ids: set manually -->
    </record>
</odoo>
```

**`mail_template.xml`:**
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="mail_template_edi_review_alert" model="mail.template">
        <field name="name">EDI: Review Required Alert</field>
        <field name="model_id" ref="model_edi_order_review"/>
        <field name="subject">EDI Order Needs Review: {{ object.customer_po_number }}</field>
        <field name="body_html"><![CDATA[
            <p>An EDI order has been received that requires manual review.</p>
            <table>
                <tr><td><strong>Reference:</strong></td><td>{{ object.name }}</td></tr>
                <tr><td><strong>Trading Partner:</strong></td><td>{{ object.trading_partner_id.name }}</td></tr>
                <tr><td><strong>PO Number:</strong></td><td>{{ object.customer_po_number }}</td></tr>
                <tr><td><strong>Type:</strong></td><td>{{ object.document_type }}</td></tr>
                <tr><td><strong>Blocking Issues:</strong></td><td>{{ object.blocking_issue_count }}</td></tr>
                <tr><td><strong>Received:</strong></td><td>{{ object.received_date }}</td></tr>
            </table>
            <p>
                <a href="/odoo/edi/reviews/{{ object.id }}">View in Odoo</a>
            </p>
        ]]></field>
        <field name="email_to">{{ ','.join(object.trading_partner_id.alert_email_ids.mapped('email')) }}</field>
        <field name="auto_delete">True</field>
    </record>
</odoo>
```

**Commit:**
```bash
git add mml.edi/data/
git commit -m "feat(mml_edi): add sequences, cron, Briscoes default record, and mail template"
```

---

### Task 16: Bulk action wizard

**Files:**
- Create: `mml.edi/wizards/edi_bulk_action.py`

```python
# mml.edi/wizards/edi_bulk_action.py
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EDIBulkAction(models.TransientModel):
    _name = "edi.bulk.action"
    _description = "EDI Bulk Approve/Reject"

    action = fields.Selection(
        [("approve", "Approve All Selected"), ("reject", "Reject All Selected")],
        required=True,
        default="approve",
        string="Action",
    )
    review_ids = fields.Many2many(
        "edi.order.review",
        string="Reviews",
        domain=[("state", "=", "pending_review")],
    )
    notes = fields.Text(string="Notes (applied to all)")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        active_ids = self.env.context.get("active_ids", [])
        if active_ids:
            reviews = self.env["edi.order.review"].browse(active_ids).filtered(
                lambda r: r.state == "pending_review"
            )
            res["review_ids"] = [(6, 0, reviews.ids)]
        return res

    def action_execute(self):
        self.ensure_one()
        if not self.review_ids:
            raise UserError(_("No pending review records selected."))

        processed = 0
        errors = []

        for review in self.review_ids:
            if review.state != "pending_review":
                continue
            try:
                if self.notes:
                    review.notes = (review.notes or "") + "\n" + self.notes
                if self.action == "approve":
                    review.action_approve()
                else:
                    review.action_reject()
                processed += 1
            except Exception as exc:
                errors.append("%s: %s" % (review.name, str(exc)))

        if errors:
            raise UserError(
                _("Bulk action completed with %d success(es) and %d error(s):\n%s")
                % (processed, len(errors), "\n".join(errors))
            )

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Bulk Action Complete"),
                "message": _("%d review(s) %s.") % (processed, self.action + "d"),
                "type": "success",
            },
        }
```

Add wizard view XML to `views/edi_order_review_views.xml` (append):
```xml
    <!-- Bulk action wizard view -->
    <record id="view_edi_bulk_action_form" model="ir.ui.view">
        <field name="name">edi.bulk.action.form</field>
        <field name="model">edi.bulk.action</field>
        <field name="arch" type="xml">
            <form string="Bulk EDI Action">
                <group>
                    <field name="action" widget="radio"/>
                    <field name="review_ids" widget="many2many_tags" readonly="1"/>
                    <field name="notes"/>
                </group>
                <footer>
                    <button name="action_execute" type="object" string="Execute"
                            class="btn-primary"/>
                    <button string="Cancel" special="cancel"/>
                </footer>
            </form>
        </field>
    </record>

    <record id="action_edi_bulk_action" model="ir.actions.act_window">
        <field name="name">Bulk Action</field>
        <field name="res_model">edi.bulk.action</field>
        <field name="view_mode">form</field>
        <field name="target">new</field>
    </record>
```

Update `wizards/__init__.py`:
```python
from . import edi_bulk_action
```

**Commit:**
```bash
git add mml.edi/wizards/ mml.edi/views/edi_order_review_views.xml
git commit -m "feat(mml_edi): add bulk approve/reject wizard"
```

---

## Track 4 — Quality Layer (After Tracks 1 + 2)

### Task 17: Remaining tests

**Files:**
- Create: `mml.edi/tests/test_deduplication.py`
- Create: `mml.edi/tests/test_price_discrepancy.py`
- Create: `mml.edi/tests/test_review_workflow.py`
- Create: `mml.edi/tests/test_po_change_workflow.py`

**`test_deduplication.py`:**
```python
# mml.edi/tests/test_deduplication.py
from odoo.tests.common import TransactionCase
from .common import make_clean_parsed_order, make_change_order_parsed_order, EDITestSetup


class TestDeduplication(TransactionCase, EDITestSetup):
    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.processor = self.env["edi.processor"]

    def test_duplicate_file_hash_skipped(self):
        """Same file hash: second call creates no review."""
        order = make_clean_parsed_order(po_number="DEDUP001")
        self.processor.process_parsed_order(order, self.trading_partner, "f.edi", "HASH001")
        self.processor.process_parsed_order(order, self.trading_partner, "f.edi", "HASH001")
        reviews = self.env["edi.order.review"].search([("customer_po_number", "=", "DEDUP001")])
        self.assertEqual(len(reviews), 1)

    def test_duplicate_so_ref_draft_skipped(self):
        """If SO exists in draft state, second order is skipped."""
        order = make_clean_parsed_order(po_number="DEDUP002")
        # First call creates draft SO (auto_confirm_clean=False)
        self.trading_partner.auto_confirm_clean = False
        self.processor.process_parsed_order(order, self.trading_partner, "f1.edi", "HASHD1")
        self.processor.process_parsed_order(order, self.trading_partner, "f2.edi", "HASHD2")
        reviews = self.env["edi.order.review"].search([("customer_po_number", "=", "DEDUP002")])
        self.assertEqual(len(reviews), 1)

    def test_change_order_same_po_not_deduped(self):
        """A change_order with the same PO number is NOT skipped as a duplicate."""
        order = make_clean_parsed_order(po_number="DEDUP003")
        self.trading_partner.auto_confirm_clean = True
        self.processor.process_parsed_order(order, self.trading_partner, "new.edi", "HASHC1")
        change = make_change_order_parsed_order(po_number="DEDUP003")
        self.processor.process_parsed_order(change, self.trading_partner, "chg.edi", "HASHC2")
        reviews = self.env["edi.order.review"].search([("customer_po_number", "=", "DEDUP003")])
        self.assertEqual(len(reviews), 2)  # One new_order + one change_order
```

**`test_price_discrepancy.py`:**
```python
# mml.edi/tests/test_price_discrepancy.py
from odoo.tests.common import TransactionCase
from .common import EDITestSetup, make_parsed_line, ParsedOrder
from datetime import date


class TestPriceDiscrepancy(TransactionCase, EDITestSetup):
    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.processor = self.env["edi.processor"]

    def _make_order_with_price(self, price, po_number="PRICETEST"):
        return ParsedOrder(
            po_number=po_number,
            order_date=date.today(),
            lines=[make_parsed_line(unit_price=price)],
            document_type="new_order",
            raw_data="MOCK",
        )

    def test_matching_price_no_issue(self):
        """Exact price match creates no blocking issue."""
        order = self._make_order_with_price(9.99, "PRICE001")
        self.processor.process_parsed_order(order, self.trading_partner, "f.edi", "HP1")
        review = self.env["edi.order.review"].search([("customer_po_number", "=", "PRICE001")])
        price_issues = review.issue_ids.filtered(lambda i: i.issue_type == "price_discrepancy")
        self.assertFalse(price_issues)

    def test_price_discrepancy_creates_blocking_issue(self):
        """EDI price far from pricelist creates blocking issue."""
        order = self._make_order_with_price(999.99, "PRICE002")
        self.processor.process_parsed_order(order, self.trading_partner, "f.edi", "HP2")
        review = self.env["edi.order.review"].search([("customer_po_number", "=", "PRICE002")])
        price_issues = review.issue_ids.filtered(lambda i: i.issue_type == "price_discrepancy")
        self.assertTrue(price_issues)
        self.assertEqual(price_issues[0].severity, "blocking")
        self.assertEqual(review.state, "pending_review")

    def test_within_tolerance_auto_approved(self):
        """Price within configured tolerance does not block auto-approval."""
        self.trading_partner.price_tolerance_pct = 10.0  # 10% tolerance
        self.trading_partner.auto_confirm_clean = True
        order = self._make_order_with_price(10.50, "PRICE003")  # 5% above 9.99
        self.processor.process_parsed_order(order, self.trading_partner, "f.edi", "HP3")
        review = self.env["edi.order.review"].search([("customer_po_number", "=", "PRICE003")])
        self.assertEqual(review.state, "auto_approved")
```

**`test_review_workflow.py`:**
```python
# mml.edi/tests/test_review_workflow.py
from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError
from .common import make_clean_parsed_order, make_price_discrepancy_parsed_order, EDITestSetup


class TestReviewWorkflow(TransactionCase, EDITestSetup):
    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()

    def test_approve_confirms_so(self):
        order = make_price_discrepancy_parsed_order("WF001")
        self.env["edi.processor"].process_parsed_order(
            order, self.trading_partner, "f.edi", "WF_H1")
        review = self.env["edi.order.review"].search([("customer_po_number", "=", "WF001")])
        review.action_approve()
        self.assertEqual(review.state, "approved")
        self.assertEqual(review.sale_order_id.state, "sale")

    def test_reject_cancels_so(self):
        order = make_price_discrepancy_parsed_order("WF002")
        self.env["edi.processor"].process_parsed_order(
            order, self.trading_partner, "f.edi", "WF_H2")
        review = self.env["edi.order.review"].search([("customer_po_number", "=", "WF002")])
        review.action_reject()
        self.assertEqual(review.state, "rejected")
        self.assertEqual(review.sale_order_id.state, "cancel")

    def test_reset_requires_manager(self):
        order = make_price_discrepancy_parsed_order("WF003")
        self.env["edi.processor"].process_parsed_order(
            order, self.trading_partner, "f.edi", "WF_H3")
        review = self.env["edi.order.review"].search([("customer_po_number", "=", "WF003")])
        review.action_reject()
        # Regular user cannot reset
        with self.assertRaises(UserError):
            review.with_user(self.env.ref("base.user_demo")).action_reset_to_review()
```

**`test_po_change_workflow.py`:**
```python
# mml.edi/tests/test_po_change_workflow.py
from odoo.tests.common import TransactionCase
from .common import (
    make_clean_parsed_order,
    make_change_order_parsed_order,
    EDITestSetup,
)


class TestPOChangeWorkflow(TransactionCase, EDITestSetup):
    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.trading_partner.auto_confirm_clean = True
        # Create original order first
        original = make_clean_parsed_order(po_number="CHANGE001", qty=10)
        self.env["edi.processor"].process_parsed_order(
            original, self.trading_partner, "orig.edi", "ORIG_HASH"
        )
        self.original_review = self.env["edi.order.review"].search([
            ("customer_po_number", "=", "CHANGE001"),
            ("document_type", "=", "new_order"),
        ])

    def test_change_order_creates_review(self):
        change = make_change_order_parsed_order(po_number="CHANGE001", qty=20)
        self.env["edi.processor"].process_parsed_order(
            change, self.trading_partner, "chg.edi", "CHG_HASH"
        )
        change_review = self.env["edi.order.review"].search([
            ("customer_po_number", "=", "CHANGE001"),
            ("document_type", "=", "change_order"),
        ])
        self.assertEqual(change_review.state, "pending_review")
        self.assertEqual(change_review.original_review_id, self.original_review)

    def test_change_order_approval_updates_qty(self):
        change = make_change_order_parsed_order(po_number="CHANGE001", qty=20)
        self.env["edi.processor"].process_parsed_order(
            change, self.trading_partner, "chg.edi", "CHG_H2"
        )
        change_review = self.env["edi.order.review"].search([
            ("customer_po_number", "=", "CHANGE001"),
            ("document_type", "=", "change_order"),
        ])
        change_review.action_approve()
        so = self.original_review.sale_order_id
        self.assertEqual(so.order_line[0].product_uom_qty, 20.0)

    def test_change_order_always_pending_even_with_auto_confirm(self):
        """auto_confirm_clean must NOT apply to change orders."""
        self.trading_partner.auto_confirm_clean = True
        change = make_change_order_parsed_order(po_number="CHANGE001", qty=5)
        self.env["edi.processor"].process_parsed_order(
            change, self.trading_partner, "chg.edi", "CHG_H3"
        )
        change_review = self.env["edi.order.review"].search([
            ("customer_po_number", "=", "CHANGE001"),
            ("document_type", "=", "change_order"),
        ])
        self.assertEqual(change_review.state, "pending_review")
```

**Update `tests/__init__.py`:**
```python
from . import test_deduplication
from . import test_price_discrepancy
from . import test_review_workflow
from . import test_po_change_workflow
from . import test_ftp_handler
from . import test_processor
```

**Commit:**
```bash
git add mml.edi/tests/
git commit -m "test(mml_edi): add dedup, price, workflow, and change order tests"
```

---

### Task 18: `__manifest__.py` and final wiring

**Files:**
- Create: `mml.edi/__manifest__.py`
- Update: `mml.edi/models/__init__.py` (ensure all imports present)
- Update: `mml.edi/__init__.py`

**`__manifest__.py`:**
```python
{
    "name": "MML EDI",
    "version": "19.0.1.0.0",
    "summary": "Customer-agnostic EDI integration for retail trading partners",
    "description": """
        EDI module for MML Consumer Products.

        Features:
        - Multi-customer trading partner configuration
        - Inbound PO processing with review dashboard
        - PO change order handling
        - Price discrepancy detection and workflow
        - FTP/SFTP polling with retry
        - Full audit trail (edi.log)
        - Briscoes Group Phase 1 (parser stub — Phase 2 adds EDIFACT D96A)
    """,
    "author": "MML Consumer Products",
    "website": "https://github.com/JonaldM/mml.edi.odoo",
    "category": "Operations/EDI",
    "license": "OPL-1",
    "depends": [
        "base",
        "mail",
        "sale",
        "stock",
        "account",
        "product",
    ],
    "data": [
        # Security first
        "security/edi_security.xml",
        "security/ir.model.access.csv",
        # Data
        "data/ir_sequence.xml",
        "data/ir_cron.xml",
        "data/mail_template.xml",
        # Views
        "views/edi_trading_partner_views.xml",
        "views/edi_log_views.xml",
        "views/edi_order_issue_views.xml",
        "views/edi_order_review_views.xml",
        "views/sale_order_views.xml",
        "views/menuitems.xml",
        # Wizards
        "wizards/edi_bulk_action_view.xml",  # See note below
        # Default data last (noupdate)
        "data/edi_trading_partner_briscoes.xml",
    ],
    "demo": [],
    "installable": True,
    "application": True,
    "auto_install": False,
}
```

> **Note:** The bulk action wizard form view XML should be in its own file `wizards/edi_bulk_action_view.xml`. Extract it from `edi_order_review_views.xml` if you placed it there.

**Final `models/__init__.py`:**
```python
from . import edi_trading_partner
from . import edi_log
from . import edi_order_issue
from . import edi_order_review
from . import sale_order
from . import edi_ftp
from . import edi_processor
```

**Step: Install and verify**
```bash
odoo-bin -d ODOOTEST --stop-after-init -i mml_edi
# Expected: Module installs without error
```

**Step: Run all tests**
```bash
odoo-bin -d ODOOTEST --test-enable --stop-after-init -i mml_edi --test-tags mml_edi
# Expected: All tests pass
```

**Commit:**
```bash
git add mml.edi/__manifest__.py mml.edi/models/__init__.py mml.edi/__init__.py
git commit -m "feat(mml_edi): add __manifest__.py and complete module wiring"
```

---

## Completion Checklist

- [ ] `mml.edi/` installs cleanly on Odoo 19 (`odoo-bin -i mml_edi`)
- [ ] All tests pass (`--test-tags mml_edi`)
- [ ] Mock Briscoes data flows end-to-end: FTP poll → 4 ParsedOrder scenarios → reviews created
- [ ] Clean order auto-approved (state=`auto_approved`, SO confirmed)
- [ ] Problem order routes to `pending_review` (2 blocking issues)
- [ ] Change order routes to `pending_review`, approving it updates the original SO qty
- [ ] Duplicate order creates no second review
- [ ] EDI menu appears in Odoo UI with Dashboard, Orders, Logs, Configuration
- [ ] Trading partner form: Test FTP Connection button works (or gives clear error)
- [ ] Briscoes default trading partner record exists post-install

---

## Post-Sprint: Configuration Steps (Manual)

After install on ODOOTEST:
1. Navigate to EDI → Configuration → Trading Partners → Briscoes Group
2. Set `pricelist_id` to "Briscoes Products"
3. Set `ftp_user` and `ftp_password` (EDIS VAN credentials)
4. Add alert email recipients
5. Set environment to `test` for initial testing
6. Click "Poll Now" to test the pipeline with mock data

---

## Phase 2 Handoff Notes

When Briscoes provides EDIFACT D96A sample files:
- Only `parsers/briscoes.py` needs updating (`parse_file` + `generate_ack`)
- Add `tests/test_briscoes_parser.py` with sample files as fixtures
- No changes to models, engine, dashboard, FTP handler, or dedup logic
