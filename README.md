# mml_edi — Electronic Data Interchange for Odoo 19

Odoo 19 application (`19.0.1.2.0`) that runs MML Consumer Products' inbound/outbound
retail EDI. It replaces the legacy .NET Windows service that bridged Briscoes Group
orders at the Odoo 15→19 cutover, and adds a second live-scoped partner (Animates)
built to full SPS Commerce EDIFACT certification.

**Company:** MML Consumer Products Ltd (NZ) · **Platform:** `mml_base` · **License:** LGPL-3

See `docs/PROD_CUTOVER.md` for the authoritative go-live runbook (double-poll guard,
test-mailbox sequencing, rollback).

---

## What it does

Retail partners send purchase orders electronically via a VAN mailbox (EDIS,
plaintext FTP `post.edis.co.nz:21`, or SFTP). The module is a **customer-agnostic EDI
engine** — the core processor is partner-neutral and dispatches to a per-partner parser:

1. **Polls** each partner's FTP/SFTP inbox for order files (duplicate files skipped by
   SHA-256 content hash).
2. **Parses** each file with the partner's allow-listed parser class.
3. **Creates / updates `sale.order`s** — one per store for `per_store` partners; ORDCHG
   updates or cancels an existing SO; unknown store codes raise a **blocking review
   issue** rather than silently delivering to head office.
4. **Lands exceptions** (price discrepancy, product-not-found, unknown store, uom
   mismatch) in a review queue for manual resolution.
5. **Sends order responses** (ORDRSP / functional ACK) back to the partner — deferred
   until every store-order for a PO is resolved, and idempotent per exchange.
6. **Emits `edi.order.processed`** so the `mml_base` platform records a billing event.

It registers with `mml_base` as the **`edi` service**, so other MML modules can trigger
EDI operations (order process, ASN send, invoice send) without a hard dependency on
this module.

### Why it exists

- Retire the old .NET Briscoes EDI bridge (`BriscoeEDI/`, `Dropship/`, etc. in this
  directory — legacy binaries being decommissioned) as part of the Odoo 15→19 migration.
- Add a second live partner (Animates, NZ) requiring full UN/EDIFACT D.01B certification
  over SPS Commerce (CONTRL, ORDRSP, DESADV with SSCC, INVOIC).
- Recent hardening prepared Briscoes for go-live (dup-race lock, stock re-clamp,
  transaction boundaries, ACK idempotency) and merged the Animates EDIFACT build. The
  newest feature detects far-future **indent** (forward-commitment) POs and exempts them
  from the out-of-stock gate.

---

## Module layout

The Odoo module root **is** the top-level directory (`__manifest__.py` at top level).
This directory also holds legacy .NET services (`BriscoeEDI/`, `BriscoeEDI_TEST/`,
`Dropship/`, `EDI.MOVEDtoD/` — binaries/config only, being decommissioned).

