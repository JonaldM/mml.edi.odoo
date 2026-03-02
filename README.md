# mml_edi — Electronic Data Interchange for Odoo 19

Odoo 19 module for automated EDI order exchange with retail partners. Replaces a legacy .NET Windows service that handled Briscoes Group purchase orders manually.

**Company:** MML Consumer Products Ltd (NZ) · **Platform:** `mml_base`

---

## What it does

Retail partners send purchase orders electronically (SFTP/FTP or email pickup). This module:

1. Polls configured FTP/SFTP paths for inbound order files
2. Parses each file using the partner's document profile
3. Routes the parsed order to an existing `sale.order` (update) or creates a new one
4. Lands any routing exceptions in a review queue for manual resolution
5. Emits an `edi.order.processed` event (billing ledger entry per order line)

On despatch confirmation (`3pl.despatch.confirmed`), the EDI service sends an ASN back to the partner — wired via `mml_base` event subscriptions when `mml_freight` and `stock_3pl_core` are also installed.

---

## Module structure

```
mml_edi/
├── __manifest__.py
├── hooks.py                    ← registers EDIService + capabilities on install
├── models/
│   ├── edi_trading_partner.py  ← partner profile (FTP config, document format, mapping)
│   ├── edi_processor.py        ← inbound order processing engine
│   ├── edi_order_review.py     ← review queue for orders needing attention
│   ├── edi_order_issue.py      ← line-level issue tracking
│   └── edi_log.py              ← processing log (one entry per file received)
├── services/
│   └── edi_service.py          ← EDIService (registered with mml.registry as 'edi')
├── parsers/
│   └── briscoes_parser.py      ← Briscoes-specific document parser (Phase 1 stub)
├── wizards/
│   └── edi_bulk_action.py      ← bulk approve / reprocess / reject wizard
├── security/
├── views/
└── data/
    ├── edi_trading_partner_briscoes.xml  ← Briscoes seed record
    ├── ir_cron.xml                       ← polling cron (every 15 min, inactive by default)
    └── ir_sequence.xml                   ← EDI document reference sequence
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
| Briscoes Group (Briscoes, Rebel Sport, Living & Giving) | Phase 1 — stub parser | CSV (EDIFACT Phase 2 pending partner spec) | SFTP |
| Harvey Norman NZ | Scoped — awaiting partner spec | EDIFACT | SFTP / VAN |
| Animates | Scoped — format TBC | CSV initially | TBC |
| PetStock (AU/NZ) | Early scope | TBC | TBC |

Phase 2 adds full EDIFACT D96A parsing once partner technical specifications are confirmed.

---

## Installation

```bash
# Install mml_base first
odoo-bin -d <db> -i mml_base --stop-after-init

# Then install mml_edi
odoo-bin -d <db> -i mml_edi --stop-after-init
```

### Post-install configuration

1. Go to **EDI → Configuration → Trading Partners** and review the Briscoes seed record
2. Set SFTP host, username, and password on the Briscoes trading partner record
3. Enable the polling cron in **Settings → Technical → Scheduled Actions → EDI: Poll Inbound Orders**
4. Run a manual poll to verify connectivity before enabling the cron

---

## Review queue

Orders that cannot be automatically processed land in **EDI → Orders → Pending Review**:

| Status | Meaning |
|--------|---------|
| `pending` | Awaiting review |
| `approved` | Manually approved and linked to a sale order |
| `rejected` | Rejected with reason (partner will be notified if configured) |
| `reprocessed` | Sent back through the parser after data correction |

The bulk action wizard lets you approve, reject, or reprocess multiple orders at once.

---

## Running tests

```bash
# Structural tests (no Odoo instance required)
cd briscoes.edi
pytest mml.edi/tests/ -m "not odoo_integration" -v

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
