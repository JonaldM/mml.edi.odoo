# BRISCOES EDI — HARDEN & DECOMMISSION PLAN

> Status: 2026-06-29 · MML Odoo 19 · Röhlig 3PL go-live · `mml_edi` is live, `.NET BriscoesEditOrder` effectively retired
> Owners: Dev (workstreams A–C, F) · Ops (workstreams D, E)

---

## 1. Executive summary

The native Odoo `mml_edi` module is the **sole** Briscoes order processor in production (86/86 EDI SOs today carry `edi_review_id`; the legacy .NET log is dead since 2026-03-02). Inbound parity — iDOC parsing, multi/single-store split, CT→EA, MML-code match, per-PO ORDRSP, ORDCHG recognition — is **achieved and running**. The North Island DC routing default was fixed earlier today. The one **core code gap** is the OOS policy: today's pipeline accepts orders **in full and backorders**, which over-promised against deprecated-Auckland phantom stock and produced ~46 partial-reserved orders at Röhlig. The build replaces this with **DC-scoped short-ship + shortfall ACK** to Briscoes, fences Auckland out of availability, and formally decommissions the .NET so it can never resume double-processing.

---

## 2. Current state (verified)

| Area | Verified fact |
|---|---|
| Order processor | `mml_edi` only. 86 EDI SOs in last 30h, all carry `edi_review_id`; zero non-EDI Briscoes SOs. |
| Live input format | **SAP iDOC XML** (`PO_Briscoes_MML_*.xml`), parsed by allowlisted `BriscoesIDOCParser`. EDIFACT `BriscoesParser` is deliberately NOT allowlisted (dormant). |
| .NET state | Idle, **not decommissioned**. Binary, FTP credential, SQL audit config all intact. Auto-restart on reboot + manual "specific files" mode are live resume vectors. |
| DC routing | All 86 today route to North Island DC. NI DC `sequence=1`, deprecated Auckland `sequence=99`. Briscoe Group partner: `warehouse_id=NI DC`, `auto_confirm_clean=True`. |
| OOS handling | Accept-in-full + backorder + heads-up email. Stock check is a **non-blocking warning** reading company-wide-ish `qty_available` with a `{}` fallback (the Auckland-phantom leak path). |
| Auckland phantom | Physically empty, stock still on the books (e.g. VCCA4: 12 @ Auckland, 0 @ NI/Röhlig). User directive: **fence off, keep tracked, do not write off.** |
| SI DC | Archived; `roq.south_dc_live=False` → all South demand rewrites to North. `_resolve_island_dc` filters `active=True`, so an archived SI DC is currently unreachable. |

---

## 3. Workstreams

### Workstream A — OOS short-ship + ACK policy (CORE BUILD)

**Goal:** stop backordering. Short-ship the DC-available qty to Röhlig and ACK the shortfall to Briscoes in the ORDRSP.

**Why it's surgical:** Briscoes' own `Short_Supplied` ORDRSP sample carries a *single* `QTY+11 = confirmed/shippable qty` (not ordered, not the shortfall) on a `LIN…+3` (qty-changed) line, message purpose `4`. The existing `_generate_ordrsp` already emits exactly this once `product_uom_qty` holds the short qty. The only real change is upstream: reduce the SO line to available before confirm.

**Steps:**
1. **Persist ordered qty.** Add `edi_ordered_qty` (Float, `digits="Product Unit of Measure"`) to `SaleOrderLine` so `product_uom_qty` can hold the short-shipped qty while audit/ACK keep the original. Keep `edi_qty_shortfall = ordered − shipped`.
2. **Rewrite `_process_order_line`** (~522–565): compute availability **before** line create → `ordered = parsed_line.quantity`; `ship_qty = min(ordered, max(available, 0))`; create line with `product_uom_qty = ship_qty`, set `edi_ordered_qty = ordered`, `edi_qty_shortfall = shortfall`. Keep the `qty_shortfall` issue as **non-blocking warning** (must not force manual review — auto-confirm directive), reworded to "ordered X, shipping Y, short Z".
3. **New helper `_dc_available_qty(product, so)`** — single home for the DC-scoping rule; returns `0.0` if `so.warehouse_id` is falsy (never company-wide). Delegates to Workstream B's `_fulfilment_free_qty`, guarded by `hasattr` so `mml_edi` degrades to native `qty_available` if `mml_roq_forecast` absent.
4. **ORDRSP zero-ship guard** in `briscoes.py:_generate_ordrsp` (~356–409): a fully-OOS line (`ship_qty=0`) emits **action `7` (rejected)**, not action `3` with `QTY+11:0.000`. Purpose/action logic otherwise unchanged (`any(edi_qty_shortfall>0)` → purpose `4`). **Do NOT** add a second QTY qualifier — not in Briscoes' spec.
5. **Copy-only** edit to `_send_oos_summary` (~1001–1062): "accepted in full / will backorder" → "short-shipped available qty / shortfall acknowledged in ORDRSP".
6. **Migration** `19.0.1.0.x/pre-migration.py`: backfill `edi_ordered_qty = product_uom_qty` for existing EDI lines (historical lines were accept-in-full, so ordered == current qty — safe).

