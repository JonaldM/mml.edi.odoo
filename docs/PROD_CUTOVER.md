# mml_edi — Production Cutover Runbook (Odoo 15 → 19)

Cutover of the **Briscoes EDI integration** to the native `mml_edi` Odoo 19 module,
in step with the MML_Production 15 → 19 upgrade. This replaces the legacy .NET
Windows service (`BriscoesEditOrder` on the .5 box).

**Format:** Briscoes sends/expects **SAP iDOC XML** (ORDERS05 / ORDERSEXT in,
ORDRSP out) over the EDIS VAN. Parser = `BriscoesIDOCParser`. (EDIFACT D96A is
also implemented but unused unless Briscoes switches MML.)

Validated (7 Jun 2026) on `MML_19_prod_test`: single-store, multi-store
(47 stores / 173 lines), modification (ORDCHG) and cancellation all process
cleanly; products match 6/6; per-PO ORDRSP generated with POSEX preserved.

---

## 0. Decisions to confirm BEFORE cutover (owner/ops)

1. **Deployment target** — where does MML_Production (v19) run, and where is
   `mml_edi` installed + polling? On-prem `.35` or HIAV. This drives #2.
2. **EDIS FTP egress allow-list** — if polling runs from **HIAV**, EDIS must
   allow-list HIAV's egress IP (`46.62.148.99`). From `.35` (on the existing
   MML public IP) it is already allowed. **Confirm with EDIS.**
3. **Single poller (double-poll guard)** — only ONE process may poll the live
   `/FromEDIS` mailbox. The legacy `.5` .NET app MUST be stopped at go-live, or
   both will download + process the same POs.
4. **`auto_confirm_clean`** — True = clean orders confirm + ACK automatically;
   False = every order waits in the review dashboard. Recommend **True** for the
   ~99% clean flow, with `alert_on_issues=True` for the rest.

---

## 1. Get the code onto prod

```
# merge the PR to master first:  claude-sprint/edi-idoc-parser  ->  master
# on the prod box (the Odoo addons checkout):
cd <addons>/mml_edi-repo
git checkout master
git pull            # (SSH deploy key is configured on HIAV)
```
Ensure the repo is on the Odoo `addons_path` and `mml_base` is present (declared
dependency).

## 2. Install the module on MML_Production (v19)

```
odoo -d MML_Production -i mml_edi --stop-after-init        # first install
# (later code updates:  -u mml_edi)
```
The install creates the EDI models, views, security groups (`group_edi_user`,
`group_edi_manager`), sequences, cron (disabled by default until configured),
and mail templates. It does NOT auto-create the Briscoes partner (next step).

## 3. Configure the Briscoes trading partner (EDI ▸ Trading Partners ▸ New)

| Field | Value |
|---|---|
| name / code | Briscoes Group / `BRISCOES` |
| partner_id | the Briscoes Group `res.partner` (customer) |
| edi_format | **SAP iDOC XML (ORDERSEXT)** (`idoc_xml`) |
| parser_class | `mml_edi.parsers.briscoes_idoc.BriscoesIDOCParser` |
| product_match_field | **Internal Reference (`default_code`)** |
| order_split_mode | Per Store |
| client_ref_template | `{po_number}_{store_code}` |
| ftp_host / port / protocol | post.edis.co.nz / 21 / ftp |
| ftp_user / ftp_password | EDIS VAN creds (stored Fernet-encrypted) |
| inbox/outbox | `/FromEDIS` `/ToEDIS` (+ `/Test/FromEDIS` `/Test/ToEDIS`) |
| environment | **test** (until step 4 passes), then production |
| pricelist_id | an **ex-GST** Briscoes pricelist |
| auto_confirm_clean / alert_on_issues | per decision #4 / True |
| alert_email_ids | EDI ops recipients |

**Data prerequisites in MML_Production** (verify — they migrate from 15):
- each Briscoes store is a child contact of the Briscoes partner with
  `ref` = the store/WERKS code (e.g. 1050); the engine resolves delivery by it.
- the assigned pricelist is GST-exclusive (a GST-inclusive one is rejected by an
  `@api.constrains` — EDI prices are net).
- products carry their MML code in `default_code` (the iDOC E1EDP19 QUALF 002 key;
  case-insensitive match). NB the iDOC QUALF 003 GTIN is the CARTON barcode, not
  Odoo's retail `barcode`, so barcode matching will NOT work — use default_code.

## 4. Pre-go-live validation (on the /Test mailbox)

With `environment = test`:
1. **Test FTP connection** (button on the partner form).
2. Drop a real Briscoes PO into `/Test/FromEDIS` (or use **Run Poll Now**).
3. Verify: SO(s) created (one per store), correct lines/qty (each, not cartons)
   /price/dates, review state, `edi.log` entries.
4. Approve (or auto-approve) → confirm **one ORDRSP** lands in `/Test/ToEDIS`.
5. **Confirm EDIS accepts the ORDRSP** (no EDIStech rejection email — this is the
   POSEX handshake that caused the original chaser; it's the one thing only a live
   exchange proves).

## 5. Go-live

1. **Stop the .5 .NET poller** (double-poll guard) — confirm it will not run.
2. Partner `environment` → **production**.
3. Enable the EDI poll cron (Settings ▸ Technical ▸ Scheduled Actions) at the
   chosen interval.
4. Watch the **EDI ▸ Review** dashboard + `edi.log` for the first live cycle.

## 6. Rollback

- Set the Briscoes partner `active = False` (or disable the cron) — stops polling.
- Re-start the `.5` .NET app to resume the legacy flow.
- No data migration needed; SOs already created stay. Re-enable when ready.

## Smoke-test checklist (post go-live)
- [ ] Single-store ZNR PO → 1 SO, lines in EACH qty, ORDRSP accepted by EDIS
- [ ] Multi-store ZNB PO → N SOs (one per store) → **one** per-PO ORDRSP
- [ ] ORDCHG (change) → change review with correct diff; approve applies it
- [ ] ORDCHG (all lines ACTION 003 = cancel) → review shows all lines removed
- [ ] Duplicate file / re-sent PO → skipped (dedup), no duplicate SO or ACK

## Known notes
- **One ORDRSP per PO**: the ACK is deferred until every store-review of a PO is
  resolved, then uploaded once (idempotent). Matches Briscoes' per-PO ORDRSP.
- **Integration tests**: the 55 `odoo_integration` tests pass in a clean CI DB;
  on a prod-clone they can error on unrelated env (stock replenishment rules for a
  dummy test product, `project_todo` welcome-task) — those are test-fixture/env
  issues, not module defects. The 98 pure-Python unit tests are the gate.
- **Secrets**: FTP password is Fernet-encrypted at rest; consider moving to
  `ir.config_parameter` (`mml_edi.{code}.ftp_password`) for multi-tenant.