```
mml_edi/
├── __manifest__.py                  ← version 19.0.1.2.0
├── hooks.py                         ← registers/deregisters the 'edi' service + capabilities
├── models/
│   ├── edi_processor.py             ← inbound engine (~2050 lines): poll → parse → SO →
│   │                                  review → ORDRSP; advisory lock, dedup, indent detect
│   ├── edi_trading_partner.py       ← partner config: transport, encrypted creds, parser
│   │                                  allow-list, oos_policy, circuit breaker, EDIFACT identity
│   ├── edi_order_review.py          ← review queue (one per PO/store) + deferred ORDRSP send-claim
│   ├── edi_order_issue.py           ← line-level blocking/warning/info issues + resolution
│   ├── edi_log.py                   ← append-only audit log (file hashes, ACK/CONTRL status)
│   ├── edi_ftp.py                   ← FTP/SFTP transport (host-key pinning, filename allow-list)
│   ├── sale_order.py                ← SO/line EDI fields + partial UNIQUE index (dup-SO backstop)
│   ├── stock_location_ext.py        ← stock.location.edi_store_gln
│   └── sscc_register.py             ← idempotent SSCC-18 minting (12-month uniqueness) for DESADV
├── parsers/
│   ├── base_parser.py               ← BaseEDIParser contract + ParsedOrder/Line dataclasses
│   ├── briscoes_idoc.py             ← PRODUCTION Briscoes path: SAP iDOC XML + ORDRSP generator
│   ├── briscoes.py                  ← EDIFACT D96A parser — deliberately NOT allow-listed
│   ├── briscoes_asn.py              ← Briscoes EDIFACT DESADV generator
│   ├── animates.py                  ← Animates ORDERS inbound parser (EDIFACT D.01B / SPS)
│   ├── animates_edifact.py          ← shared EDIFACT tokenizer + envelope/sender validation
│   ├── animates_contrl.py           ← CONTRL functional acknowledgement
│   ├── animates_ordrsp.py           ← ORDRSP order response
│   ├── animates_desadv.py           ← DESADV (ASN) with SSCC
│   ├── animates_invoic.py           ← INVOIC builder
│   └── gs1_sscc.py                  ← GS1 SSCC-18 helpers
├── services/
│   ├── edi_service.py               ← EDIService (mml.registry service 'edi'); routes outbound
│   │                                  DESADV per partner, gated by mml_edi.asn_enabled
│   └── animates_invoice.py          ← account.move → Animates INVOIC mapping (ex-GST)
├── wizards/
│   ├── edi_bulk_action.py           ← bulk approve / reject / reprocess
│   ├── edi_seed_stores.py           ← seed Briscoes store child-contacts
│   └── animates_store_master_data.py← seed Animates store master data
├── migrations/                      ← 19.0.1.0.1 / .0.2 / .0.3 (incl. dup-SO pre-check)
├── report/sscc_label_report.xml     ← SSCC carton label render
├── views/                           ← partner health dashboard, review queue, log viewer, menus
├── security/                        ← groups + ir.model.access
├── data/                            ← ir_cron, ir_sequence, mail_template, Briscoes seed (comments-only)
├── utils/credential_store.py        ← Fernet encrypt/decrypt for FTP passwords
├── tests/                           ← 482 pure-Python tests + Odoo integration tests
└── docs/                            ← PROD_CUTOVER.md, gate reviews, partner specs
```

---

## Key features

### Briscoes Group — SAP iDOC XML (the live/production path)

`parsers/briscoes_idoc.py` parses `ORDERS` + `ORDCHG` iDOC XML, matches on the **MML
internal code** (`E1EDP19 QUALF 002`, *not* the carton GTIN-14), handles carton (`CT`)
vs each (`EA`) quantities, and emits `ORDRSP` order responses with `ABGRU` reject/short
codes and fail-closed defaults — spec-true per the Briscoes iDOC IG v1.7.

### Animates (NZ) — EDIFACT D.01B via SPS Commerce (the 19.0.1.2.0 certification build)

`parsers/animates*.py` implement UN/EDIFACT D.01B over SPS Commerce: `ORDERS` inbound,
mandatory `CONTRL` functional ack per interchange, `ORDRSP`, `DESADV`+SSCC (ASN), and
`INVOIC` builders. A shared EDIFACT tokenizer validates the envelope and sender, switches
the recipient identity (`TST1ANIMATES` test vs `ANIMATES` prod), and decodes iso-8859-1
(Latin-1) wire bytes.

### Concurrency + idempotency hardening (go-live)

- **Per-partner `pg_advisory_xact_lock`** serialises concurrent polls (the lock is
  transaction-scoped, so it is re-taken after each per-file commit).
- **Partial UNIQUE index** on `sale_order (edi_trading_partner_id, company_id,
  client_order_ref)` is the DB backstop for the duplicate-SO race. The
  `19.0.1.0.3` migration pre-checks for existing live duplicates and **aborts the upgrade
  with a listing** rather than dying mid-load.
- **Strict ordering invariant**: process → write dedup marker → COMMIT per file → FTP
  rename → ACK, so partial failures retry safely.
