# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Directory Is

This is a **deployment directory for compiled .NET Windows applications** — there is no source code here. The binaries are built from a separate solution (not tracked in this repo) and deployed here for production/test operation. Key DLLs (`BriscoesEDI.dll`, `OdooHandler.dll`, `DynamicXmlApi.dll`) are the compiled application logic.

Parent module context: see `../CLAUDE.md` → `mml_edi` module section.

---

## Applications

### BriscoesEditOrder (`BriscoeEDI/` and `BriscoeEDI_TEST/`)

A .NET Framework 4.8 Windows service that:
1. Polls the EDIS VAN FTP server (`post.edis.co.nz`) every 15 minutes for Briscoes purchase order files
2. Parses PO files and creates Odoo sales orders via XML-RPC
3. Sends acknowledgement messages back to EDIS FTP
4. Logs errors and sends email alerts via Office 365 SMTP

**PO structure:** One Briscoes PO generates multiple Odoo SOs — one per store (order type "MultiStore"). Order IDs are `{BriscoesPONumber}_{StoreCode}` (e.g., `4500176806_1017`).

**Stock checking:** Order lines check available stock at creation; items may be flagged `LineQtyShortfall` but still created if the product has "allow over-sell" enabled.

### DropshipGetTickets (`Dropship/`)

A .NET Framework 4.6.1 Windows service that handles dropship orders. In addition to EDIS FTP polling, it integrates with the Aramex/Fastway API (`api.myfastway.co.nz`) to generate consignment labels (4x6 format).

---

## Infrastructure

| Component | Production | Test |
|---|---|---|
| FTP (EDIS VAN) | `ftp://post.edis.co.nz` `/FromEDIS`, `/ToEDIS` | `/Test/FromEDIS`, `/Test/ToEDIS` |
| Odoo XML-RPC | `https://10.0.0.35:8443/MML_Production/xmlrpc/2` | `https://10.0.0.35:8443/ODOOTEST/xmlrpc/2` |
| PostgreSQL (Odoo DB) | `10.0.0.35:5432` `MML_Production` | `10.0.0.35:5432` `ODOOTEST` |
| SQL Server (EDI audit log) | `10.0.0.6` `BriscoesEDI` | `10.0.0.6` `BriscoesEDI_Test` |
| Local file paths | `C:\BriscoeEDI\{Inbox,Outbox,Processed}` | `C:\BriscoeEDI_TEST\{Inbox,Outbox,Processed}` |
| Alert emails | `edialert@mml.co.nz` | `george@totalsql.co.nz` |

**Odoo integration details:**
- Customer ID for Briscoes Group: `3324`
- Price list: `"Briscoes Products"`
- Company: `"MML Limited"`

---

## Logging

Three destinations (configured in `log4net.config`):
1. **Rolling file** — `BriscoesEditOrder.log` (10MB max, 5 backups)
2. **Windows Event Log** — application name `BriscoesEditOrder` / `DropshipGetTickets`
3. **SQL Server `Log` table** — real-time, buffered at 1 record; includes `Category` field

---

## Deployment Notes

- **`_OldVer/` folders** contain the previous generation (`BriscoesOdooEDI.exe`) — do not restore these
- **`EDI.MOVEDtoD/`** is a legacy archive with 2014-era sample EDI files and WinSCP FTP scripts from before EDIS VAN; retained for reference only
- Configuration lives entirely in `*.exe.config` — all credentials, URLs, and paths are in `appSettings`
- To swap environments (prod ↔ test), the active keys and `x`-prefixed keys in `appSettings` need to be swapped — there is no environment variable or command-line switch
- The `ServiceSleepMins` key controls polling interval (production: 15 min, test: 1 min)
