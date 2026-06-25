# Animates EDI — AUTONOMOUS BUILD RUNBOOK

> Build tracker + build-critical detail. If context resets, READ THIS, then continue from "Progress".
> Companion spec: `specs/2026-06-26-animates-edi-integration-plan.md`. Source MIGs: `mml_edi/docs/animates/`.

## Autonomy contract
User: "read and plan a sprint for integration with existing edi app, basically a translation layer. use octo and go fully autonomous." → Plan (done, grounded by 9-agent workflow) + build B1..B9 TDD autonomously. Don't ask the user. Build the CODE against the verbatim-MIG golden fixtures (self-contained); cert/go-live is gated on external IDs (R1-R12) which get `ir.config_parameter` placeholders. Octo review the plan first (personas; droids are broken in this env). Commit on branch `feat/animates-edi`; never to master directly.

## Key facts
- Module: `mml_edi` (repo JonaldM/mml.edi.odoo, branch master clean). Build branch: `feat/animates-edi`.
- 5 messages, EANCOM2002/UN-EDIFACT **D.01B**, over SPS Commerce VAN. UNOC:3, UNA always present (honour `?` release char), NO UNG/UNE, numeric interchange control ref.
- Parser interface `parsers/base_parser.py`: only `parse_file` + `generate_ack` are abstract. Outbound beyond ORDRSP needs the §A dispatch extension.
- Allowlist (single mandatory edit to existing code): `models/edi_trading_partner.py:18-27` `_ALLOWED_PARSER_CLASSES`.
- Inbound flow: `ir.cron → EDIProcessor.run_scheduled_poll → poll_trading_partner → EDIFTPHandler → _process_file → parser.parse_file() → ParsedOrder → process_parsed_order → sale.order + edi.order.review`.
- ORDRSP path: `EDIOrderReview._queue_ack()` (`edi_order_review.py:288-365`) → `parser.generate_ack(review)`.
- ASN hardcoded today: `edi_service.py:131` `BriscoesASNGenerator` → must be generalised to partner-dispatched.
- Product cascade `_find_product` (`edi_processor.py:771-830`): barcode→default_code→vendor_code→supplier_sku. ISC (buyer code) is NOT a strategy yet → add it.

## §A — Outbound-dispatch extension (the base/service change Briscoes doesn't cover)
Add a thin partner-dispatched outbound layer modelled on `EDIService` (`services/edi_service.py`):
- Generalise `_generate_and_upload_asn` so the ASN builder resolves from the partner (`partner.get_asn_builder()` / `build_message(msg_type, payload)`), not hardcoded `BriscoesASNGenerator`.
- Add `EDIService.on_invoice_posted` (account.move post hook) → INVOIC builder → upload.
- Auto-emit CONTRL inside the inbound path after successful interchange parse (UCI action 8), idempotent via `edi.log`; + inbound CONTRL consumer correlating Animates' acks to sent interchanges, alert on missing/≠8.
- `generate_ack` stays the ORDRSP entry (keeps `_queue_ack` idempotency).

