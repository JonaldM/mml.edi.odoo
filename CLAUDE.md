# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## What This Directory Contains

Two distinct systems share this directory:

1. **`mml_edi/` — Odoo 19 Python module** (the active development target): a customer-agnostic EDI engine that replaces the legacy .NET service. The module root is the directory itself (`__manifest__.py` is at the top level).

2. **`BriscoeEDI/`, `BriscoeEDI_TEST/`, `Dropship/` — Legacy .NET binaries** (deployed, no source here): compiled Windows services for Briscoes EDI and dropship order processing.

---

## Odoo Module: `mml_edi`

### Architecture

The module is structured as a processing pipeline:

```
FTP (EDIS VAN)
    │
    ▼
EDIFTPHandler          (models/edi_ftp.py)       — FTP/SFTP connection, retry, path traversal guard
    │
    ▼
BaseEDIParser          (parsers/base_parser.py)   — Abstract contract: parse_file() + generate_ack()
KestrelbyParser        (parsers/kestrelby.py)      — EDIFACT D96A: ORDERS(220) + ORDCHG(230) + ORDRSP(231)
KestrelbyIDOCParser    (parsers/kestrelby_idoc.py) — SAP ORDERSEXT stub (Phase 2 — raises NotImplementedError)
    │
    ▼  ParsedOrder / ParsedOrderLine (dataclasses in base_parser.py)
    │
    ▼
EDIProcessor           (models/edi_processor.py)  — AbstractModel, no DB table, orchestrates full pipeline
    │
    ├── product lookup cascade: barcode → internal ref → vendor_code → supplier_sku
    ├── issues: product_not_found (blocking), price_discrepancy (blocking), qty_shortfall (warning)
    ├── auto-confirm clean orders (if trading_partner.auto_confirm_clean = True)
    └── emits mml.event 'edi.order.processed' (billable unit)
    │
    ▼
EDIOrderReview         (models/edi_order_review.py) — Manual review dashboard; approve/reject/reset
EDIOrderIssue          (models/edi_order_issue.py)  — Per-line issue records with resolution workflow
EDILog                 (models/edi_log.py)           — Append-only audit log; use .log() helper always
    │
    ▼  On approve: generate_ack() → upload ORDRSP to FTP outbox
    │
    ▼
EDIService             (services/edi_service.py)    — mml.registry service 'edi'; handles outbound ASN
KestrelbyASNGenerator  (parsers/kestrelby_asn.py)    — Generates EDIFACT DESADV D96A (ASN)
```

### Key Models

| Model | Purpose |
|---|---|
| `edi.trading.partner` | Per-partner config: FTP credentials, parser class, pricelist, auto-confirm flag, order split mode |
| `edi.processor` | AbstractModel — entry point for cron (`run_scheduled_poll`) and manual poll; public API for tests: `process_parsed_order()`, `apply_change_order()` |
| `edi.order.review` | One per inbound PO/store; states: `pending_review → approved/rejected/auto_approved` |
| `edi.order.issue` | Attached to review; severities: `blocking` (prevents auto-confirm) / `warning` / `info` |
| `edi.log` | Audit trail — always use `self.env['edi.log'].log(partner, direction, event_type, status, message)` |
| `sale.order` | Extended with `edi_trading_partner_id`, `edi_review_id`, `is_edi_order` |
| `sale.order.line` | Extended with `edi_line_number`, `edi_price`, `edi_system_price`, `edi_qty_shortfall`, `edi_matched_by` |

### Integration with mml_base Platform

- `post_init_hook` registers capabilities: `edi.order.process`, `edi.asn.send`, `edi.invoice.send`
- `EDIService` is registered as `mml.registry.service('edi')`
- `EDIService.on_3pl_despatch_confirmed(event)` generates and uploads DESADV to EDIS VAN — **gated by `ir.config_parameter` `mml_edi.asn_enabled = '1'`** (default `'0'` — off until the legacy .NET service is retired)

### Parser Extension Points

- All parsers must subclass `BaseEDIParser` and implement `parse_file()` + `generate_ack()`
- New parser classes must be added to `_ALLOWED_PARSER_CLASSES` in `edi_trading_partner.py` (security allowlist — prevents arbitrary class loading)
- `client_ref_template` on `edi.trading.partner` controls SO `client_order_ref` format; supports `{po_number}` and `{store_code}` variables

### EDIFACT / File Handling Notes

- Kestrelby files may use Windows-1252 encoding with `\x92` (right single quote) as an alternate segment terminator — `_split_segments()` normalises this before parsing
- Processed files are renamed in-place on FTP: `{filename}.processed.{YYYYMMDDHHMMSS}` (not deleted)
- File-level deduplication via SHA-256 hash checked against `edi.log` (`event_type=file_download, status=success`)
- EAN-13 check digit validation is enforced before ORDRSP and DESADV generation — missing/invalid barcodes raise `UserError` to prevent silent partner rejection

### Outbound ASN (DESADV) Flow — ⚠️ NOT YET WIRED (design only)

No module emits `3pl.despatch.confirmed` yet, no `mml.event.subscription` is
registered for it (the uninstall hook deregisters one that install never creates),
and `mml_edi.asn_enabled` defaults off. The components below exist but the flow
does not run end-to-end — treat as roadmap (2026-06-10 review, finding M4).