- **ORDRSP send-claim**: a committed claim row plus a per-exchange attempt-counter
  filename mean a reset-and-reapprove re-sends without double-responding.

### Out-of-stock short-ship policy

Per-partner `oos_policy` (`backorder` / `short_ship`). `short_ship` clamps each line to
island-DC `free_qty` (fail-safe to 0), tracks the original ordered qty (`edi_ordered_qty`)
and shortfall (`edi_qty_shortfall`), and acknowledges the shortfall in the ORDRSP.
Re-clamped again at approve/ORDCHG time, with guards so it only moves qty *down* from an
operator's manual correction.

### Indent (forward-commitment) detection — newest feature

A requested delivery date beyond ~28 days (configurable via
`ir.config_parameter mml_edi.indent_threshold_days`) marks the SO `x_is_indent`, accepts
it in full (skips the OOS gate, since stock is expected absent at import), and flags it for
release-window holding.

### Operational resilience & security

- Per-partner **circuit breaker** with cooldown/half-open; rate-limited cron and per-file
  failure alerts; partner **health kanban** + guided setup checklist; append-only audit
  log; `retry_pending_acks` safety-net cron.
- Parser **class allow-list** (`_ALLOWED_PARSER_CLASSES` — blocks arbitrary class loading
  and de-lists the unsafe EDIFACT-D96A Briscoes parser), Fernet-encrypted FTP credentials,
  SFTP host-key pinning (blank key rejects all SFTP), group-restricted credential fields,
  manager-only configuration menus.
- **Per-island DC routing**: new store SOs route to the destination island's fulfilment
  warehouse via the ROQ resolver (defensive/optional dependency on `mml_roq_forecast`),
  falling back to the partner's configured warehouse.

---

## Platform integration

`mml_edi` registers with `mml_base` on install (see `hooks.py`):

| Registration | Value |
|---|---|
| Service name | `edi` — reachable via `env['mml.registry'].service('edi')` |
| Capabilities | `edi.order.process`, `edi.asn.send`, `edi.invoice.send` |

Other modules call EDI operations without a hard dependency — if `mml_edi` is not
installed, `service('edi')` returns a NullService and calls no-op safely. Outbound
DESADV/INVOIC is triggered by a 3PL despatch event (`on_3pl_despatch_confirmed`) and
routed per partner (Briscoes iDOC path vs Animates DESADV path).

---

## Partner status

| Partner | Format / transport | Status |
|---|---|---|
| **Briscoes Group** (Briscoes, Rebel Sport, Living & Giving) | SAP iDOC XML over EDIS VAN (FTP) | **Production path**, code-complete and hardened. Live prod runs `oos_policy='backorder'` + `auto_confirm_clean`; short-ship protection is coded but **not yet flipped on** (config-only, pending Briscoes sign-off on short-confirm ORDRSPs). |
| **Animates** (NZ) | EDIFACT D.01B / EANCOM over SPS Commerce | **Spec- and code-complete, NOT yet go-live**. No committed partner seed, ~66 stores unseeded, SPS transport/GLN/vendor-code provisioning is external, and the operator-UX findings for EDI remain open. Do not present as live. |

---

## Install / deploy

Install `mml_base` first, then `mml_edi`:

```bash
odoo-bin -d <db> -i mml_base --stop-after-init
odoo-bin -d <db> -i mml_edi --stop-after-init
```

Deployed under the production Odoo 19 addons path on MML's self-hosted Odoo host.

### Post-install configuration (by hand)

There is **no committed trading-partner seed** — the Briscoes seed file
(`data/edi_trading_partner_briscoes.xml`) is comments-only documenting the required
values, and there is no Animates seed. After install:

1. Create the `edi.trading.partner` record in **EDI → Configuration → Trading Partners**
   (FTP/SFTP creds, parser class, pricelist, split mode). For Briscoes the production
   values are documented in the seed-file comments; start in `environment = test` (uses
   `/Test` mailbox paths).
