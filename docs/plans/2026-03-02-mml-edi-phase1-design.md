# mml_edi Phase 1 — Design Document

**Date:** 2026-03-02
**Status:** Approved
**Sprint:** Phase 1 — Full module build (all models, engine, dashboard, FTP, security, data, wizards, tests)
**GitHub:** https://github.com/JonaldM/mml.edi.odoo

---

## Overview

Build `mml_edi`, a customer-agnostic Odoo 19 EDI module that replaces the existing .NET Windows service handling Briscoes Group purchase orders. Briscoes is the first trading partner; the architecture supports additional partners via configuration only.

**Key addition beyond original spec:** Full PO change order handling — Briscoes can send amendments to existing POs (qty, dates, line additions/removals). Changes always route to manual review; approved changes update the existing SO.

---

## Module Identity

- **Technical name:** `mml_edi`
- **Directory:** `mml.edi/`
- **Odoo version:** 19
- **Depends:** `sale`, `account`, `stock`, `mail`
- **Install sequence:** standalone (no hard imports from `mml_3pl` or `mml_freight_forwarder`)

---

## Architecture: Parallel Agent Build

The sprint uses **4 parallel build tracks**. Interface contracts are defined first; tracks execute concurrently.

### Interface Contracts (Pre-defined)

All tracks agree on these before building:

```python
# parsers/base_parser.py

@dataclass
class ParsedOrderLine:
    product_code: str
    description: str
    quantity: float
    unit_price: float
    uom: str | None
    line_number: int

@dataclass
class ParsedOrder:
    po_number: str
    store_code: str | None
    order_date: date
    requested_delivery_date: date | None
    delivery_address_code: str | None
    lines: list[ParsedOrderLine]
    document_type: str = "new_order"   # "new_order" | "change_order"
    change_reason: str | None = None   # Optional reason from EDI
    raw_data: str | None = None

class BaseEDIParser(ABC):
    def parse_file(self, raw_content: bytes, trading_partner) -> list[ParsedOrder]: ...
    def generate_ack(self, review_record) -> bytes: ...
```

```python
# models/edi_ftp.py

class EDIFTPHandler:
    def __init__(self, trading_partner): ...
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def list_files(self) -> list[str]: ...
    def download_file(self, filename: str) -> bytes: ...
    def upload_file(self, filename: str, content: bytes) -> None: ...
    def move_to_processed(self, filename: str) -> None: ...
    @contextmanager
    def connection(self): ...
```

---

## Build Tracks

### Track 1 — Data Layer (no dependencies)

Files:
- `parsers/base_parser.py` — dataclasses + BaseEDIParser ABC
- `models/edi_trading_partner.py`
- `models/edi_log.py`
- `models/edi_order_issue.py`
- `models/edi_order_review.py`
- `models/sale_order.py`
- `models/sale_order_line.py`

### Track 2 — Logic Layer (depends on Track 1 contracts)

Files:
- `models/edi_ftp.py` — FTP/SFTP handler, retry, timeouts
- `models/edi_processor.py` — processing engine (new_order + change_order paths)
- `parsers/briscoes.py` — stub parser with mock data

### Track 3 — UI/Config Layer (depends on Track 1 model definitions)

Files:
- `views/edi_trading_partner_views.xml`
- `views/edi_order_review_views.xml` — kanban + list + form
- `views/edi_order_issue_views.xml`
- `views/edi_log_views.xml`
- `views/sale_order_views.xml`
- `views/menuitems.xml`
- `security/ir.model.access.csv`
- `security/edi_security.xml`
- `data/ir_cron.xml`
- `data/ir_sequence.xml`
- `data/edi_trading_partner_briscoes.xml`
- `data/mail_template.xml`
- `wizards/edi_bulk_action.py`

### Track 4 — Quality Layer (depends on Track 1 + 2)

Files:
- `tests/common.py`
- `tests/test_deduplication.py`
- `tests/test_price_discrepancy.py`
- `tests/test_review_workflow.py`
- `tests/test_po_change_workflow.py`
- `tests/test_ftp_handler.py`
- `tests/test_processor.py`
- `__manifest__.py`
- `__init__.py`

