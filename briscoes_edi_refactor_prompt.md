# Prompt: Build Customer-Agnostic EDI Module for Odoo 19

## Context

We are building a **customer-agnostic EDI integration module** for Odoo 19 (self-hosted). The module must support multiple retail trading partners, each with their own EDI format, FTP configuration, product mapping, and pricing rules. Briscoes Group is the first trading partner — we have an existing .NET Windows service handling their EDI that is being retired and replaced by this module.

**Infrastructure:** Odoo 19, PostgreSQL on `10.0.0.35`, company "MML Limited". We distribute ~400 SKUs across 5 brands through major NZ/AU retailers.

**What we are replacing (Briscoes only):** A .NET Framework 4.8 service that polls EDIS VAN FTP for Briscoes PO files (EDIFACT ORDERS D96A), creates Odoo SOs via XML-RPC, and sends ACK files back. One Briscoes PO generates multiple SOs (one per store, ref format `{PONumber}_{StoreCode}`). The .NET service has no dedup, no manual review capability, and rejects entire orders on any price mismatch — all of these are being fixed.

**This is a live integration.** Product matching, store-to-partner mapping, pricelists, and delivery addresses are already configured in Odoo and working. The parser (to be built later when sample files are provided) just needs to extract the same fields the .NET service currently extracts. Do not treat any Odoo-side mapping as an open question.

---

## Build Scope — Phase 1 (Now)

Build the complete module **except** the Briscoes EDIFACT parser and ACK generator. Those two components require sample EDI files that will be provided in Phase 2. Everything else — models, processing engine, FTP handler, dedup, review dashboard, price discrepancy workflow, views, security, cron, tests — should be fully implemented and functional.

The Briscoes parser class should exist as a concrete stub: correct class structure, correct method signatures, returns hardcoded/mock `ParsedOrder` data for testing, with clear `# PHASE 2: Replace with EDIFACT D96A parsing` markers where real parsing logic will go. The ACK generator should be similarly stubbed. This allows the entire pipeline to be tested end-to-end with mock data before real EDI files are connected.

---

## Requirements

### 1. Multi-Customer Architecture — Trading Partner Model

The core abstraction is the **Trading Partner** (`edi.trading.partner`). All EDI configuration, format handling, and business rules are scoped to a trading partner. Adding a new customer means creating a new trading partner record and (if needed) a new parser class.

#### Model: `edi.trading.partner`