Designed trigger: `mml.event` `3pl.despatch.confirmed`:
1. `EDIService.on_3pl_despatch_confirmed(event)` looks up `stock.picking`
2. Validates picking has a linked `sale.order` with an active `edi.trading.partner`
3. Groups `stock.move` lines by `location_dest_id.edi_store_gln` (custom field on `stock.location`)
4. `KestrelbyASNGenerator.generate(despatch)` builds EDIFACT DESADV D96A
5. Uploads via `EDIFTPHandler` to partner outbox; logs `ir.attachment` on the picking

### System Parameters

| Key | Default | Purpose |
|---|---|---|
| `mml_edi.asn_enabled` | `'0'` | Enable outbound DESADV — set to `'1'` after legacy .NET retirement |
| `mml_edi.sender_id` | (none) | Our own sender identity on the VAN — DESADV UNB S002 + NAD+SE |
| `mml_edi.kestrelby_buyer_gln` | (none) | Counterparty GLN — DESADV UNB S003 + NAD+BY |
| `mml_edi.default_unb_recipient_id` | (none) | Install-level fallback for the counterparty's VAN mailbox (production) |
| `mml_edi.default_unb_recipient_test_id` | (none) | Same, for the partner's TEST portal mailbox (a distinct identity) |
| `mml_edi.gs1_company_prefix` | (none) | GS1 company prefix SSCC-18 codes are minted under |
| `mml_edi.notify_from` | (none) | From-address for EDI notification mail |
| `mml.cron_alert_email` | (none) | Email address for cron failure alerts |

**Account-specific by design (19.0.1.3.0).** Every key marked `(none)` above
used to be a hardcoded code default. They are deployment identities — a VAN
mailbox, a GS1 prefix allocated to one company, a counterparty GLN — so the
module ships none of them: an unconfigured install fails closed with an explicit
error rather than putting one deployment's identity on another's wire.
`migrations/19.0.1.3.0/post-migration.py` backfills them on upgrade from the
deployment-local `_legacy_deployment_values.py` overlay (present only in this
repo, excluded from the productised tree), and only where the key is unset.

---

## Running Tests

```bash
# All pure-Python tests (no Odoo needed)
pytest -q

# Single test file
pytest tests/test_kestrelby_edifact_parser.py -q

# Single test by name
pytest tests/test_kestrelby_edifact_parser.py -k "test_parse_multistore" -q
```

Tests run from the `mml.edi/` directory. The `pytest.ini` restricts collection to `tests/` and uses `--import-mode=importlib` (required because the directory name contains a dot).

The `tests/conftest.py` bootstraps `mml_edi` as a Python package alias and stubs `odoo.*` so pure-Python tests can import parsers and services without a running Odoo instance.

Odoo integration tests (requiring `self.env`) must be run via `odoo-bin --test-enable -u mml_edi -d <db>`.

---

## Legacy .NET Services

### BriscoesEditOrder (`BriscoeEDI/` and `BriscoeEDI_TEST/`)

.NET Framework 4.8 Windows service: polls EDIS VAN FTP every 15 min, parses Briscoes POs, creates Odoo SOs via XML-RPC, sends ACKs. One Briscoes PO → multiple SOs (one per store). Order IDs: `{PONumber}_{StoreCode}` (e.g., `4500176806_1017`).

### DropshipGetTickets (`Dropship/`)

.NET Framework 4.6.1 Windows service: dropship orders via EDIS FTP + Aramex/Fastway API for consignment labels.

### Infrastructure

| Component | Production | Test |
|---|---|---|
| FTP (EDIS VAN) | `ftp://post.edis.co.nz` `/FromEDIS`, `/ToEDIS` | `/Test/FromEDIS`, `/Test/ToEDIS` |
| Odoo XML-RPC | `https://10.0.0.35:8443/MML_Production/xmlrpc/2` | `https://10.0.0.35:8443/ODOOTEST/xmlrpc/2` |
| PostgreSQL | `10.0.0.35:5432` `MML_Production` | `10.0.0.35:5432` `ODOOTEST` |
| SQL Server (audit) | `10.0.0.6` `BriscoesEDI` | `10.0.0.6` `BriscoesEDI_Test` |

**Odoo customer details:** Briscoes Group ID `3324`, pricelist `"Briscoes Products"`, company `"MML Limited"`.

### Deployment Notes

- Config lives entirely in `*.exe.config` (`appSettings`) — no env vars or CLI switches
- Swap prod ↔ test: swap active keys and `x`-prefixed keys in `appSettings`
- `ServiceSleepMins`: production = 15, test = 1
- `_OldVer/` folders contain previous-generation binaries — do not restore
- `EDI.MOVEDtoD/` is a legacy archive (2014-era sample files, WinSCP scripts) — reference only

## Available Commands

- `/tdd` — write parser tests first; pure-Python tests run without Odoo
- `/plan` — implementation plan before adding new parsers or pipeline stages
- `/code-review` — review before enabling production EDI flows
- `/security-scan` — check FTP path traversal guards, HMAC validation, allowlists