## Per-message build detail
- **ORDERS (in):** BGM 1225 9/5/1 → new/change/cancel; DTM 137/2; NAD BY/SU/ST; LIN/PIA(IN=ISC + SA=MML code)/IMD/QTY(21 ordered, 59 pack)/MOA/PRI/TAX; CNT validation. Map ISC→buyer_article_no, SA→vendor_code; qty in EA. Cancel(+1)→order no lines; replace(+5)→change_order.
- **ORDRSP (out):** BGM 231 (1225 29/27/4); ALL PO lines echoed; LIN 1229 ∈ {5 accepted,7 rejected,3 changed}; QTY 21+59+113(committed; 0 on reject); FTX+LIN reason mandatory when 1229=7; PRI AAA 4dp; TAX 7/GST; BGM 1004=ORDRSP seq; RFF ON=PO. BGM1225↔LIN1229 coupling (full-accept→all 5).
- **DESADV (out):** hierarchy CPS 1E shipment + PAC totals → per-unit CPS 3 → PAC(09/CT) → PCI 33E → GIN AW+SSCC → [pallet: inner PAC carton count] → LIN/PIA(IN+SA)/QTY 12. RFF ON(PO)+CN(connote); ALI 165/164 on splits; QVR (AC/BP/CP) + DTM 17 ETA on variance. Fixtures: p.57 (pallet, UNT=39,CNT=3), p.58 (split, ALI165+QVR200:66+BP+DTM17, UNT=27,CNT=1).
- **INVOIC (out):** BGM 388:::TAX INVOICE; DTM 137 (not future); RFF ON+(AAK|CN); NAD BY/SU/ST + RFF AMT (NZBN/ABN); CUX 2:NZD:4; LIN/PIA(IN+SA)/IMD/QTY(47+59); **line MOA 128/369/203 + PRI = 4dp; summary MOA 128/369/39 + TAX rate = 2dp**; CNT; SG50 totals. Fixture p.64-65 UNT=32,CNT=1.
- **CONTRL (both):** build UNA→UNB(→ANIMATES/TST1ANIMATES)→UNH(CONTRL:D:3:UN:EAN004)→UCI(orig ctrl-ref, inverted parties, action 8)→UNT(3)→UNZ. Parse: validate, read UCI 0020 to correlate, ≠8/missing → alert. Fixture p.8.
- **Envelope invariants (validate read+write):** UNB0020==UNZ0020; UNH0062==UNT0062; UNT0074=segcount incl UNH/UNT; UNZ0036=msg count; CNT 2==LIN count.

## Store master seed (B7)
xlsx `Animates-Clinic-Store-Master-File-12112025.xlsx`, sheet "Clinic _ Store Master File", header at ROW 3: `Site Name | Address | Store Email | Manager Phone | Store Code`. 66 data rows + region divider rows + trailing blanks. → res.partner delivery children of Animates (clone `wizards/edi_seed_stores.py`, tuple (ref,name,email,phone,address,region,is_vet)). CRITICAL: 10 vet clinics SHARE retail codes (08,25,11,13,33,09,18,29,03,05) → namespace clinic ref as `<code>-V` (idempotency key (parent_id, ref) else silently drops clinics). Strip BOM/﻿ in addresses. No GLN/active in file → GLN null, active True.

## External blockers (config placeholders during build; needed for cert) 
R1 transport+endpoints+creds (if AS2/API, EDIFTPHandler gap) · R2 MML supplier GLN · R3 Animates supplier code V#### · R4 GS1 company prefix (SSCC) · R5 ISC↔SKU map · R6 NZBN/ABN · R7 store GLNs · R8 SSCC label-print scope · R9 numbering schemes · R10 doc artifacts (emit IN+SA both; CTA SU) · R11 go-live timeline+cert plan · R12 re-cert trigger. Store as `ir.config_parameter mml_edi.animates.*` placeholders.

## Reference files
base_parser.py · parsers/briscoes.py + briscoes_asn.py + briscoes_idoc.py (pattern, D96A — NOT reusable code, structure only) · services/edi_service.py:131 (ASN to generalise) · models/edi_order_review.py:288-365 (_queue_ack) · edi_processor.py:771-830 (_find_product cascade) · wizards/edi_seed_stores.py (seed pattern) · data/edi_trading_partner_briscoes.xml · tests/conftest.py + common.py + fixtures/loader.py (TDD harness).

## Progress
- [x] Grounding workflow (9 agents) → sprint plan
- [x] Spec + runbook committed
- [ ] Octo review of plan (personas) — NEXT
- [ ] B1 envelope + partner config + fixtures + allowlist
- [ ] B2 ORDERS inbound parser (+ ISC match)
- [ ] B3 ORDRSP · [ ] B4 DESADV+SSCC · [ ] B5 INVOIC · [ ] B6 CONTRL
- [ ] B7 store seed · [ ] B8 sequencing/compliance gates · [ ] B9 integration tests
- [ ] Local pytest green (baseline pre-existing failures) → branch ready for review/cert