**Files:** `mml_edi/models/sale_order.py`, `mml_edi/models/edi_processor.py`, `mml_edi/parsers/briscoes.py`, `mml_edi/models/edi_trading_partner.py` (optional `oos_policy` flag — decision 5.1), `mml_edi/migrations/19.0.1.0.x/pre-migration.py`.

**Tests (pure-Python first):**
- `tests/test_short_ship.py`: `_process_order_line` sets `product_uom_qty = min(ordered, available)`, `edi_ordered_qty = ordered`, `edi_qty_shortfall = ordered − ship`; phantom company-wide stock excluded (availability mocked per-warehouse).
- `tests/test_briscoes_ordrsp.py`: short line → `QTY+11 = ship_qty`, action `3`, purpose `4`; zero-ship line → action `7`; full line → action `5`, purpose `29`.
- Idempotency: second `process_parsed_order`/re-poll is a no-op (asserts no double-reduce).
- **Odoo integration:** auto-confirm path confirms the *short* order, reserves exactly what exists, Röhlig sees a fully-reservable order (no partial-reserve); per-PO ORDRSP fires once.

---

### Workstream B — DC-specific fulfilment availability + Auckland fence

**Goal:** availability is read from the order's resolved DC only (NI + live SI), never company-wide. **The fence = the `roq_island` tag.** Auckland carries no tag → uncountable for availability, but stays valued/tracked (no write-off).

**Steps (in `mml_roq_forecast`, which owns `roq_island` / `_resolve_island_dc`):**
1. **`StockWarehouse._fulfilment_dcs(island=None)`** — `search([('roq_island','!=',False)], active_test=False)` (+ island filter). `active_test=False` deliberately includes the **archived** SI DC; Auckland (no tag) is never in the set. This single method **is** the fence.
2. **`product.product._fulfilment_free_qty(island=None)`** — sum `free_qty` per `wh.lot_stock_id` via `location` context (never `warehouse` company-wide), across `_fulfilment_dcs`. `free_qty` = on-hand − reserved, so already-reserved stock isn't double-promised (this is what prevents the Röhlig over-promise; `qty_available` ignores reservations).
3. **Wire into Workstream A** — `_dc_available_qty` calls `product._fulfilment_free_qty(island=<routed DC island>)` (scope to the routed island so North orders never promise South stock). Remove the current `wh_ctx = {}` fallback (the leak path).

**Files:** `mml.roq.model/mml_roq_forecast/models/stock_warehouse_ext.py`, new `mml_roq_forecast/models/product_fulfilment_ext.py`.

**Tests:**
- Pure-Python: `_fulfilment_dcs` excludes a no-tag warehouse (Auckland) and includes an archived `roq_island='south'` warehouse; `_fulfilment_free_qty` sums per-location `free_qty` and nets reservations.
- Odoo integration: 12 phantom units at untagged Auckland → `_fulfilment_free_qty` returns 0 for that SKU at NI; a reserved qty reduces the next promise in the same run.

---

### Workstream C — SI DC routing futureproofing

**Goal:** make the archived South Island DC representable, and guarantee the routing fallback is **never Auckland**.