| Field | Type | Description |
|---|---|---|
| `name` | Char | Display name (e.g., "Briscoes Group") |
| `code` | Char | Short code, unique (e.g., `BRISCOES`, `HARVNORM`) — used in references and file naming |
| `partner_id` | Many2one → res.partner | Linked Odoo customer |
| `active` | Boolean | Enable/disable this trading partner |
| `edi_format` | Selection | EDI format: `edifact_d96a` / `edifact_d01b` / `csv` / `custom` — extensible |
| `parser_class` | Char | Python dotted path to the parser class (e.g., `mml_edi.parsers.briscoes.BriscoesParser`) — allows per-customer parser logic |
| **FTP Configuration** | | |
| `ftp_protocol` | Selection | `ftp` / `sftp` |
| `ftp_host` | Char | FTP server hostname |
| `ftp_port` | Integer | Default 21 (FTP) or 22 (SFTP) |
| `ftp_user` | Char | FTP username |
| `ftp_password` | Char | FTP password (stored via Odoo's encrypted field or system parameter) |
| `ftp_inbox_path` | Char | Inbound directory path (e.g., `/FromEDIS`) |
| `ftp_outbox_path` | Char | Outbound directory path (e.g., `/ToEDIS`) |
| `ftp_test_inbox_path` | Char | Test inbound path |
| `ftp_test_outbox_path` | Char | Test outbound path |
| `environment` | Selection | `production` / `test` — determines which FTP paths to use |
| **Processing Rules** | | |
| `pricelist_id` | Many2one → product.pricelist | Pricelist for price comparison |
| `price_tolerance_pct` | Float | Auto-accept threshold for price discrepancies (default 0.0 = exact match) |
| `auto_confirm_clean` | Boolean | Auto-confirm orders with no blocking issues |
| `poll_interval_minutes` | Integer | Cron polling interval (default 15) |
| `order_split_mode` | Selection | `per_store` / `single` — Briscoes = per_store (one SO per store code), other customers might send one PO = one SO |
| `product_match_field` | Selection | `barcode` / `default_code` / `supplier_sku` — which Odoo field to match EDI product codes against |
| `client_ref_template` | Char | Python format string for SO client reference (e.g., `{po_number}_{store_code}` for Briscoes, `{po_number}` for single-order customers) |
| **Notification** | | |
| `alert_email_ids` | Many2many → res.partner | Recipients for error alerts |
| `alert_on_issues` | Boolean | Send email when orders route to review |
| **Methods** | | |
| `test_ftp_connection()` | | Button on form to test FTP connectivity |
| `run_poll_now()` | | Button to trigger immediate poll (bypasses cron) |

#### Parser Architecture

Each trading partner points to a parser class. All parsers inherit from a base class:

```python
class BaseEDIParser:
    """Base class for EDI parsers. One subclass per EDI format/customer."""

    def parse_file(self, raw_content: bytes, trading_partner) -> list[ParsedOrder]:
        """Parse raw file content into structured order data.
        Returns a list because one file may contain multiple orders."""
        raise NotImplementedError

    def generate_ack(self, review_record) -> bytes:
        """Generate acknowledgement file content for a processed order."""
        raise NotImplementedError

@dataclass
class ParsedOrder:
    """Standardised intermediate representation — parser output, SO creator input."""
    po_number: str
    store_code: str | None          # None for single-order customers
    order_date: date
    requested_delivery_date: date | None
    delivery_address_code: str | None
    lines: list[ParsedOrderLine]
    raw_data: str | None = None     # Original EDI content for debugging/audit

@dataclass
class ParsedOrderLine:
    product_code: str               # Matched against trading_partner.product_match_field
    description: str
    quantity: float
    unit_price: float               # Price from EDI (what customer expects to pay)
    uom: str | None
    line_number: int                # EDI line number for ACK reference
```

**Phase 1 Briscoes parser stub:** Implement `BriscoesParser` with `parse_file()` returning mock `ParsedOrder` data (2 stores, 3 lines each, one with a deliberate price discrepancy, one with an unknown product code). `generate_ack()` returns a placeholder bytestring. Mark both methods with `# PHASE 2` comments.

### 2. Core EDI Processing Engine

This is customer-agnostic. It operates on `ParsedOrder` objects from any parser.

- **FTP Polling:** A single `ir.cron` that iterates all active trading partners. Use `ftplib` for FTP, `paramiko` for SFTP. Download all files from inbox, process each, track in `edi.log`.
- **SO Creation from ParsedOrder:**
  - Partner: from `trading_partner.partner_id` (or mapped store contact if `order_split_mode == per_store` — look up child contact by `ref` field matching store code)
  - Pricelist: from `trading_partner.pricelist_id`
  - Company: "MML Limited"
  - Client reference: rendered from `trading_partner.client_ref_template` using `po_number` and `store_code`
  - Product matching: look up product using field specified by `trading_partner.product_match_field`
  - Order lines: from `ParsedOrderLine` data
- **Stock Check:** On each line, check `qty_available`. Record shortfall on custom SO line field. Create the line regardless. Create `edi.order.issue` with severity `warning` (non-blocking).
- **Price Comparison:** On each line, compare EDI unit price vs pricelist price for the matched product. Use `pricelist_id._get_product_price()` or equivalent. Apply tolerance from `trading_partner.price_tolerance_pct`. Create `edi.order.issue` with severity `blocking` if outside tolerance.
- **Product Not Found:** If product lookup fails for a line, create `edi.order.issue` with type `product_not_found`, severity `blocking`. Do NOT create the SO line. Store the raw EDI line data on the issue for manual resolution.
- **Issue Detection → Routing:** After processing all lines:
  - Zero blocking issues AND `auto_confirm_clean` is True → set review state to `auto_approved`, confirm SO, queue ACK generation
  - Any blocking issues → set review state to `pending_review`, leave SO in `draft`, do NOT generate ACK
- **ACK Generation:** Call the trading partner's parser `generate_ack()` method. Upload to FTP outbox. Track in `edi.log`.
- **Logging:** All activity to `edi.log` — always linked to the trading partner.
- **Email Alerts:** Via Odoo mail system to `trading_partner.alert_email_ids` on errors and (optionally) on orders routed to review.

### 3. Idempotency & Deduplication

- **File-level dedup:** SHA-256 hash of file content checked against `edi.log` before processing. Duplicate → skip + log.
- **Order-level dedup:** Check for existing SO with same client reference (rendered from template). Rules:
  - Existing SO in `draft` / `sent` → skip, log as duplicate
  - Existing SO `cancelled` → allow re-creation
  - Existing SO `sale` / `done` → skip, log as duplicate
- **FTP file tracking:** After download, move/rename file on FTP server. Track filename in `edi.log`.
- **Outbound dedup:** ACK filenames/hashes tracked in `edi.log`. Never send same ACK twice.

### 4. EDI Review Dashboard

Customer-agnostic — shows all trading partners' orders, filterable by partner.

#### Model: `edi.order.review`

| Field | Type | Description |
|---|---|---|
| `name` | Char | Auto-sequence reference (e.g., `EDI/BRISCOES/2026/0001`) |
| `trading_partner_id` | Many2one → edi.trading.partner | Source trading partner |
| `customer_po_number` | Char | Original PO number from the customer |
| `store_code` | Char | Store/location code (null for single-order customers) |
| `sale_order_id` | Many2one → sale.order | Linked SO (created in draft) |
| `state` | Selection | `pending_review` / `approved` / `rejected` / `auto_approved` |
| `issue_ids` | One2many → edi.order.issue | Individual issues found |
| `issue_count` | Integer (computed, stored) | Total issues |
| `blocking_issue_count` | Integer (computed, stored) | Blocking issues only |
| `edi_file_hash` | Char | SHA-256 of source file |
| `edi_filename` | Char | Original filename from FTP |
| `edi_raw_data` | Text | Raw EDI content for debugging |
| `received_date` | Datetime | When the EDI file was received |
| `reviewed_by` | Many2one → res.users | Who approved/rejected |
| `reviewed_date` | Datetime | When reviewed |
| `notes` | Text | Free-text notes |

**State transition methods (implement as proper Odoo workflow):**
- `action_approve()` — resolve all pending issues as `accepted`, confirm SO, queue ACK
- `action_approve_corrected()` — confirm SO at current (possibly corrected) line values, queue ACK
- `action_reject()` — cancel SO, queue rejection ACK
- `action_reset_to_review()` — edi_manager only, allows re-opening a rejected/approved review

#### Model: `edi.order.issue`

| Field | Type | Description |
|---|---|---|
| `review_id` | Many2one → edi.order.review | Parent review record |
| `issue_type` | Selection | `price_discrepancy` / `product_not_found` / `qty_shortfall` / `unknown_store` / `uom_mismatch` / `other` |
| `severity` | Selection | `blocking` / `warning` / `info` |
| `description` | Text | Human-readable description |
| `edi_line_data` | Text/Json | Raw EDI line data for reference |
| `sale_order_line_id` | Many2one → sale.order.line | Linked SO line if applicable |
| `edi_price` | Float | Price from EDI file |
| `system_price` | Float | Price from Odoo pricelist |
| `price_difference_pct` | Float (computed) | Absolute % difference |
| `resolution` | Selection | `pending` / `accepted` / `corrected` / `rejected` |
| `resolved_by` | Many2one → res.users | |
| `resolved_date` | Datetime | |
| `resolution_notes` | Text | Optional notes on resolution |

**Issue resolution methods:**
- `action_accept()` — accept the EDI value as-is (e.g., accept Briscoes' price)
- `action_correct(new_value)` — update the linked SO line with a corrected value
- `action_reject()` — reject this line

#### Dashboard Views

- **Kanban:** Grouped by state (`pending_review` first), cards show trading partner, PO number, issue count, blocking count, received date
- **List view** with filters: "Needs Review" (default), "Price Issues", "Product Issues", "By Trading Partner", "Today", "This Week", "Auto-Approved"
- **Form view:** Full review record with:
  - Header: state bar, action buttons (Approve / Approve with Corrections / Reject)
  - Top section: trading partner, PO number, store code, dates, linked SO (clickable)
  - Issues tab: tree view of issues with inline resolution buttons and editable price fields for corrections
  - SO Lines tab: embedded view of the linked SO's order lines (read-only until correction mode)
  - Log tab: filtered `edi.log` entries for this review
  - Raw Data tab: `edi_raw_data` in a code-formatted text field

#### Bulk Actions (Wizard)

- `edi.bulk.approve` wizard — triggered from list view multi-select
- Options: "Approve all selected" / "Reject all selected"
- Only operates on records in `pending_review` state
- Logs each action individually

### 5. Price Discrepancy Handling

- Compare EDI price vs `trading_partner.pricelist_id` price for every line
- Tolerance from `trading_partner.price_tolerance_pct` (default 0%)
- Price comparison uses the same logic Odoo uses to compute pricelist prices (call `pricelist_id._get_product_price()` or equivalent to handle pricelist rules, discounts, etc.)
- When discrepancy exceeds tolerance:
  - SO line created with **EDI price** (what customer expects to pay)
  - `edi.order.issue` created: type `price_discrepancy`, severity `blocking`
  - Description includes: product name, product code, EDI price, system pricelist price, absolute difference, % difference
  - Order routed to `pending_review`
- On the review dashboard, reviewer can:
  - Accept EDI price (mark issue `accepted` — SO line stays at EDI price)
  - Correct to system price (mark issue `corrected` — update SO line to pricelist price)
  - Enter manual override (mark issue `corrected` — update SO line to entered price, log the override)
  - Flag for pricelist update (creates an activity on the pricelist record as a reminder)

### 6. EDI Log Model

#### Model: `edi.log`

| Field | Type | Description |
|---|---|---|
| `name` | Char | Auto-generated reference |
| `trading_partner_id` | Many2one → edi.trading.partner | |
| `timestamp` | Datetime | When the event occurred |
| `direction` | Selection | `inbound` / `outbound` / `internal` |
| `event_type` | Selection | `file_download` / `file_parse` / `order_created` / `duplicate_skipped` / `ack_sent` / `review_approved` / `review_rejected` / `error` / `ftp_connection` / `info` |
| `filename` | Char | EDI filename if applicable |
| `file_hash` | Char | SHA-256 if applicable |
| `sale_order_id` | Many2one → sale.order | Linked SO if applicable |
| `review_id` | Many2one → edi.order.review | Linked review if applicable |
| `user_id` | Many2one → res.users | Who triggered the action |
| `status` | Selection | `success` / `warning` / `error` |
| `message` | Text | Human-readable log message |
| `detail` | Text | Technical detail / stack trace for errors |

**Views:** List view with filters by trading partner, direction, event type, status, date range. No form view needed (read-only model).

### 7. FTP Handler

Reusable FTP/SFTP connection handler used by the processing engine.

```python
class EDIFTPHandler:
    """Manages FTP/SFTP connections for a trading partner."""

    def __init__(self, trading_partner):
        """Initialize from trading partner config."""

    def connect(self) -> None:
        """Establish connection. Raises EDIFTPError on failure."""

    def disconnect(self) -> None:
        """Clean disconnect."""

    def list_files(self) -> list[str]:
        """List files in the active inbox directory."""

    def download_file(self, filename: str) -> bytes:
        """Download a single file. Returns raw bytes."""

    def upload_file(self, filename: str, content: bytes) -> None:
        """Upload a file to the active outbox directory."""

    def move_to_processed(self, filename: str) -> None:
        """Move/rename a processed file in the inbox."""

    @contextmanager
    def connection(self):
        """Context manager for auto-connect/disconnect."""
```

- Implement retry logic: 3 attempts with exponential backoff (2s, 4s, 8s)
- Timeout: 30 seconds connect, 60 seconds transfer
- Log all FTP operations to `edi.log`
- `test_ftp_connection()` on trading partner calls `connect()` + `list_files()` and reports result

### 8. Sale Order / Sale Order Line Extensions

#### `sale.order` inherit

| Field | Type | Description |
|---|---|---|
| `edi_trading_partner_id` | Many2one → edi.trading.partner | Source trading partner (null for non-EDI orders) |
| `edi_review_id` | Many2one → edi.order.review | Linked review record |
| `is_edi_order` | Boolean (computed) | True if `edi_trading_partner_id` is set |

#### `sale.order.line` inherit

| Field | Type | Description |
|---|---|---|
| `edi_line_number` | Integer | Original line number from EDI file |
| `edi_price` | Float | Price as received from EDI |
| `edi_system_price` | Float | Pricelist price at time of processing |
| `edi_price_discrepancy` | Boolean (computed) | True if edi_price != edi_system_price beyond tolerance |
| `edi_qty_shortfall` | Float | Qty requested minus qty available (0 if sufficient) |

### 9. Module Structure

```
mml_edi/
├── __manifest__.py
├── __init__.py
├── models/
│   ├── __init__.py
│   ├── edi_trading_partner.py   # edi.trading.partner — core config model
│   ├── edi_log.py               # edi.log — audit trail
│   ├── edi_order_review.py      # edi.order.review — review model + workflow
│   ├── edi_order_issue.py       # edi.order.issue — per-line issue tracking
│   ├── edi_processor.py         # Customer-agnostic processing engine
│   ├── edi_ftp.py               # FTP/SFTP connection handler
│   ├── sale_order.py            # sale.order inherit — EDI fields
│   └── sale_order_line.py       # sale.order.line inherit — EDI line fields
├── parsers/
│   ├── __init__.py
│   ├── base_parser.py           # BaseEDIParser + ParsedOrder/ParsedOrderLine dataclasses
│   └── briscoes.py              # BriscoesParser — STUBBED with mock data for Phase 1
├── data/
│   ├── ir_cron.xml              # Scheduled action for EDI polling
│   ├── ir_sequence.xml          # Sequences for edi.order.review, edi.log
│   ├── edi_trading_partner_briscoes.xml  # Default Briscoes trading partner record
│   └── mail_template.xml        # Alert email templates
├── views/
│   ├── edi_trading_partner_views.xml  # Trading partner config form + list
│   ├── edi_order_review_views.xml     # Review dashboard: kanban, list, form
│   ├── edi_order_issue_views.xml      # Issue list/form (mostly inline on review)
│   ├── edi_log_views.xml              # Log list view
│   ├── sale_order_views.xml           # Inherited SO views — show EDI fields
│   └── menuitems.xml                  # Menu structure under a top-level "EDI" menu
├── security/
│   ├── ir.model.access.csv
│   └── edi_security.xml         # Groups: edi_user, edi_manager
├── wizards/
│   ├── __init__.py
│   └── edi_bulk_action.py       # Bulk approve/reject wizard
├── static/
│   └── description/
│       └── icon.png             # Module icon
└── tests/
    ├── __init__.py
    ├── test_deduplication.py
    ├── test_price_discrepancy.py
    ├── test_review_workflow.py
    ├── test_ftp_handler.py
    ├── test_processor.py
    └── common.py                # Shared test fixtures, mock ParsedOrder factory
```

### 10. Briscoes Default Data (XML Data File)

Create `data/edi_trading_partner_briscoes.xml` that installs a default trading partner record:

| Field | Value |
|---|---|
| `name` | Briscoes Group |
| `code` | BRISCOES |
| `partner_id` | ref to Briscoes Group partner (ID 3324) |
| `edi_format` | `edifact_d96a` |
| `parser_class` | `mml_edi.parsers.briscoes.BriscoesParser` |
| `ftp_protocol` | `ftp` |
| `ftp_host` | `post.edis.co.nz` |
| `ftp_port` | 21 |
| `ftp_inbox_path` | `/FromEDIS` |
| `ftp_outbox_path` | `/ToEDIS` |
| `ftp_test_inbox_path` | `/Test/FromEDIS` |
| `ftp_test_outbox_path` | `/Test/ToEDIS` |
| `environment` | `production` |
| `pricelist_id` | ref to "Briscoes Products" pricelist |
| `price_tolerance_pct` | 0.0 |
| `auto_confirm_clean` | True |
| `poll_interval_minutes` | 15 |
| `order_split_mode` | `per_store` |
| `product_match_field` | `barcode` |
| `client_ref_template` | `{po_number}_{store_code}` |

FTP credentials (`ftp_user`, `ftp_password`) and `alert_email_ids` should NOT be in the data file — these are set manually post-install.

### 11. Non-Functional Requirements

- **Odoo 19 compatible** — current ORM patterns, no deprecated API
- **Transactional safety** — each PO file processed in its own `cr.savepoint()`. If one file fails, others still process. Catch and log exceptions per file, continue processing.
- **FTP resilience** — 3 retries with exponential backoff, configurable timeouts, proper connection cleanup in finally blocks
- **Audit trail** — every action logged to `edi.log`
- **No SQL Server** — all state in PostgreSQL/Odoo
- **No Windows** — runs on Linux
- **Security groups:**
  - `edi_user` — view dashboard, review/approve/reject orders, view logs
  - `edi_manager` — configure trading partners, view all logs, reset reviews, access bulk actions
- **Record rules:** Users see reviews for all trading partners (no multi-company scoping needed for now)
- **Tests:** Full coverage of dedup, price comparison, review state machine, issue resolution, processor pipeline (using mock ParsedOrder data). FTP tests should mock the connection.

### 12. Menu Structure

```
EDI                                    (top-level menu)
├── Dashboard                          (edi.order.review kanban — default view)
├── Orders
│   ├── Pending Review                 (edi.order.review list, filtered)
│   ├── All Reviews                    (edi.order.review list, no filter)
│   └── Sales Orders (EDI)            (sale.order list, filtered to EDI orders)
├── Logs                               (edi.log list)
└── Configuration
    └── Trading Partners               (edi.trading.partner list/form)
```

---

## Deliverables (Phase 1)

1. Complete, installable Odoo 19 module with all models, views, security, cron, and tests
2. Fully functional processing engine that can take mock `ParsedOrder` data through the entire pipeline: dedup check → SO creation → price comparison → issue detection → routing → review → approve/reject → ACK queue
3. Briscoes parser stubbed with mock data that exercises all code paths (clean order, price discrepancy, product not found, duplicate)
4. Review dashboard fully operational with all views, filters, and action buttons
5. FTP handler implemented and testable (with mock for unit tests, real connection for integration via "Test Connection" button)
6. README with installation and configuration instructions

## Build Order

1. `base_parser.py` — dataclasses + base class
2. `edi_trading_partner.py` — model with all fields
3. `edi_log.py` — audit model
4. `edi_order_issue.py` — issue model
5. `edi_order_review.py` — review model + state machine + action methods
6. `sale_order.py` + `sale_order_line.py` — SO/SOL inherits
7. `edi_ftp.py` — FTP handler
8. `edi_processor.py` — processing engine (this ties everything together)
9. `briscoes.py` — stubbed parser with mock data
10. Security — groups, access rules, record rules
11. Views — trading partner form, review dashboard (kanban/list/form), log list, SO inherited views, menus
12. Data files — sequences, Briscoes default record, cron, mail templates
13. Wizards — bulk approve/reject
14. Tests — all non-parser logic
15. `__manifest__.py` + `__init__.py` wiring

## Phase 2 (When Sample Files Provided)

When the sample inbound PO file and outbound ACK file are provided:
1. Replace `BriscoesParser.parse_file()` stub with real EDIFACT D96A parsing
2. Replace `BriscoesParser.generate_ack()` stub with real ACK generation
3. Add parser-specific unit tests with the sample files as fixtures
4. End-to-end integration test with real EDI data through the full pipeline
5. Connect to test FTP environment and validate

No changes to any other module component should be needed for Phase 2.

## Adding Future Customers

To onboard a new retail customer (e.g., Harvey Norman):
1. Create a new parser class in `parsers/` inheriting from `BaseEDIParser`
2. Create an `edi.trading.partner` record with their FTP config, pricelist, and format settings
3. No changes to core processing, dashboard, dedup, or review logic needed

This is the target architecture — one module, many customers, only parsers are customer-specific.
