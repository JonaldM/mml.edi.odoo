# mml_edi — Electronic Data Interchange for Odoo 19

Odoo 19 module for automated EDI order exchange with retail partners. Replaces the legacy .NET Kestrelby EDI bridge at the Odoo 15→19 cutover — see `docs/PROD_CUTOVER.md` for the authoritative go-live runbook (double-poll guard, test-mailbox sequencing, rollback).

**Company:** Cadence Demo Trading Ltd (NZ) · **Platform:** `mml_base`

---

## What it does

Retail partners send purchase orders electronically (SFTP/FTP or email pickup). This module:

1. Polls configured FTP/SFTP paths for inbound order files (duplicate files are
   skipped by content hash; the processed marker is only written after **every**
   store-order in a file succeeds, so partial failures retry safely)
2. Parses each file using the partner's allow-listed parser class
3. Splits per store, routes to an existing `sale.order` (update/cancel via ORDCHG)
   or creates new ones — unknown store codes raise a **blocking review issue**
   rather than silently delivering to head office
4. Lands exceptions (price discrepancy, product not found, uom mismatch…) in a
   review queue for manual resolution
5. Generates one ORDRSP (ACK) per PO **per exchange** — deferred until all of the
   PO's store-orders are resolved, idempotent per exchange (ORDCHG/re-orders get
   their own response), with a 30-min retry cron for failed uploads
6. Emits an `edi.order.processed` event (billing ledger entry per order)

**ASN status:** outbound ASN on despatch confirmation is designed but **not yet
wired** — there is no `3pl.despatch.confirmed` emitter in the 3PL modules yet and
`mml_edi.asn_enabled` defaults off. Treat ASN as roadmap, not a working flow.

---

## Module structure

```
mml_edi/
├── __manifest__.py
├── hooks.py                    ← registers EDIService + capabilities on install
├── docs/
│   └── PROD_CUTOVER.md         ← 15→19 go-live runbook (authoritative)
├── models/
│   ├── edi_trading_partner.py  ← partner profile (FTP/SFTP config, parser allowlist,
│   │                             environment flag, circuit breaker, Fernet-encrypted creds)
│   ├── edi_processor.py        ← inbound engine (poll, hash-dedup, per-store savepoints,
│   │                             ACK retry cron method)
│   ├── edi_ftp.py              ← FTP/SFTP transport (host-key pinning, filename allowlist;
│   │                             credentials read via sudo so non-admin approvers can upload)
│   ├── edi_order_review.py     ← review queue + deferred per-PO/per-exchange ORDRSP
│   ├── edi_order_issue.py      ← line-level issue tracking
│   ├── edi_log.py              ← append-only processing log (file hashes, ACK status)
│   ├── sale_order.py / stock_location_ext.py
├── services/
│   └── edi_service.py          ← EDIService (registered with mml.registry as 'edi')
├── parsers/
│   ├── base_parser.py
│   ├── kestrelby_idoc.py       ← KestrelbyIDOCParser — iDOC XML parser + ORDRSP generator (PRODUCTION path)
│   ├── kestrelby.py            ← KestrelbyParser — EDIFACT D96A parser — NOT allow-listed (ships carton qty
│   │                             as eaches; re-enable only after CT→EA conversion is ported)
│   └── kestrelby_asn.py        ← KestrelbyASNGenerator — ASN builder (unwired — see ASN status above)
├── wizards/
│   ├── edi_bulk_action.py      ← bulk approve / reprocess / reject wizard
│   └── edi_seed_stores.py      ← seed Kestrelby store partners
├── migrations/
├── security/
├── views/
├── tests/                      ← pure-Python parser/processor tests + Odoo integration tests
└── data/
    ├── edi_trading_partner_kestrelby.xml  ← Kestrelby seed record
    ├── ir_cron.xml   ← "EDI: Poll Trading Partners" (15 min, ships INACTIVE — the runbook's
    │                    double-poll gate) + "EDI: Retry Pending ACKs" (30 min, active)
    ├── ir_sequence.xml                   ← EDI document reference sequence
    └── mail_template.xml
```