---

## Models

### `edi.trading.partner`

Full spec as per `briscoes_edi_refactor_prompt.md` §1. Key fields:

| Field | Notes |
|---|---|
| `code` | Unique short code (BRISCOES, HARVNORM, etc.) |
| `parser_class` | Dotted Python path to parser implementation |
| `ftp_protocol` | `ftp` / `sftp` |
| `environment` | `production` / `test` — selects FTP paths |
| `order_split_mode` | `per_store` / `single` |
| `product_match_field` | `barcode` / `default_code` / `supplier_sku` |
| `client_ref_template` | e.g., `{po_number}_{store_code}` |
| `price_tolerance_pct` | Auto-accept threshold (default 0.0) |
| `auto_confirm_clean` | Auto-confirm orders with no blocking issues |

### `edi.order.review`

Base spec per §4, extended with:

| Added Field | Type | Description |
|---|---|---|
| `document_type` | Selection | `new_order` / `change_order` |
| `original_review_id` | Many2one → self | Links change_order back to original new_order review |
| `change_summary` | Text | Human-readable diff of what changed |

**States:** `pending_review` → `approved` / `rejected` / `auto_approved`

**Change order constraint:** Change orders are **always** routed to `pending_review`. `auto_confirm_clean` does not apply to change orders.

### `edi.order.issue`

Per spec §4. Issue types: `price_discrepancy`, `product_not_found`, `qty_shortfall`, `unknown_store`, `uom_mismatch`, `other`.

### `edi.log`

Per spec §6. Immutable audit trail. No create/write/unlink overrides needed — log entries are never modified.

### `sale.order` / `sale.order.line` Inherits

Per spec §8. No changes beyond spec.

---

## Processing Engine

### New Order Flow

```
File downloaded → hash dedup check → parse → for each ParsedOrder:
  → client_ref rendered → SO dedup check
  → SO created (draft) → lines created
  → stock check (warning issues)
  → price comparison (blocking issues if outside tolerance)
  → product not found (blocking issues, no SO line)
  → routing:
      blocking issues? → pending_review
      no issues + auto_confirm_clean? → auto_approved → confirm SO → queue ACK
  → edi.order.review created
  → edi.log entries
  → email alert if pending_review (optional)
```

### Change Order Flow

```
File downloaded → hash dedup check → parse → document_type == "change_order":
  → find original SO by client_ref (same PO number + store code)
  → compute diff (dates, qty, lines added/removed)
  → edi.order.review created: document_type=change_order, change_summary=diff
  → ALWAYS route to pending_review (ignore auto_confirm_clean)
  → link original_review_id
  → edi.log entries → email alert
```

### Change Order Approval (`action_approve` on change_order review)

1. Find existing SO
2. Apply diff:
   - Update `commitment_date` if delivery date changed
   - Update SO line quantities for changed lines
   - Add SO lines for new EDI lines
   - Remove SO lines for deleted EDI lines (if SO not yet confirmed) or flag for manual review (if confirmed)
3. Post chatter message on SO: "EDI change order approved: {change_summary}"
4. Log to `edi.log`
5. Queue ACK generation

### Transactional Safety

Each file processed in `cr.savepoint()`. Exception per file → logged, processing continues.

---

## FTP Handler

- `ftplib` for FTP, `paramiko` for SFTP
- 3 retries, exponential backoff: 2s, 4s, 8s
- Timeouts: 30s connect, 60s transfer
- `move_to_processed()`: rename with `.processed` suffix + timestamp
- All operations logged to `edi.log`

---

## Deduplication

| Check | Logic |
|---|---|
| File-level | SHA-256 hash checked against `edi.log.file_hash`. Duplicate → skip + log. |
| Order-level (new_order) | Client ref exists as SO in draft/sent/sale/done → skip + log. |
| Order-level (change_order) | File hash check only. Same PO number is expected. |
| Outbound ACK | ACK filename + hash tracked in `edi.log`. Never send same ACK twice. |