**Steps:**
1. **Never-Auckland floor** in `_resolve_island_dc` (`stock_warehouse_ext.py:76–94`): after the `active=True` island search, if empty, pin to the active NI DC (`roq_island='north', active=True`) instead of returning an empty recordset (which lets the caller fall to Auckland).
2. **Tighten EDI fallback** (`edi_processor.py:405–415`): trust `_resolve_island_dc` as authoritative; only use `partner.warehouse_id` if it itself carries a `roq_island` tag; never fall to the EDI user's default (= Auckland).
3. **Seed data:** exactly one warehouse tagged `roq_island='north'` (NI DC); tag the archived SI DC `roq_island='south'` so it's reachable when `roq.south_dc_live` flips. Auckland must carry **no** tag.
4. **Leave `roq.south_dc_live=False`** until SI go-live (decision 5.4). When flipped, `_resolve_island_dc` must find the (now active) SI DC — confirm un-archive + activation are part of that go-live runbook.

**Files:** `mml_roq_forecast/models/stock_warehouse_ext.py`, `mml_roq_forecast/models/sale_order_ext.py` (onchange parity), `mml_roq_forecast/data/ir_config_parameter_data.xml`, warehouse seed/data.

**Tests:** island `None`/south-no-SI-DC → floor returns NI DC, never Auckland; EDI fallback never resolves an untagged warehouse; SI DC reachable once active + `south_dc_live=True`.

---

### Workstream D — .NET formal decommission + duplicate-processing guard

**Goal:** the two pollers share the `/FromEDIS` mailbox with **no shared lock** — safety today rests solely on "only one process is consuming." Make that an enforced guarantee.

**Confirm (read-only, on the `.5` box):**
1. `sc.exe query BriscoesEditOrder` — record `STATE` + `START_TYPE`.
2. `Get-ScheduledTask | ? {$_.TaskName -like '*Briscoe*'}` — check for a Task Scheduler wrapper.
3. Confirm `BriscoesEditOrder.log` and SQL `10.0.0.6/BriscoesEDI.Log` have no entries after go-live.
4. In Odoo: confirm `cron_edi_poll` is **active** (seed default is `active=False`), `edi.log` shows `file_download/success` rows, and Briscoe Group partner `active=True, environment=production`. Assert exactly one Odoo node runs the cron (HA nbg1/hel1).

**Enforce (stop resume):**
5. **Stop + disable service:** `Stop-Service BriscoesEditOrder; Set-Service -StartupType Disabled` (blocks reboot auto-start). Disable any Scheduled Task.
6. **Neuter manual mode:** rename `BriscoesEditOrder.exe` → `…exe.DECOMMISSIONED` (keep binaries for rollback).
7. **Credential / folder separation (the only *hard* stop):** have EDIS **rotate the FTP password**, give the new one only to `mml_edi` (Fernet-encrypted on `edi.trading.partner`); the .NET's `.exe.config` then can't log in even if started. Optionally point `mml_edi` at a distinct inbox path. Confirm Odoo's egress IP is allow-listed (HIAV `46.62.148.99` if polling from there).
8. **Capture `.exe.config`** from the server (not in repo) and reconcile FTP paths + any SQL-audit consumer before flipping. Rotate the SQL `edi` account password (`log4net.config` leaks `edi/ediPassword2021`).
9. Decommission SQL audit DB only **after** the rollback window.

**Duplicate-processing risks:**

| # | Risk | Mitigation |
|---|---|---|
| R1 | .NET auto-restarts on reboot, races for files | Disable startup (D5) + rotate credential (D7) |
| R3 | .NET wins race → deletes file → **PO silently never reaches Odoo** | Credential rotation (D7) — only hard stop |
| R4 | Both process same PO → **duplicate/conflicting ORDRSP to Briscoes** | Single-poller (D4) + credential rotation (D7) |
| R5 | No cross-process dedup (hash store is Odoo-only) | Structural — enforce single-consumer, don't rely on cross-system dedup |
| R6 | Two Odoo nodes both poll | Covered by SHA-256 `edi.log` + `client_order_ref` dedup; still pin cron to one node |

**Files (reference, not edited):** `mml_edi/models/edi_processor.py`, `edi_ftp.py`, `edi_log.py`, `BriscoeEDI/.../BriscoesEditOrder.log`, `log4net.config`, `docs/PROD_CUTOVER.md`.

---

### Workstream E — Remediate the ~46 Auckland-routed partial orders already at Röhlig

