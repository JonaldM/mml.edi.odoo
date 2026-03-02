# mml_edi Phase 2 Foundation Sprint — Design Document

**Date:** 2026-03-03
**Status:** Approved
**Sprint:** Phase 2 Foundation — Data model hardening, cascade product lookup, iDOC scaffold
**Approach:** Approach C (Foundation hardening + cascade product lookup + iDOC XML scaffold)
**Phase 2A/2B gate:** Awaiting Briscoes EDIFACT D96A and iDOC sample files

---

## Context

Phase 1 (all models, engine, FTP, views, security, tests) is complete. Module installs and runs against the stub parser. This sprint hardens the foundation before real parser data arrives:

- Briscoes sends THREE product codes per line — current processor only tries one
- iDOC XML is a current Briscoes format (v1.6 Oct 2025) but not yet scaffolded
- Per-customer pricelist is implemented but its UI prominence doesn't reflect its importance
- Briscoes store partners already exist in live Odoo; wizard needed for dev/fresh installs
- `test_processor.py` was planned but not created in Phase 1

**mml_base:** Will be installed alongside mml_edi. The `mml.event.emit()` call in processor is not a blocker.

---

## FTP Architecture Decision

**Decision: keep EDIS VAN. No self-hosted FTP.**

MML connects as a *client* to EDIStech's VAN at `post.edis.co.nz`. The VAN provides delivery guarantees, non-repudiation, message sequencing, compliance validation, and keeps Briscoes IT managing connectivity with EDIStech rather than MML. The `edi_ftp.py` client already supports SFTP — switching to direct SFTP in future (if VAN costs become significant) is a config change only.

---

## Section 1: Data Model Changes

### 1.1 `ParsedOrderLine` — new fields (`parsers/base_parser.py`)

```python
@dataclass
class ParsedOrderLine:
    product_code: str       # Primary code (EAN-13 by convention)
    description: str
    quantity: float
    unit_price: float
    line_number: int
    uom: str | None = None
    carton_qty: float | None = None        # QTY+52 (EDIFACT) / BMNG2 (iDOC) — qty per carton
    buyer_article_no: str | None = None    # PIA+IN (EDIFACT) / E1EDP19 001 (iDOC) — Briscoes' code
    vendor_code: str | None = None         # PIA+SA (EDIFACT) / E1EDP19 002 (iDOC) — MML internal ref
```

**Mapping:**

| EDI Field | EDIFACT | iDOC | Odoo Field |
|---|---|---|---|
| EAN-13 / GTIN | `LIN+EN` / `PIA+EN` | `E1EDP19 003` | `product.product.barcode` |
| Buyer article no | `PIA+IN` | `E1EDP19 001` | `product.supplierinfo.product_code` |
| Vendor article no | `PIA+SA` | `E1EDP19 002` | `product.product.default_code` (MML internal ref) |
| Carton qty | `QTY+52` | `BMNG2` | SO line note / new field |

The stub parser leaves the new fields `None` — no behaviour change until Phase 2A/2B real parsers populate them.

### 1.2 `sale.order.line` — new field (`models/sale_order.py`)

```python
edi_matched_by = fields.Selection([
    ('barcode', 'Barcode (EAN-13)'),
    ('default_code', 'Internal Reference'),
    ('supplier_sku', 'Supplier Code'),
], string='Matched By', help='Product lookup strategy used to match this line')
```

Visible in the review form's SO Lines tab. Shows reviewers how each product was found — critical for auditing fallback matches.

### 1.3 `edi.trading.partner.edi_format` — add `idoc_xml` (`models/edi_trading_partner.py`)

```python
edi_format = fields.Selection([
    ('edifact_d96a', 'EDIFACT D96A'),
    ('idoc_xml',     'SAP iDOC XML (ORDERSEXT)'),
    ('edifact_d01b', 'EDIFACT D01B'),
    ('csv',          'CSV'),
    ('custom',       'Custom'),
])
```

### 1.4 `edi.order.issue` — new issue type (`models/edi_order_issue.py`)

Add `product_matched_by_fallback` to `issue_type` selection:

```python
('product_matched_by_fallback', 'Product Matched by Fallback'),
```

Severity: `warning`. Created when cascade lookup succeeds on a non-primary strategy.

---

## Section 2: Cascade Product Lookup

### Logic (`models/edi_processor.py` — `_find_product()`)

Current: tries one field, returns None on miss.

New cascade:

```
Input: product_code (primary), buyer_article_no, vendor_code, partner

Strategy order:
  1. Try partner.product_match_field with product_code
  2. If miss and product_match_field != 'barcode': try barcode with product_code
  3. If miss and vendor_code: try default_code with vendor_code
  4. If miss and buyer_article_no: try product.supplierinfo.product_code with buyer_article_no
  5. If all miss: return None (product_not_found blocking issue — unchanged from today)

Return: (product_record | None, matched_by: str | None)
```

On fallback hit (strategy 2, 3, or 4): create `product_matched_by_fallback` warning issue on the review record, set `sol.edi_matched_by = matched_strategy`.

On primary hit (strategy 1): set `sol.edi_matched_by = partner.product_match_field`.

**Effect with stub parser (all new fields = None):** Strategies 3 and 4 are skipped (no vendor_code / buyer_article_no). Strategy 2 still provides a useful fallback. Behaviour is strictly better than today with no risk of regression.

---

## Section 3: Pricelist UX + Store Master

### 3.1 Pricelist UX (`views/edi_trading_partner_views.xml`)