2. Ensure the partner's `res.partner` exists, each store is a child contact keyed by its
   store/WERKS code, and an **ex-GST** pricelist is assigned (a GST-inclusive pricelist is
   rejected — EDI prices are net).
3. Run a **manual poll** against the *test* mailbox to verify connectivity and parsing.
4. **Only at go-live** (per `docs/PROD_CUTOVER.md`): stop the legacy .NET poller **first**,
   then enable **Settings → Technical → Scheduled Actions → EDI: Poll Trading Partners**.

### Scheduled actions

| Cron | Ships | Notes |
|---|---|---|
| **EDI: Poll Trading Partners** | **INACTIVE** | Double-poll safety gate. Enable manually only after the legacy .NET poller is stopped. |
| **EDI: Retry Pending ACKs** | active | Safety-net that re-queues ORDRSPs for resolved POs with no successful send; idempotent, safe to ship active. |

Outbound ASN/DESADV/INVOIC is additionally gated **off** by
`ir.config_parameter mml_edi.asn_enabled = '0'` (default). Both the Briscoes despatch path
and the Animates DESADV path exist but do not run until the flag is set and the 3PL emits
the despatch event.

---

## Testing

482 pure-Python tests (parsers, processor logic, idempotency invariants) run with no Odoo
instance:

```bash
pytest -q          # from the module directory — 482 pass at 19.0.1.2.0
```

Odoo integration tests (require an Odoo runtime):

```bash
odoo-bin --test-enable --stop-after-init -d testdb \
  -i mml_base,mml_edi --test-tags=mml_edi
```

---

## Gotchas / notes worth knowing

- **Gate-review docs are historical.** `docs/2026-07-02-golive-gate-review.md` is a
  snapshot *before* the fix commits — do not read its NO-GO/NOT-READY verdicts as current.
  `docs/2026-07-02-blocker-fixes-review.md` and the later commits
  (`537042d` / `3ea57f5` / `aaddd68` / `7f47bcc`) supersede it. Verify against HEAD.
- **`CLAUDE.md` (sibling) is stale** — it describes `briscoes_idoc` as a stub raising
  `NotImplementedError`. That is false: `briscoes_idoc.BriscoesIDOCParser` is the
  production parser. Likewise the manifest `description` still reads "Phase 1 stub".
- **The EDIFACT D96A Briscoes parser is deliberately excluded** from
  `_ALLOWED_PARSER_CLASSES` (`parsers/briscoes.py` ships carton qty as eaches). Only
  `briscoes_idoc.BriscoesIDOCParser` and `animates.AnimatesParser` are allow-listed —
  adding a parser means editing that frozenset.
- **Odoo 19 removed `Registry.in_test_mode()`.** The module uses a bespoke
  `_commit_suppressed(env)` helper (registry hook else `config['test_enable']`,
  fail-toward-suppress) at both commit sites. Never reintroduce a direct `in_test_mode()`
  call — it silently killed every ACK send.
- **Plaintext FTP to EDIS VAN** (`post.edis.co.nz:21`) is a VAN constraint, not a config
  slip.
- **Separate git repo.** This is `JonaldM/mml.edi.odoo`, a standalone module repo
  distinct from MML's other Odoo modules and infrastructure tooling.

---

## Dependencies

- **`mml_base`** — MML platform layer (`mml.registry` service bus, `mml.capability`,
  `mml.event` billing events).
- **Odoo core** — `sale` (sale.order/line), `account` (account.move → Animates INVOIC),
  `stock` (picking/move/location, `free_qty`), `mail` (mail.thread on reviews, alerts).
- **Python** — `cryptography` (Fernet credential encryption); `paramiko`/`ftplib`
  (SFTP/FTP transport).
- **Optional/soft** — `mml_roq_forecast` (per-island DC resolver), called defensively; not
  a hard depend.

## Related MML repos

Part of the MML Consumer Products Odoo ERP ecosystem: `mml_base` (platform), and the 3PL /
freight / ROQ forecast modules (the ROQ resolver here consumes `mml_roq_forecast`). It runs on
MML's self-hosted, high-availability Odoo 19 platform.
