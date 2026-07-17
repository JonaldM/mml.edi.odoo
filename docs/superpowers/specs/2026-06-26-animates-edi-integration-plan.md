# Animates EDI Integration — Sprint Plan (translation layer in mml_edi)

- **Date:** 2026-06-26
- **Status:** Planned — autonomous build in progress
- **Repo:** `JonaldM/mml.edi.odoo` (module `mml_edi`)
- **Source docs:** `mml_edi/docs/animates/` (5 SPS Commerce MIGs + Supplier Compliance Manual + Order-Fulfilment Workflow + SSCC label spec + store master xlsx)
- **Grounding:** synthesized from a 9-agent workflow that read all MIGs in full + mapped the mml_edi architecture (see git history / runbook).

## 1. Summary
Add **Animates (NZ)** as an EDI trading partner exchanging **5 EANCOM 2002 / UN-EDIFACT D.01B** messages over the **SPS Commerce** VAN (MML never connects to Animates directly). Inbound ORDERS → Odoo `sale.order` reuses the existing parser/review engine; the new surface is **genuine D01B generation for 4 outbound messages** + a partner-dispatched outbound layer + SSCC mint/register.

### Message flow (strict sequence ORDRSP → DESADV → INVOIC)
| # | Message | Dir | EDIFACT id | Trigger / SLA |
|---|---|---|---|---|
| 1 | ORDERS | Inbound | `ORDERS:D:01B:UN:EAN011` | PO → sale.order |
| 1a | CONTRL | Outbound | `CONTRL:D:3:UN:EAN004` | auto-ack every inbound interchange (UCI action 8) |
| 2 | ORDRSP | Outbound | `ORDRSP:D:01B:UN:EAN009` | ≤48h after PO, before despatch |
| 3 | DESADV | Outbound | `DESADV:D:01B:UN:EAN008` | after ORDRSP, before goods arrive; SSCC pack hierarchy |
| 4 | INVOIC | Outbound | `INVOIC:D:01B:UN:EAN011` | after DESADV; one invoice per PO+delivery |
| - | CONTRL | Inbound | same | Animates acks each MML send; track + alert on missing |

### Integration into mml_edi
- **Inbound** reuses the engine unchanged: `ir.cron → EDIProcessor → EDIFTPHandler → parser.parse_file() → ParsedOrder → process_parsed_order → sale.order + edi.order.review`. Dispatch via `partner.get_parser_instance()` (`edi_trading_partner.py:439-459`).
- **ORDRSP** reuses `EDIOrderReview._queue_ack()` → `parser.generate_ack(review)` (`edi_order_review.py:288-365`).
- **DESADV/INVOIC/CONTRL have no parser-dispatched hook** — `generate_ack` is the only outbound method, ASN is hardcoded to `BriscoesASNGenerator` (`edi_service.py:131`). This sprint adds a thin partner-dispatched outbound layer (§2.6 in the runbook) rather than overloading `generate_ack`.
- **Single mandatory edit to existing code:** add `AnimatesParser` to `_ALLOWED_PARSER_CLASSES` (`edi_trading_partner.py:18-27`). `edifact_d01b` is already a valid `edi_format`.

## 2. New files (mirror Briscoes layout)
```
parsers/animates.py            AnimatesParser: parse_file (ORDERS→ParsedOrder) + generate_ack (ORDRSP)
parsers/animates_edifact.py    D01B helpers: UNA-aware tokenizer, ?-release escape, composites,
                               UNB/UNH/UNT/UNZ envelope, numeric ctrl-ref allocator, count/CNT validators
parsers/animates_ordrsp.py     ORDRSP builder (BGM 231; LIN 1229 5/7/3; QTY 21/113/59)
parsers/animates_desadv.py     DESADV builder: CPS/PAC/PCI/GIN hierarchy + SSCC
parsers/animates_invoic.py     INVOIC builder (BGM 388; 4dp line / 2dp summary)
parsers/animates_contrl.py     CONTRL parse (inbound ack) + build (UCI+8)
models/edi_sscc_register.py    edi.sscc.register — SSCC-18 mint + 12-month no-reuse
models/edi_outbound_dispatch.py partner-dispatched outbound routing (extends EDIService)
data/edi_trading_partner_animates.xml ; data/ir_sequence_animates.xml
wizards/edi_seed_animates_stores.py (+views)  store-master seed (66 sites; dup-code namespacing)
tests/test_animates_*.py + tests/fixtures/animates_*.edi (verbatim MIG worked examples = golden files)
```