Changes to the trading partner form:
- Rename "Processing Rules" group header → "Order Processing & Pricing"
- Add `help` text to `pricelist_id` field: *"EDI price comparison uses this pricelist — not the product's sale price. Required if price checking is enabled."*
- Add `help` text to `price_tolerance_pct`: *"Auto-accept orders where the EDI price is within this % of the pricelist price. Set to 0.0 to require exact match."*

No model changes. This is pure XML.

### 3.2 Store Master Seeder (`wizards/edi_seed_stores.py`)

A new transient wizard accessible from the trading partner form via button: "Seed Store Partners".

Behaviour:
- Only enabled when `order_split_mode == 'per_store'`
- Shows a list of known store codes + names for the partner (from a Python dict in the wizard)
- Creates `res.partner` child contacts if they don't already exist:
  - `parent_id = trading_partner.partner_id`
  - `ref = store_code`
  - `name = store_name`
  - `type = 'delivery'`
- If a partner with `ref = store_code` already exists (as in the live Odoo), skips it — idempotent
- Reports: X created, Y already existed

**Briscoes store data (initial list):** Populated from the existing legacy `.NET` app's store config. The list is a Python constant in the wizard — not a database table — so it can be updated in future sprints without a migration.

**Note:** In the live Odoo instance, Briscoes store partners already exist from the legacy system. The wizard is primarily for dev environments and future fresh installs. Running it in production is safe (idempotent — skips existing records).

---

## Section 4: iDOC XML Parser Scaffold

### 4.1 `parsers/briscoes_idoc.py`

New file, parallel to `parsers/briscoes.py`:

```python
class BriscoesIDOCParser(BaseEDIParser):
    """
    Parser for Briscoes SAP iDOC ORDERSEXT purchase orders.

    Phase 1: Returns same 4 mock scenarios as EDIFACT stub.
    Phase 2: Replace parse_file() with real XML parsing:
      - EDI_DC40 header (MANDT=300, IDOCTYP=ORDERS05, CIMTYP=ORDERSEXT)
      - E1EDK01 (BELNR=PO number, BSART=order type, KZABS=ack required)
      - E1EDK03 (dates: 012=PO date, 011=delivery date)
      - E1EDKA1 (partners: AG=buyer, WE=ship-to, LF=vendor)
      - E1EDPA1 (per-line ship-to for multi-store orders)
      - E1EDP01 (lines: POSEX, ACTION, MENGE, VPREI, NETWR)
      - ZE1EDP01 (ATTYP: skip 01=generic articles)
      - E1EDP19 (product IDs: 001=Briscoes, 002=vendor, 003=GTIN)
      - E1EDP20 (delivery schedule: WMENG, AMENG, EDATU)
      - E1EDS01 (summary)
      Reference: Briscoes iDOC ORDERSEXT Implementation Guide v1.6
    """

    def parse_file(self, raw_content: bytes, trading_partner) -> list[ParsedOrder]:
        # PHASE 2: Replace with real XML parsing
        # Phase 1: same mock data as EDIFACT stub
        ...

    def generate_ack(self, review_record) -> bytes:
        # PHASE 2: Generate iDOC ORDRSP XML response
        # Phase 1: placeholder
        ...
```

### 4.2 Trading partner config

`edi_trading_partner_briscoes.xml` stays on `edifact_d96a` / `BriscoesParser`. The iDOC format is selectable via the UI when needed — no second seed data record added automatically.

---

## Section 5: test_processor.py

Missing from Phase 1. Test cases:

| Test | What it exercises |
|---|---|
| `test_full_pipeline_auto_approve` | Mock parser → SO created, auto-approved, ACK queued |
| `test_full_pipeline_pending_review` | Mock parser (problem_order) → pending_review, blocking issues |
| `test_cascade_barcode_miss_fallback_default_code` | Product has no barcode, only default_code; EAN sent → miss → fallback match, warning issue |
| `test_cascade_all_miss_product_not_found` | No product matches any strategy → blocking issue |
| `test_carton_qty_flows_through` | `carton_qty` on `ParsedOrderLine` → stored on SO line |
| `test_duplicate_file_skipped` | Same file hash → no new SO |
| `test_change_order_routed_to_review` | Change order → always pending_review |

---

## Files Changed / Created

| File | Change |
|---|---|
| `parsers/base_parser.py` | Add `carton_qty`, `buyer_article_no`, `vendor_code` to `ParsedOrderLine` |
| `parsers/briscoes_idoc.py` | NEW — iDOC parser stub |
| `models/edi_trading_partner.py` | Add `idoc_xml` to `edi_format` selection |
| `models/edi_processor.py` | Cascade `_find_product()`, set `sol.edi_matched_by` |
| `models/edi_order_issue.py` | Add `product_matched_by_fallback` issue type |
| `models/sale_order.py` | Add `edi_matched_by` to `SaleOrderLine` |
| `views/edi_trading_partner_views.xml` | Pricelist UX improvements |
| `views/edi_order_review_views.xml` | Show `edi_matched_by` in SO Lines tab |
| `wizards/edi_seed_stores.py` | NEW — store seeder wizard |
| `wizards/edi_seed_stores_views.xml` | NEW — wizard view |
| `wizards/__init__.py` | Add import for new wizard |
| `tests/test_processor.py` | NEW — full pipeline test coverage |

---

## Out of Scope (Phase 2A/2B — awaiting sample files)

- Real EDIFACT D96A parsing (replace `BriscoesParser.parse_file()` stub)
- Real iDOC XML parsing (replace `BriscoesIDOCParser.parse_file()` stub)
- Real ORDRSP/APERAK ACK generation
- Per-line delivery dates (multi-store LOC structure)
- Generic article filtering (iDOC ATTYP=01 skip) — scaffold comment only