These (≈MML47936–47982) routed to deprecated Auckland **before** today's fix and partial-reserved at Röhlig against phantom stock. They predate the Workstream A code, so they won't self-heal.

**Decision required (5.5) — recommended path:**
1. **Recompute availability** for each affected SO against the **NI DC** (`_fulfilment_free_qty`), determine the true short per line.
2. **Re-ACK the shortfall to Briscoes** — regenerate a corrected ORDRSP (purpose `4`, short lines action `3`/zero-ship action `7`) so the customer's expected receipt matches what Röhlig will actually ship. This is the customer-facing correctness step.
3. **Align Röhlig:** short-ship at Röhlig to the corrected qty (cancel the un-fulfillable backorder remainder) rather than leaving an indefinite partial-reserve.
4. **Do not write off** Auckland phantom — it's fenced (Workstream B), stays valued/tracked.

**Open sub-questions for ops/Briscoes:** does Briscoes accept a *corrected/re-issued* ORDRSP for an already-ACKed PO, or do these need a manual exception process? Confirm before mass re-ACK. If re-ACK is not acceptable, fall back to: leave to Röhlig partial-fulfil + manual short-close, no second ORDRSP.

---

### Workstream F — Fix the stale `mml_edi/CLAUDE.md`

Correct the stale claims so future work isn't misled:
- ".NET still live / cutover pending" → **.NET effectively retired; `mml_edi` is sole processor** (pending formal decommission, Workstream D).
- "EDIFACT real parsing (Phase 2)" / manifest description → **iDOC (`BriscoesIDOCParser`) is the live path; EDIFACT `BriscoesParser` is intentionally dormant (not allowlisted, no CT→EA).**
- ASN/DESADV → note it's **built but OFF and unwired**, and was **never a .NET behaviour** (not a decommission blocker); flag as a forward gap only if Briscoes contractually expects DESADV.
- OOS → update to the new **short-ship + ACK** policy once Workstream A ships.

**Files:** `mml_edi/CLAUDE.md`, `mml_edi/__manifest__.py` (description).

---

## 4. Sequencing

```
Ship independently / in parallel:
  B (fence) ──┐
  C (SI DC)  ─┤──► A (short-ship) depends on B's helper
  F (docs)   ─┘
  D (decommission) — operational, parallel to all code; DO FIRST-ish (lowest cost, highest safety)

Order of execution:
  1. D-confirm (D1–D4)  ............  read-only; do immediately, no window
  2. B + C  ........................  ship together (same module, mml_roq_forecast); pure-Python + odoo tests
  3. A  ............................  depends on B helper; ship after B/C verified
  4. D-enforce (D5–D8)  ............  credential rotation needs a brief coordinated window with EDIS
  5. E  ............................  after A is live (so re-ACK uses the corrected DC availability)
  6. F  ............................  anytime; bundle with A's release commit
```

- **Ships with no maintenance window:** B, C, F, D-confirm (code is additive/guarded; docs).
- **Needs a coordinated window:** D-enforce step 7 (FTP credential rotation with EDIS) — short, schedule with ops + EDIS. A's deploy is a standard module upgrade (`-u mml_edi`) but should land in a low-traffic poll gap.
- **A before E:** E's re-ACK depends on A's DC-availability helper being live.

---

## 5. Open decisions needing user sign-off