## 3. Key decisions / identifiers
- **EDIFACT:** D.01B, UNOC:3, UNA always present (honour `?` release), no UNG/UNE. Numeric interchange control reference. Test vs prod routed by recipient address (`TST1ANIMATES` vs `ANIMATES`), driven by `partner.environment`.
- **Parties:** NAD BY=`ANIMATES`, SU=MML's Animates-assigned supplier code (e.g. V####), ST=Animates store code (`res.partner` child `ref=store_code`). Envelope GLNs (UNB qualifier 14) for MML's GLN.
- **Item key:** Animates **ISC** is the canonical product key (`PIA+5+<isc>:IN` on every ORDRSP/INVOIC line); MML code rides in `PIA+1+<code>:SA`. No GTIN in ORDERS/ORDRSP/INVOIC. **Decision:** add a dedicated ISC product field + cascade match strategy (ISC round-trips exactly).
- **Order split:** `single` (one ORDERS = one PO = one store, DSD), unlike Briscoes per-store.
- **Quantities in eaches** (avoid the Briscoes CT-as-EA bug).
- **Decimal precision (INVOIC build-critical):** line MOA/PRI 4dp, summary MOA + TAX rate 2dp.
- **SSCC:** GS1 SSCC-18, 12-month no-reuse register.

## 4. Phased build (B1..B9) — TDD, golden-file fixtures from verbatim MIG samples
- **B1** shared D01B envelope helpers + partner config + sequences + allowlist edit.
- **B2** ORDERS inbound parser (→ParsedOrder; ISC match strategy).
- **B3** ORDRSP outbound (via `generate_ack`).
- **B4** DESADV + SSCC register (+ generalise the hardcoded ASN dispatch).
- **B5** INVOIC outbound (+ `EDIService.on_invoice_posted`).
- **B6** CONTRL both directions (auto-emit + ack tracking).
- **B7** store-master seed wizard (66 sites; clinic codes namespaced `-V` to break 10 shared codes).
- **B8** outbound sequencing + compliance gates (ORDRSP→DESADV→INVOIC ordering, uniqueness, CONTRL-receipt alerts).
- **B9** Odoo integration tests + SPS cert harness.

Each phase: write `test_*` against verbatim MIG `.edi` fixtures (RED) → implement (GREEN) → wire config. Pure-Python tests run via `pytest -m "not odoo_integration"`; baseline against known pre-existing failures before claiming regressions.

## 5. External blockers (needed for CERT/GO-LIVE, not for build/test)
Transport protocol+endpoints+creds (SPS-issued; if AS2/API, EDIFTPHandler is a gap) · MML supplier GLN · Animates-assigned supplier code (V####) · GS1 company prefix for SSCC · ISC↔SKU master map · NZBN/ABN (INVOIC) · store GLNs · SSCC label-print scope · numbering schemes · go-live timeline + SPS cert test plan. These get config placeholders (`ir.config_parameter`) during the build.

## 6. Test + cert strategy
Golden-file fixtures = the 6 verbatim MIG worked examples (ORDERS p.50, ORDRSP p.50, DESADV p.57+p.58, INVOIC p.64-65, CONTRL p.8) — byte-level segment equivalence asserts envelope/composites/precision/`?`-release/control counts. Odoo TransactionCase end-to-end for the full cycle. SPS phases: enablement → test (`TST1ANIMATES`) → certification → cutover (flip `environment=production`) → re-cert on major changes.

> Full architecture detail, the §2.6 outbound-dispatch design, the per-phase file/acceptance breakdown, and the 12-row risk table live in the companion runbook `../2026-06-26-animates-edi-RUNBOOK.md`.