---

## Platform integration

`mml_edi` registers with `mml_base` on install:

| Registration | Value |
|---|---|
| Service name | `edi` (accessible via `env['mml.registry'].service('edi')`) |
| Capabilities | `edi.order.process`, `edi.asn.send`, `edi.invoice.send` |

Other modules can call EDI operations without a hard dependency — if `mml_edi` is not installed, `service('edi')` returns a NullService and all calls return `None` safely.

---

## Current partner support

| Partner | Status | Format | Transport |
|---------|--------|--------|-----------|
| Kestrelby Retail Group (Kestrelby, Vantekka Sports, Larkbury Living) | **Production-grade** — iDOC XML parser + per-PO/per-exchange ORDRSP, validated against real order fixtures (each-vs-carton and MML-code-vs-shipper-GTIN handling locked by tests). Cutover pending per `docs/PROD_CUTOVER.md`. | iDOC XML (EDIFACT D96A parser exists but is **de-listed** until CT→EA conversion is ported) | FTP/SFTP (EDIS VAN) |
| Dovemarch Home & Living | Scoped — awaiting partner spec | EDIFACT | SFTP / VAN |
| Nimbrel Pet Co | Scoped — format TBC | CSV initially | TBC |
| Palisade Pet Co (AU/NZ) | Early scope | TBC | TBC |

---

## Installation

```bash
# Install mml_base first
odoo-bin -d <db> -i mml_base --stop-after-init

# Then install mml_edi
odoo-bin -d <db> -i mml_edi --stop-after-init
```

### Post-install configuration

1. Go to **EDI → Configuration → Trading Partners** and review the Kestrelby seed record
2. Set FTP/SFTP host, username, and password on the Kestrelby trading partner record
   (verify `KestrelbyId` partner id and the exact pricelist name against production data)
3. Run a manual poll (**Run Poll Now**) against the partner's *test* mailbox to verify
   connectivity and parsing
4. Only at go-live, per `docs/PROD_CUTOVER.md`: stop the legacy .NET poller FIRST
   (double-poll guard), then enable **Settings → Technical → Scheduled Actions →
   EDI: Poll Trading Partners** (it ships inactive for exactly this reason)

---

## Review queue

Orders that cannot be automatically processed land in **EDI → Orders → Pending Review**:

| Status | Meaning |
|--------|---------|
| `pending_review` | Awaiting review (blocking issues: unknown store, product not found, price discrepancy…) |
| `approved` | Manually approved — sale order confirmed, ORDRSP queued once all of the PO's stores are resolved |
| `auto_approved` | Clean order auto-confirmed (when the partner's `auto_confirm_clean` is on) |
| `rejected` | Rejected with reason — reflected as a rejection (ABGRU) in the ORDRSP |

The bulk action wizard lets you approve or reject multiple orders at once; a manager
can also reset a review and re-approve, which generates a fresh per-exchange ORDRSP.

---

## Running tests

```bash
# Structural tests (no Odoo instance required) — from the module directory:
cd mml_edi
pytest -m "not odoo_integration" -q     # 128 pass as of 2026-06-10

# Odoo integration tests
odoo-bin --test-enable --stop-after-init -d testdb \
  -i mml_base,mml_edi \
  --test-tags=mml_edi
```

---

## Adding a new trading partner

1. Create an `edi.trading.partner` record in **EDI → Trading Partners**
2. Configure transport (SFTP host/path, credentials), document format, and field mappings
3. Implement a parser class in `parsers/<partner>_parser.py` inheriting the base parser interface
4. Register the parser key on the trading partner record
5. Add the partner's GLN/identifier to the document mapping

No code changes are required to the core processing engine — it dispatches to the parser registered on the trading partner record.

---

## License

LGPL-3. See `__manifest__.py`.