1. **Per-partner OOS policy flag vs global.** Recommend `oos_policy` on `edi.trading.partner` (`short_ship` for Briscoe Group, `backorder` default for Animates/others until each partner's ORDRSP is verified). Global is simpler but unverified for other partners. **Recommend: per-partner flag.**
2. **`free_qty` vs `qty_available`.** Recommend **`free_qty`** (on-hand − reserved) — `qty_available` ignores reservations and is the exact cause of the Röhlig over-promise.
3. **Fully-OOS line (ship_qty 0): keep line at qty 0 with ORDRSP action 7, or omit the line?** Recommend **keep** (matches Briscoes `Incorrect_Items` reject sample, preserves 1:1 line↔ORDRSP mapping and `CNT+2` count). Needs confirmation that (a) Briscoes accepts a qty-0 confirmed line and (b) Odoo `action_confirm` tolerates a zero-qty line — if not, exclude from SO but still emit the reject from `edi_ordered_qty`.
4. **ORDRSP shortfall format — does Briscoes accept a short-confirm?** Confirmed from their `Short_Supplied` sample: single `QTY+11 = ship_qty`, no second qualifier. **Confirm this is contractually acceptable** before go-live.
5. **Carton (CT) UoM.** Do live Briscoes POs ever use `QTY+11…:CT`? Availability is in EA; if any line is CT, a UoM conversion is required **before** short-ship math is safe (current code doesn't convert). Need a sample/confirmation.
6. **SI DC activation timing.** When does `roq.south_dc_live` flip? Drives Workstream C seed/un-archive runbook.
7. **Workstream E remediation path:** re-ACK corrected ORDRSP vs leave to Röhlig partial-fulfil. Needs Briscoes' position on re-issuing an ORDRSP for an already-ACKed PO.
8. **ORDCHG short-ship.** Change orders flow through `apply_change_order` (qty from change msg, not availability). Recommend **keep current behaviour** (no short-ship recompute) — separate phase.
9. **`edi_ordered_qty` backfill scope** = `product_uom_qty` for all historical EDI lines. Recommend **accept** (historical = accept-in-full, so ordered == current qty).

---

## 6. Risks + rollback

| Risk | Severity | Mitigation |
|---|---|---|
| 🔴 **.NET resumes (reboot/manual) → deletes files → silent order loss OR duplicate ORDRSP to Briscoes** | **HIGHEST** | Workstream D5–D7. **Credential rotation (D7) is the only hard stop** — disabling the service is necessary but not sufficient. |
| 🔴 **CT UoM unhandled → short-ship math wrong** (ship qty computed in EA vs ordered in CT) | **HIGH** | Block A go-live on decision 5.5; add UoM conversion if any live line is CT. Until confirmed, treat as a release gate. |
| 🟠 **Auckland phantom leaks into availability** (current `{}` fallback) | HIGH | Workstream B fence (`roq_island` tag + `0.0` on no-DC) + Workstream C never-Auckland floor. Removes the leak path entirely. |
| 🟠 **`free_qty` netting on reprocess double-reduces a re-polled PO** | MED | `_find_existing_so` skips a valid existing SO before any line is touched; file-hash dedup skips earlier. New-SO creation is the only path that reduces. Explicit idempotency test required. |
| 🟠 **Zero-qty confirmed line breaks Odoo `action_confirm`** | MED | Decision 5.3 verification; fallback = exclude from SO, still ACK reject. |
| 🟡 **SI DC unreachable after `south_dc_live` flip** (archived + `active=True` filter) | MED | Workstream C un-archive + tag in go-live runbook; floor prevents Auckland fallback in the interim. |
| 🟡 **ORDCHG automation regression** (change orders now need an operator click vs hands-free under .NET) | LOW-MED | Out of scope here; flag to ops as a deliberate throughput decision. |
| 🟡 **Re-ACK rejected by Briscoes** for already-ACKed POs (Workstream E) | MED | Confirm with Briscoes (decision 5.7) before mass re-ACK; fallback to manual short-close. |

**Rollback (if `mml_edi` fails post-decommission):**
1. **Stop Odoo polling** — disable EDI poll cron **or** set Briscoe Group partner `active=False`.
2. **Re-arm .NET** — rename `…exe.DECOMMISSIONED` back, `Set-Service -StartupType Automatic`, `Start-Service`, re-enable Scheduled Task.
3. 🔴 **Credential gotcha (biggest rollback risk):** if FTP password was rotated (D7), the .NET `.exe.config` holds the **old** one and will fail to log in. Rollback **must** restore the old credential (or have EDIS revert). **Keep the pre-rotation credential in the sealed runbook** — without it, rollback is broken.
4. **Sweep `/FromEDIS` for `.processed.*` files** before re-arming — the .NET's filter differs and will re-import already-created orders.
5. Code rollback for A/B/C is a standard module downgrade; SOs already created stay (no data migration). Keep the rollback window short; watch `BriscoesEditOrder.log` + SQL audit for the first .NET cycle.

**Highest-value, lowest-cost action overall:** Workstream D confirm + enforce (credential rotation) — it converts today's implicit single-consumer safety into an enforced guarantee, independent of the code build.