---

## Briscoes Parser Stub (Phase 1 Mock Data)

`BriscoesParser.parse_file()` returns 4 `ParsedOrder` objects representing:
1. **Clean new order** — 3 lines, all products found, prices match → auto-approves
2. **New order with issues** — 3 lines: one price discrepancy, one unknown product → pending_review
3. **Change order** — modifies store from order #1: qty change + new delivery date
4. **Duplicate new order** — same PO number as order #1 → tests dedup

Both methods marked `# PHASE 2: Replace with EDIFACT D96A parsing/ACK generation`.

---

## Review Dashboard

### Views
- **Kanban:** Grouped by state, pending_review first. Cards: trading partner, PO number, document_type badge, issue count, blocking count, received date.
- **List:** Default filter "Needs Review". Columns include document_type indicator.
- **Form:** State bar, action buttons. Tabs: Issues, SO Lines, Log, Raw Data.
  - Change orders: additional "Change Summary" tab showing the diff.

### Bulk Actions Wizard

Handles both `new_order` and `change_order` review types. Operates on `pending_review` records only.

---

## Security

| Group | Permissions |
|---|---|
| `edi_user` | View/review dashboard, approve/reject orders, view logs |
| `edi_manager` | Configure trading partners, view all logs, reset reviews, bulk actions |

No multi-company scoping for now.

---

## Menu Structure

```
EDI
├── Dashboard          (edi.order.review kanban)
├── Orders
│   ├── Pending Review
│   ├── All Reviews
│   └── Sales Orders (EDI)
├── Logs               (edi.log list)
└── Configuration
    └── Trading Partners
```

---

## Briscoes Default Data

`edi_trading_partner_briscoes.xml` installs default record:

| Field | Value |
|---|---|
| name | Briscoes Group |
| code | BRISCOES |
| edi_format | edifact_d96a |
| parser_class | mml_edi.parsers.briscoes.BriscoesParser |
| ftp_host | post.edis.co.nz |
| ftp_port | 21 |
| ftp_protocol | ftp |
| ftp_inbox_path | /FromEDIS |
| ftp_outbox_path | /ToEDIS |
| ftp_test_inbox_path | /Test/FromEDIS |
| ftp_test_outbox_path | /Test/ToEDIS |
| environment | production |
| price_tolerance_pct | 0.0 |
| auto_confirm_clean | True |
| poll_interval_minutes | 15 |
| order_split_mode | per_store |
| product_match_field | barcode |
| client_ref_template | {po_number}_{store_code} |

FTP credentials and alert email not in data file — set manually post-install.

---

## Tests

| File | Coverage |
|---|---|
| `test_deduplication.py` | File hash dedup, SO ref dedup for new_order, hash-only dedup for change_order |
| `test_price_discrepancy.py` | Within tolerance (auto), outside tolerance (blocking), tolerance boundary |
| `test_review_workflow.py` | new_order state transitions, approve/reject/reset |
| `test_po_change_workflow.py` | Change order routing, diff computation, approve applies diff to SO |
| `test_ftp_handler.py` | Connect/disconnect (mocked), retry logic, move_to_processed |
| `test_processor.py` | Full pipeline with mock ParsedOrder data, each scenario |

---

## Phase 2 (Not in scope)

When Briscoes provides sample EDIFACT D96A files:
1. Replace `BriscoesParser.parse_file()` stub with real parsing
2. Replace `BriscoesParser.generate_ack()` stub with real ACK generation
3. Add parser unit tests with sample files as fixtures
4. End-to-end integration test on test FTP environment

No changes to models, engine, dashboard, or dedup logic needed.

---

## 3PL Awareness

When a change order is approved and the SO is updated, a chatter message is posted on the SO. 3PL (Mainfreight) notification is **out of scope for Phase 1** — the `mml_3pl` module will handle this separately when it detects SO changes on EDI orders.
