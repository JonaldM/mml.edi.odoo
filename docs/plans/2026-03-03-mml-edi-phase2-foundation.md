# mml_edi Phase 2 Foundation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Harden the mml_edi module foundation before real Briscoes EDI files arrive — extend data model for multi-code product IDs, implement cascade product lookup with fallback warning issues, scaffold iDOC XML parser, improve pricelist UX, add store seeder wizard, and write missing test_processor.py.

**Architecture:** No new Odoo models. Changes span base_parser.py contracts (dataclass fields), edi_processor.py logic (cascade lookup), model field additions (SaleOrderLine, EDIOrderIssue), two new parser/wizard files, and view XML updates. All changes are backward-compatible with the existing Phase 1 stub parser.

**Tech Stack:** Odoo 19 ORM, Python dataclasses, Odoo TransactionCase tests, `odoo-bin --test-enable`

**Working directory:** `E:\ClaudeCode\projects\mml.odoo.apps\briscoes.edi\mml.edi\`

**Run tests:** `./odoo-bin --test-enable -d <db> --test-tags mml_edi`
(Or from the Odoo server: `odoo-bin -c /etc/odoo/odoo.conf --test-enable --stop-after-init -i mml_edi`)

---

## Task 1: Extend ParsedOrderLine dataclass

Foundation for all downstream tasks. Add three optional fields to the data contract.

**Files:**
- Modify: `parsers/base_parser.py` (lines 26-34)
- Modify: `tests/common.py` — update `make_parsed_line()` helper

**Step 1: Edit `parsers/base_parser.py`**

Replace the `ParsedOrderLine` dataclass (lines 26-34) with:

```python
@dataclass
class ParsedOrderLine:
    """Represents a single line item from an EDI order document."""

    product_code: str          # Primary code (EAN-13 by convention) — matched against product_match_field
    description: str
    quantity: float
    unit_price: float          # Price from EDI — what the customer expects to pay
    line_number: int           # EDI line number for ACK reference
    uom: str | None = None     # Unit of measure from EDI (may differ from Odoo UOM)
    carton_qty: float | None = None       # QTY+52 (EDIFACT) / BMNG2 (iDOC) — qty per carton/inner pack
    buyer_article_no: str | None = None   # PIA+IN (EDIFACT) / E1EDP19 001 (iDOC) — buyer's own item code
    vendor_code: str | None = None        # PIA+SA (EDIFACT) / E1EDP19 002 (iDOC) — MML internal reference
```

**Step 2: Update `tests/common.py` `make_parsed_line()` — no change needed**

The new fields have defaults of `None` so existing call sites are unaffected. Verify by checking all `make_parsed_line(` calls — no signature changes required.

**Step 3: Verify no breakage (parsers package loads cleanly)**

```bash
cd "E:/ClaudeCode/projects/mml.odoo.apps/briscoes.edi/mml.edi"
python -c "from parsers.base_parser import ParsedOrderLine, ParsedOrder; print('OK')"
```
Expected: `OK`

**Step 4: Commit**

```bash
cd "E:/ClaudeCode/projects/mml.odoo.apps/briscoes.edi/mml.edi"
git add parsers/base_parser.py
git commit -m "feat(edi): extend ParsedOrderLine with carton_qty, buyer_article_no, vendor_code"
```

---

## Task 2: Add `edi_matched_by` to SaleOrderLine + new issue type

**Files:**
- Modify: `models/sale_order.py`
- Modify: `models/edi_order_issue.py`

**Step 1: Add `edi_matched_by` field to `SaleOrderLine` in `models/sale_order.py`**

In the `SaleOrderLine` class, after the `edi_qty_shortfall` field (line ~64), add:

```python
    edi_matched_by = fields.Selection(
        [
            ("barcode", "Barcode (EAN-13)"),
            ("default_code", "Internal Reference"),
            ("supplier_sku", "Supplier Code"),
        ],
        string="Matched By",
        help="Product lookup strategy that succeeded for this EDI line",
    )
```

**Step 2: Add `product_matched_by_fallback` to `issue_type` in `models/edi_order_issue.py`**

In the `issue_type` selection field, add the new option before `("other", "Other")`:

```python
            ("product_matched_by_fallback", "Product Matched by Fallback"),
```

Full updated selection (replace the existing `issue_type` selection list):

```python
    issue_type = fields.Selection(
        [
            ("price_discrepancy", "Price Discrepancy"),
            ("product_not_found", "Product Not Found"),
            ("qty_shortfall", "Stock Shortfall"),
            ("unknown_store", "Unknown Store"),
            ("uom_mismatch", "UOM Mismatch"),
            ("product_matched_by_fallback", "Product Matched by Fallback"),
            ("other", "Other"),
        ],
        required=True,
        string="Issue Type",
    )
```

**Step 3: Verify Odoo model syntax — import check**

```bash
python -c "
import sys; sys.path.insert(0, '.')
# Quick AST check — no Odoo needed
import ast, pathlib
for f in ['models/sale_order.py', 'models/edi_order_issue.py']:
    ast.parse(pathlib.Path(f).read_text())
    print(f'{f}: syntax OK')
"
```
Expected: both files print `syntax OK`

**Step 4: Commit**

```bash
git add models/sale_order.py models/edi_order_issue.py
git commit -m "feat(edi): add edi_matched_by to SOLine, product_matched_by_fallback issue type"
```

---

## Task 3: Write failing test for cascade product lookup

Write the test FIRST so it fails, confirming the test is real.

**Files:**
- Modify: `tests/common.py` — add cascade test helper
- Create: `tests/test_cascade_lookup.py`

**Step 1: Add helper to `tests/common.py`**

At the bottom of `common.py`, add a new helper after `make_change_order_parsed_order`:

```python
def make_fallback_lookup_order(
    primary_ean="NONEXISTENT_EAN_0000",
    vendor_code="MML-INTERNAL-001",
    buyer_article_no="BRISCOES-ART-001",
    po_number="TESTPO_CASCADE",
):
    """
    ParsedOrder where primary product_code (EAN) won't match anything,
    but vendor_code (MML internal ref) WILL match a product with default_code.
    Used for cascade lookup tests.
    """
    return ParsedOrder(
        po_number=po_number,
        order_date=date.today(),
        lines=[
            ParsedOrderLine(
                product_code=primary_ean,
                description="Cascade Test Product",
                quantity=5.0,
                unit_price=9.99,
                line_number=1,
                vendor_code=vendor_code,
                buyer_article_no=buyer_article_no,
            )
        ],
        document_type="new_order",
        raw_data="MOCK_CASCADE_EDI",
    )
```

**Step 2: Create `tests/test_cascade_lookup.py`**

```python
# mml.edi/tests/test_cascade_lookup.py
"""
Cascade product lookup tests.

Tests the _find_product() cascade logic:
  1. Try configured product_match_field with product_code
  2. If miss, try barcode with product_code
  3. If miss, try default_code with vendor_code
  4. If miss, try supplierinfo.product_code with buyer_article_no
  5. If all miss -> product_not_found blocking issue

Also tests:
  - sol.edi_matched_by is set correctly
  - A warning 'product_matched_by_fallback' issue is created on fallback
  - Primary match does NOT create a fallback warning issue

Run with: ./odoo-bin --test-enable -d <db> --test-tags mml_edi
"""
from odoo.tests.common import TransactionCase

from .common import EDITestSetup, make_fallback_lookup_order


class TestCascadeLookup(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        # The trading partner uses product_match_field='barcode' from EDITestSetup.
        # For cascade tests we keep that — the primary match will fail,
        # forcing fallback through vendor_code (default_code).

    def _create_product_no_barcode(self, internal_ref="MML-INTERNAL-001"):
        """Product with default_code set but NO barcode — primary EAN lookup will miss."""
        return self.env["product.product"].create({
            "name": "Cascade Test Product",
            "default_code": internal_ref,
            "list_price": 9.99,
            "type": "product",
            # Intentionally NO barcode field set
        })

    def _create_supplier_coded_product(self, buyer_code="BRISCOES-ART-001"):
        """Product with neither barcode nor default_code, but a supplierinfo entry."""
        product = self.env["product.product"].create({
            "name": "Supplier Code Test Product",
            "list_price": 9.99,
            "type": "product",
        })
        self.env["product.supplierinfo"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "product_id": product.id,
            "product_code": buyer_code,
        })
        return product

    # ── Fallback to default_code ──────────────────────────────────────────

    def test_cascade_fallback_to_default_code(self):
        """
        EAN-13 (product_code) doesn't match. vendor_code matches default_code.
        SO line should be created, edi_matched_by='default_code', warning issue raised.
        """
        product = self._create_product_no_barcode("MML-INTERNAL-001")
        # Add to pricelist so no price discrepancy issue is raised
        self.env["product.pricelist.item"].create({
            "pricelist_id": self.trading_partner.pricelist_id.id,
            "product_id": product.id,
            "compute_price": "fixed",
            "fixed_price": 9.99,
        })

        order = make_fallback_lookup_order(
            primary_ean="NONEXISTENT_EAN_0000",
            vendor_code="MML-INTERNAL-001",
        )

        review = self.env["edi.order.review"].create({
            "trading_partner_id": self.trading_partner.id,
            "customer_po_number": order.po_number,
            "document_type": "new_order",
        })
        so = self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "edi_trading_partner_id": self.trading_partner.id,
            "client_order_ref": order.po_number,
        })

        # This call should succeed via fallback
        blocking = self.processor._process_order_line(order.lines[0], so, self.trading_partner, review)

        # SO line created (not blocking)
        self.assertEqual(len(blocking), 0, "Cascade fallback should not be blocking")
        sol = so.order_line
        self.assertEqual(len(sol), 1, "One SO line should be created")
        self.assertEqual(sol.edi_matched_by, "default_code",
                         "edi_matched_by should record the fallback strategy used")

        # Warning issue raised for fallback
        fallback_issues = review.issue_ids.filtered(
            lambda i: i.issue_type == "product_matched_by_fallback"
        )
        self.assertEqual(len(fallback_issues), 1,
                         "One fallback warning issue should be created")
        self.assertEqual(fallback_issues.severity, "warning")

    def test_cascade_fallback_to_supplier_sku(self):
        """
        EAN and vendor_code both miss. buyer_article_no matches supplierinfo.product_code.
        edi_matched_by='supplier_sku', warning issue raised.
        """
        product = self._create_supplier_coded_product("BRISCOES-ART-001")
        self.env["product.pricelist.item"].create({
            "pricelist_id": self.trading_partner.pricelist_id.id,
            "product_id": product.id,
            "compute_price": "fixed",
            "fixed_price": 9.99,
        })

        order = make_fallback_lookup_order(
            primary_ean="NONEXISTENT_EAN_9999",
            vendor_code="NONEXISTENT_INTERNAL",
            buyer_article_no="BRISCOES-ART-001",
        )

        review = self.env["edi.order.review"].create({
            "trading_partner_id": self.trading_partner.id,
            "customer_po_number": order.po_number,
            "document_type": "new_order",
        })
        so = self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "edi_trading_partner_id": self.trading_partner.id,
            "client_order_ref": order.po_number,
        })

        blocking = self.processor._process_order_line(order.lines[0], so, self.trading_partner, review)

        self.assertEqual(len(blocking), 0)
        sol = so.order_line
        self.assertEqual(sol.edi_matched_by, "supplier_sku")

        fallback_issues = review.issue_ids.filtered(
            lambda i: i.issue_type == "product_matched_by_fallback"
        )
        self.assertEqual(len(fallback_issues), 1)

    def test_cascade_all_miss_product_not_found(self):
        """
        All four strategies fail. product_not_found blocking issue raised. No SO line.
        """
        order = make_fallback_lookup_order(
            primary_ean="DEAD_EAN",
            vendor_code="DEAD_INTERNAL",
            buyer_article_no="DEAD_BUYER_CODE",
        )
        review = self.env["edi.order.review"].create({
            "trading_partner_id": self.trading_partner.id,
            "customer_po_number": order.po_number,
            "document_type": "new_order",
        })
        so = self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "edi_trading_partner_id": self.trading_partner.id,
            "client_order_ref": order.po_number,
        })

        blocking = self.processor._process_order_line(order.lines[0], so, self.trading_partner, review)

        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0]["type"], "product_not_found")
        self.assertEqual(len(so.order_line), 0, "No SO line when product not found")

    def test_primary_match_no_fallback_issue(self):
        """
        Primary barcode match succeeds. edi_matched_by='barcode', no fallback issue.
        (Regression: cascade must not trigger for primary matches.)
        """
        # self.test_product from EDITestSetup has barcode='TEST001'
        # Add pricelist item for it
        order = make_fallback_lookup_order(
            primary_ean="TEST001",  # matches self.test_product.barcode
            vendor_code="SOME_CODE",
        )
        review = self.env["edi.order.review"].create({
            "trading_partner_id": self.trading_partner.id,
            "customer_po_number": order.po_number,
            "document_type": "new_order",
        })
        so = self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "edi_trading_partner_id": self.trading_partner.id,
            "client_order_ref": order.po_number,
        })

        blocking = self.processor._process_order_line(order.lines[0], so, self.trading_partner, review)

        sol = so.order_line
        self.assertEqual(len(sol), 1)
        self.assertEqual(sol.edi_matched_by, "barcode",
                         "Primary match → edi_matched_by should be 'barcode'")

        fallback_issues = review.issue_ids.filtered(
            lambda i: i.issue_type == "product_matched_by_fallback"
        )
        self.assertEqual(len(fallback_issues), 0,
                         "No fallback issue on primary match")
```

**Step 3: Verify test file is syntactically valid**

```bash
python -c "import ast, pathlib; ast.parse(pathlib.Path('tests/test_cascade_lookup.py').read_text()); print('syntax OK')"
```

**Step 4: Run tests — confirm they FAIL** (cascade not yet implemented)

```bash
./odoo-bin --test-enable -d <db> --test-tags mml_edi.TestCascadeLookup
```
Expected: `test_cascade_fallback_to_default_code` fails because `_find_product()` still uses single-mode lookup.

**Step 5: Commit (tests only)**

```bash
git add tests/common.py tests/test_cascade_lookup.py
git commit -m "test(edi): add failing cascade product lookup tests"
```

---

## Task 4: Implement cascade product lookup

Replace `_find_product()` in `models/edi_processor.py` with cascade logic.

**Files:**
- Modify: `models/edi_processor.py`

**Step 1: Replace `_find_product()` (currently lines ~442-460)**

The current signature returns `product | None`. The new version returns a tuple `(product | None, matched_by: str | None)`. You must also update the single call site `_process_order_line()`.

Replace the entire `_find_product` method:

```python
    def _find_product(self, parsed_line, partner):
        """
        Look up product using cascade strategy:
        1. Try partner.product_match_field with parsed_line.product_code (primary)
        2. If miss: try barcode with product_code (if not already barcode mode)
        3. If miss: try default_code with parsed_line.vendor_code
        4. If miss: try product.supplierinfo.product_code with parsed_line.buyer_article_no

        Returns: (product_record | None, matched_by: str | None)
        matched_by is the strategy name that succeeded, or None if not found.
        """
        strategies = []

        # Strategy 1: configured primary field
        primary_field = partner.product_match_field
        strategies.append((primary_field, parsed_line.product_code))

        # Strategy 2: barcode fallback (if primary wasn't already barcode)
        if primary_field != "barcode":
            strategies.append(("barcode", parsed_line.product_code))

        # Strategy 3: internal reference via vendor_code
        if parsed_line.vendor_code:
            strategies.append(("default_code", parsed_line.vendor_code))

        # Strategy 4: supplier code via buyer_article_no
        if parsed_line.buyer_article_no:
            strategies.append(("supplier_sku", parsed_line.buyer_article_no))

        for strategy, code in strategies:
            if not code:
                continue
            product = self._lookup_by_strategy(strategy, code)
            if product:
                return product, strategy

        return None, None

    def _lookup_by_strategy(self, strategy: str, code: str):
        """Single-strategy product lookup. Returns product.product record or empty."""
        if strategy == "barcode":
            return self.env["product.product"].search(
                [("barcode", "=", code)], limit=1
            ) or None
        elif strategy == "default_code":
            return self.env["product.product"].search(
                [("default_code", "=", code)], limit=1
            ) or None
        elif strategy == "supplier_sku":
            info = self.env["product.supplierinfo"].search(
                [("product_code", "=", code)], limit=1
            )
            return info.product_id if info else None
        return None
```

**Step 2: Update `_process_order_line()` to use the new signature**

Find the call to `_find_product` (~line 229):

```python
        product = self._find_product(parsed_line.product_code, partner)
```

Replace it with:

```python
        product, matched_by = self._find_product(parsed_line, partner)
```

After the `product not found` block (after the `return blocking` for None product), find the SO line creation block and update it to set `edi_matched_by`:

Current SO line create (around line 250):
```python
        sol = self.env["sale.order.line"].create({
            "order_id": so.id,
            "product_id": product.id,
            "product_uom_qty": parsed_line.quantity,
            "price_unit": parsed_line.unit_price,
            "edi_line_number": parsed_line.line_number,
            "edi_price": parsed_line.unit_price,
        })
```

Replace with:
```python
        sol = self.env["sale.order.line"].create({
            "order_id": so.id,
            "product_id": product.id,
            "product_uom_qty": parsed_line.quantity,
            "price_unit": parsed_line.unit_price,
            "edi_line_number": parsed_line.line_number,
            "edi_price": parsed_line.unit_price,
            "edi_matched_by": matched_by,
        })

        # Create fallback warning issue if product was not found on primary strategy
        primary_field = partner.product_match_field
        if matched_by and matched_by != primary_field:
            self.env["edi.order.issue"].create({
                "review_id": review.id,
                "issue_type": "product_matched_by_fallback",
                "severity": "warning",
                "description": (
                    "Product matched by fallback '%s' — primary code '%s' not found "
                    "via '%s'. Consider adding the barcode/code to the product record." % (
                        matched_by, parsed_line.product_code, primary_field,
                    )
                ),
                "sale_order_line_id": sol.id,
            })
```

**Step 3: Run the cascade tests — they must now PASS**

```bash
./odoo-bin --test-enable -d <db> --test-tags mml_edi.TestCascadeLookup
```
Expected: All 4 cascade tests PASS.

**Step 4: Run full test suite — no regressions**

```bash
./odoo-bin --test-enable -d <db> --test-tags mml_edi
```
Expected: All existing tests still PASS.

**Step 5: Commit**

```bash
git add models/edi_processor.py
git commit -m "feat(edi): implement cascade product lookup with fallback warning issues"
```

---

## Task 5: Add `idoc_xml` format + `BriscoesIDOCParser` scaffold

**Files:**
- Modify: `models/edi_trading_partner.py`
- Create: `parsers/briscoes_idoc.py`
- Modify: `parsers/__init__.py` (if it explicitly lists imports)

**Step 1: Add `idoc_xml` to `edi_format` selection in `models/edi_trading_partner.py`**

Replace the existing `edi_format` field definition:

```python
    edi_format = fields.Selection(
        [
            ("edifact_d96a", "EDIFACT D96A"),
            ("idoc_xml", "SAP iDOC XML (ORDERSEXT)"),
            ("edifact_d01b", "EDIFACT D01B"),
            ("csv", "CSV"),
            ("custom", "Custom"),
        ],
        required=True,
        string="EDI Format",
    )
```

**Step 2: Create `parsers/briscoes_idoc.py`**

```python
# mml.edi/parsers/briscoes_idoc.py
"""
Briscoes iDOC XML Parser — Phase 1 Stub.

Returns same mock ParsedOrder data as BriscoesParser to exercise all pipeline
code paths. Phase 2: Replace parse_file() and generate_ack() with real
SAP ORDERSEXT XML parsing when sample files are provided by Briscoes IT.

iDOC XML Structure (ORDERSEXT v1.6, MANDT=300):
  EDI_DC40          — interchange header
  E1EDK01           — PO header (BELNR=PO#, BSART=type, KZABS=ack flag)
  E1EDK03           — dates (012=PO date, 011=delivery date)
  E1EDKA1           — header partners (AG=buyer, WE=single ship-to, LF=vendor)
  E1EDP01           — line items (POSEX, ACTION, MENGE, MENEE, BMNG2, VPREI, NETWR)
  ZE1EDP01          — extended line (ATTYP: skip if 01=generic article)
  E1EDPA1           — per-line ship-to for multi-store orders (PARVW=WE, LIFNR=store)
  E1EDP19           — product IDs (001=Briscoes code, 002=vendor/MML code, 003=GTIN)
  E1EDP20           — delivery schedule (WMENG, AMENG, EDATU)
  E1EDS01           — summary
"""

import logging
from datetime import date, timedelta

from .base_parser import BaseEDIParser, ParsedOrder, ParsedOrderLine

_logger = logging.getLogger(__name__)

_MOCK_STORE_A = "1017"
_MOCK_STORE_B = "1042"


class BriscoesIDOCParser(BaseEDIParser):
    """
    Parser for Briscoes SAP iDOC ORDERSEXT purchase orders.

    Phase 1: Returns mock data for end-to-end pipeline testing.
    Phase 2: Implement real ORDERSEXT XML parsing.
    """

    def parse_file(
        self, raw_content: bytes, trading_partner
    ) -> list[ParsedOrder]:
        """
        # PHASE 2: Replace this stub with real iDOC XML parsing.
        #
        # The real implementation should:
        # 1. Parse raw_content as XML (xml.etree.ElementTree or lxml)
        # 2. Validate EDI_DC40 header: MANDT=300, IDOCTYP=ORDERS05, CIMTYP=ORDERSEXT
        # 3. Identify order type from E1EDK01 BSART:
        #      ZNS/ZWB=new order, ZNC/ZNB/ZNR/ZNP=various new types, ZCH=change order
        # 4. Extract dates from E1EDK03 (012=PO date, 011=delivery date)
        # 5. Extract partners from E1EDKA1 (AG=buyer, WE=single ship-to)
        # 6. For multi-store: E1EDPA1 at line level (PARVW=WE, LIFNR=store code)
        # 7. For each E1EDP01 line:
        #    a. Skip if ZE1EDP01 ATTYP == '01' (generic article placeholder — not a real product)
        #    b. Extract POSEX (line number), ACTION (001=add, 002=change, 003=delete)
        #    c. Extract MENGE (qty), BMNG2 (carton qty), VPREI (unit price)
        #    d. Extract product codes from E1EDP19:
        #         001 → buyer_article_no (Briscoes' code)
        #         002 → vendor_code (MML internal reference)
        #         003 → product_code (GTIN/EAN-13) — use as primary
        #    e. Extract delivery schedule from E1EDP20 (EDATU=date, WMENG=qty)
        # 8. Group lines by ship-to store code → one ParsedOrder per store
        # 9. If E1EDK01 KZABS == 'X' → set ack_required=True on ParsedOrder
        #
        # Reference: Briscoes iDOC ORDERSEXT Implementation Guide v1.6 (Oct 2025)
        # Sample files: provided by Briscoes IT in Phase 2

        Phase 1: Return same 4 mock ParsedOrder objects as EDIFACT stub.
        """
        _logger.info(
            "[BriscoesIDOCParser] Phase 1 stub: returning mock parsed orders (iDOC format)"
        )

        today = date.today()
        delivery_date = today + timedelta(days=7)
        changed_delivery_date = today + timedelta(days=14)
        raw_text = raw_content.decode("utf-8", errors="replace")

        # Scenario 1: Clean new order for store 1017
        clean_order = ParsedOrder(
            po_number="4500999001",
            store_code=_MOCK_STORE_A,
            order_date=today,
            requested_delivery_date=delivery_date,
            lines=[
                ParsedOrderLine(
                    product_code="9300601234567",
                    description="Volere Sparkling Water 12pk",
                    quantity=24.0,
                    unit_price=18.99,
                    line_number=1,
                    buyer_article_no="BRS-001234",
                    vendor_code="VOL-SPARK-12",
                ),
                ParsedOrderLine(
                    product_code="9300601234568",
                    description="Volere Still Water 12pk",
                    quantity=12.0,
                    unit_price=18.99,
                    line_number=2,
                    buyer_article_no="BRS-001235",
                    vendor_code="VOL-STILL-12",
                ),
                ParsedOrderLine(
                    product_code="9300601234569",
                    description="Enkel Sparkling 6pk",
                    quantity=6.0,
                    unit_price=11.99,
                    line_number=3,
                    buyer_article_no="BRS-002100",
                    vendor_code="ENK-SPARK-6",
                ),
            ],
            document_type="new_order",
            raw_data=raw_text,
        )

        # Scenario 2: New order with issues for store 1042
        problem_order = ParsedOrder(
            po_number="4500999002",
            store_code=_MOCK_STORE_B,
            order_date=today,
            requested_delivery_date=delivery_date,
            lines=[
                ParsedOrderLine(
                    product_code="9300601234567",
                    description="Volere Sparkling Water 12pk",
                    quantity=24.0,
                    unit_price=999.99,
                    line_number=1,
                    vendor_code="VOL-SPARK-12",
                ),
                ParsedOrderLine(
                    product_code="UNKNOWN_SKU_00000",
                    description="Mystery Product",
                    quantity=10.0,
                    unit_price=9.99,
                    line_number=2,
                ),
            ],
            document_type="new_order",
            raw_data=raw_text,
        )

        # Scenario 3: Change order
        change_order = ParsedOrder(
            po_number="4500999001",
            store_code=_MOCK_STORE_A,
            order_date=today,
            requested_delivery_date=changed_delivery_date,
            lines=[
                ParsedOrderLine(
                    product_code="9300601234567",
                    description="Volere Sparkling Water 12pk",
                    quantity=36.0,
                    unit_price=18.99,
                    line_number=1,
                    vendor_code="VOL-SPARK-12",
                ),
                ParsedOrderLine(
                    product_code="9300601234568",
                    description="Volere Still Water 12pk",
                    quantity=12.0,
                    unit_price=18.99,
                    line_number=2,
                    vendor_code="VOL-STILL-12",
                ),
                ParsedOrderLine(
                    product_code="9300601234569",
                    description="Enkel Sparkling 6pk",
                    quantity=6.0,
                    unit_price=11.99,
                    line_number=3,
                    vendor_code="ENK-SPARK-6",
                ),
            ],
            document_type="change_order",
            change_reason="Customer increased order quantity",
            raw_data=raw_text,
        )

        # Scenario 4: Duplicate
        duplicate_order = ParsedOrder(
            po_number="4500999001",
            store_code=_MOCK_STORE_A,
            order_date=today,
            requested_delivery_date=delivery_date,
            lines=[
                ParsedOrderLine(
                    product_code="9300601234567",
                    description="Volere Sparkling Water 12pk",
                    quantity=24.0,
                    unit_price=18.99,
                    line_number=1,
                    vendor_code="VOL-SPARK-12",
                ),
            ],
            document_type="new_order",
            raw_data=raw_text,
        )

        return [clean_order, problem_order, change_order, duplicate_order]

    def generate_ack(self, review_record) -> bytes:
        """
        # PHASE 2: Replace with real iDOC ORDRSP XML generation.
        #
        # The real implementation should:
        # 1. Generate ORDRSP iDOC XML (ORDERS05/ORDERSEXT response)
        # 2. Set E1EDK01 BSART to appropriate response type
        # 3. Include accepted/rejected line details
        # 4. Follow Briscoes-specific iDOC response spec
        #
        # Reference: Briscoes iDOC PO Response Guide v1.7

        Phase 1: return a placeholder ACK for pipeline testing.
        """
        _logger.info(
            "[BriscoesIDOCParser] Phase 1 stub: generating placeholder iDOC ACK for %s",
            review_record.customer_po_number,
        )
        return (
            "<?xml version='1.0'?><ORDERS05><EDI_DC40><IDOCTYP>ORDERS05</IDOCTYP>"
            "</EDI_DC40><!-- PHASE2_PLACEHOLDER: %s %s --></ORDERS05>" % (
                review_record.customer_po_number,
                review_record.state,
            )
        ).encode("utf-8")
```

**Step 3: Check `parsers/__init__.py`** — verify it doesn't need updating

```bash
cat parsers/__init__.py
```

If it's empty or uses relative imports like `from . import briscoes`, add `from . import briscoes_idoc`. If it's an empty `__init__.py`, no change needed — the parser is loaded dynamically via `parser_class` string.

**Step 4: Verify import works**

```bash
python -c "
from parsers.briscoes_idoc import BriscoesIDOCParser
p = BriscoesIDOCParser()
orders = p.parse_file(b'test', None)
print(f'OK: {len(orders)} orders returned')
"
```
Expected: `OK: 4 orders returned`

**Step 5: Commit**

```bash
git add models/edi_trading_partner.py parsers/briscoes_idoc.py parsers/__init__.py
git commit -m "feat(edi): add idoc_xml format option and BriscoesIDOCParser Phase 1 stub"
```

---

## Task 6: Pricelist UX improvements in views

**Files:**
- Modify: `views/edi_trading_partner_views.xml`

**Step 1: Locate the "Processing Rules" group in the form view**

Open `views/edi_trading_partner_views.xml` and find the `<group string="Processing Rules">` element.

**Step 2: Make these changes:**

1. Rename `string="Processing Rules"` → `string="Order Processing &amp; Pricing"`
2. On the `<field name="pricelist_id">` element, add:
   ```xml
   help="EDI price comparison uses this pricelist — not the product's sale price. Required if price checking is enabled."
   ```
3. On the `<field name="price_tolerance_pct">` element, add:
   ```xml
   help="Auto-accept orders where the EDI price is within this % of the pricelist price. Set 0.0 to require exact match (recommended for retail compliance)."
   ```
4. On the `<field name="auto_confirm_clean">` element, add:
   ```xml
   help="Automatically confirm and queue ACK for orders with no blocking issues. Disable for high-value partners where human review is always required."
   ```

**Step 3: Verify XML is well-formed**

```bash
python -c "import xml.etree.ElementTree as ET; ET.parse('views/edi_trading_partner_views.xml'); print('XML OK')"
```
Expected: `XML OK`

**Step 4: Commit**

```bash
git add views/edi_trading_partner_views.xml
git commit -m "ux(edi): improve pricelist field labels and help text on trading partner form"
```

---

## Task 7: Show `edi_matched_by` in review form SO Lines tab

**Files:**
- Modify: `views/edi_order_review_views.xml`

**Step 1: Locate the SO Lines tab in the review form**

Find `<field name="sale_order_line_ids">` tree view inside the form view.

**Step 2: Add `edi_matched_by` column**

In the `<tree>` inside `sale_order_line_ids`, add after the `edi_line_number` column:

```xml
<field name="edi_matched_by" optional="show"/>
```

Also add `edi_price_discrepancy` if not already shown:
```xml
<field name="edi_price_discrepancy" optional="show"/>
```

**Step 3: Verify XML**

```bash
python -c "import xml.etree.ElementTree as ET; ET.parse('views/edi_order_review_views.xml'); print('XML OK')"
```

**Step 4: Commit**

```bash
git add views/edi_order_review_views.xml
git commit -m "ux(edi): show edi_matched_by in review form SO lines tab"
```

---

## Task 8: Store seeder wizard

**Files:**
- Create: `wizards/edi_seed_stores.py`
- Create: `wizards/edi_seed_stores_views.xml`
- Modify: `wizards/__init__.py`
- Modify: `views/edi_trading_partner_views.xml` (add button)
- Modify: `__manifest__.py` (add new view XML)

**Step 1: Create `wizards/edi_seed_stores.py`**

```python
# mml.edi/wizards/edi_seed_stores.py
"""
Wizard to seed Briscoes store partners as child contacts of the Briscoes Group partner.

Used for fresh installs / dev environments. In production, store partners
already exist from the legacy .NET system — running this wizard is safe
(idempotent: skips partners where res.partner.ref already exists as a child).
"""
from odoo import _, api, fields, models

# Briscoes store master data — update when new stores are added
# Format: (store_code, store_name)
_BRISCOES_STORES = [
    ("1017", "Briscoes - Auckland City"),
    ("1042", "Briscoes - Penrose"),
    ("1043", "Briscoes - Manukau"),
    ("1044", "Briscoes - Albany"),
    ("1045", "Briscoes - Westgate"),
    ("1046", "Briscoes - Henderson"),
    ("1050", "Briscoes - Hamilton"),
    ("1060", "Briscoes - Tauranga"),
    ("1070", "Briscoes - Wellington City"),
    ("1071", "Briscoes - Petone"),
    ("1072", "Briscoes - Porirua"),
    ("1080", "Briscoes - Christchurch"),
    ("1081", "Briscoes - Riccarton"),
    ("1082", "Briscoes - Papanui"),
    ("1090", "Briscoes - Dunedin"),
    ("2017", "Rebel Sport - Auckland City"),
    ("2042", "Rebel Sport - Penrose"),
    ("2050", "Rebel Sport - Hamilton"),
    ("2070", "Rebel Sport - Wellington"),
    ("2080", "Rebel Sport - Christchurch"),
    ("3017", "Living & Giving - Auckland City"),
    ("3070", "Living & Giving - Wellington"),
    ("3080", "Living & Giving - Christchurch"),
]


class EDISeedStoresWizard(models.TransientModel):
    _name = "edi.seed.stores.wizard"
    _description = "Seed Briscoes Store Partners"

    trading_partner_id = fields.Many2one(
        "edi.trading.partner",
        required=True,
        readonly=True,
        string="Trading Partner",
    )

    # Result summary (populated after execution)
    result_created = fields.Integer(string="Partners Created", readonly=True)
    result_skipped = fields.Integer(string="Already Existed (Skipped)", readonly=True)
    result_message = fields.Text(string="Result", readonly=True)
    state = fields.Selection(
        [("draft", "Ready"), ("done", "Complete")],
        default="draft",
        string="State",
    )

    def action_seed_stores(self):
        """Create missing store partners. Idempotent — skips existing."""
        self.ensure_one()
        parent_partner = self.trading_partner_id.partner_id
        created = 0
        skipped = 0

        for store_code, store_name in _BRISCOES_STORES:
            existing = self.env["res.partner"].search([
                ("parent_id", "=", parent_partner.id),
                ("ref", "=", store_code),
            ], limit=1)

            if existing:
                skipped += 1
            else:
                self.env["res.partner"].create({
                    "name": store_name,
                    "parent_id": parent_partner.id,
                    "ref": store_code,
                    "type": "delivery",
                    "customer_rank": 1,
                })
                created += 1

        self.write({
            "result_created": created,
            "result_skipped": skipped,
            "result_message": "Created %d store partner(s). %d already existed and were skipped." % (
                created, skipped
            ),
            "state": "done",
        })

        return {
            "type": "ir.actions.act_window",
            "res_model": "edi.seed.stores.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
```

**Step 2: Create `wizards/edi_seed_stores_views.xml`**

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="view_edi_seed_stores_wizard_form" model="ir.ui.view">
        <field name="name">edi.seed.stores.wizard.form</field>
        <field name="model">edi.seed.stores.wizard</field>
        <field name="arch" type="xml">
            <form string="Seed Briscoes Store Partners">
                <group>
                    <field name="trading_partner_id"/>
                </group>
                <group invisible="state == 'draft'">
                    <field name="result_created"/>
                    <field name="result_skipped"/>
                    <field name="result_message" widget="html"/>
                </group>
                <footer>
                    <button name="action_seed_stores"
                            string="Seed Store Partners"
                            type="object"
                            class="btn-primary"
                            invisible="state == 'done'"/>
                    <button string="Close" class="btn-secondary" special="cancel"/>
                </footer>
            </form>
        </field>
    </record>

    <record id="action_edi_seed_stores_wizard" model="ir.actions.act_window">
        <field name="name">Seed Store Partners</field>
        <field name="res_model">edi.seed.stores.wizard</field>
        <field name="view_mode">form</field>
        <field name="target">new</field>
        <field name="context">{'default_trading_partner_id': active_id}</field>
    </record>
</odoo>
```

**Step 3: Update `wizards/__init__.py`**

Add import for the new wizard. Open `wizards/__init__.py` and add:

```python
from . import edi_seed_stores
```

**Step 4: Add button to trading partner form (`views/edi_trading_partner_views.xml`)**

In the form view's header or in the Configuration section (after FTP config), add a button visible only when `order_split_mode == 'per_store'`:

```xml
<button name="%(mml_edi.action_edi_seed_stores_wizard)d"
        string="Seed Store Partners"
        type="action"
        invisible="order_split_mode != 'per_store'"
        groups="mml_edi.group_edi_manager"
        help="Create missing Briscoes store contacts as child delivery addresses. Safe to run multiple times — skips existing records."/>
```

Place this in the `<header>` of the form, or as a button in the FTP Configuration group.

**Step 5: Update `__manifest__.py`** — add the new wizard view XML to `data` list

Add `"wizards/edi_seed_stores_views.xml"` after `"wizards/edi_bulk_action_views.xml"`:

```python
        "wizards/edi_bulk_action_views.xml",
        "wizards/edi_seed_stores_views.xml",
```

**Step 6: Verify XML is well-formed**

```bash
python -c "
import xml.etree.ElementTree as ET
ET.parse('wizards/edi_seed_stores_views.xml')
ET.parse('views/edi_trading_partner_views.xml')
ET.parse('__manifest__.py'[:-3])  # skip
print('XML OK')
"
# Simpler:
python -c "
import xml.etree.ElementTree as ET
for f in ['wizards/edi_seed_stores_views.xml', 'views/edi_trading_partner_views.xml']:
    ET.parse(f)
    print(f'{f}: XML OK')
"
```

**Step 7: Syntax check**

```bash
python -c "import ast, pathlib; ast.parse(pathlib.Path('wizards/edi_seed_stores.py').read_text()); print('syntax OK')"
```

**Step 8: Commit**

```bash
git add wizards/edi_seed_stores.py wizards/edi_seed_stores_views.xml wizards/__init__.py views/edi_trading_partner_views.xml __manifest__.py
git commit -m "feat(edi): add store seeder wizard for Briscoes store partner creation"
```

---

## Task 9: Write `test_processor.py`

Missing from Phase 1. Covers the full pipeline using mock parser data.

**Files:**
- Create: `tests/test_processor.py`

**Step 1: Create `tests/test_processor.py`**

```python
# mml.edi/tests/test_processor.py
"""
Full EDI processing pipeline tests.

Covers:
- Happy path: clean order → auto-approved SO
- Problem order → pending_review with blocking issues
- Duplicate file hash → skipped
- Duplicate SO ref → skipped
- Change order → always pending_review
- carton_qty passes through ParsedOrderLine without error (field stored on line)

Note: these tests use the BriscoesParser stub (mock data) via process_parsed_order().
They test the PIPELINE, not the parser.

Run with: ./odoo-bin --test-enable -d <db> --test-tags mml_edi
"""
from datetime import date, timedelta

from odoo.tests.common import TransactionCase

from .common import (
    EDITestSetup,
    make_clean_parsed_order,
    make_price_discrepancy_parsed_order,
    make_product_not_found_parsed_order,
    make_change_order_parsed_order,
    make_parsed_line,
)
from mml_edi.parsers.base_parser import ParsedOrder, ParsedOrderLine


class TestEDIProcessor(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        # Enable auto-confirm for clean-order tests
        self.trading_partner.write({
            "auto_confirm_clean": True,
            "price_tolerance_pct": 0.0,
        })

    def _run(self, order, filename="test.edi", file_hash=None):
        """Convenience: process one ParsedOrder through the pipeline."""
        if file_hash is None:
            import hashlib
            file_hash = hashlib.sha256((order.raw_data or "test").encode()).hexdigest()
        self.processor.process_parsed_order(order, self.trading_partner, filename, file_hash)

    # ── Happy path ────────────────────────────────────────────────────────

    def test_clean_order_auto_approved(self):
        """
        A clean order (product found, price matches) is auto-approved when
        auto_confirm_clean=True.  SO is confirmed, review is auto_approved.
        """
        order = make_clean_parsed_order(po_number="PROC-CLEAN-001")
        self._run(order)

        review = self.env["edi.order.review"].search([
            ("customer_po_number", "=", "PROC-CLEAN-001"),
        ])
        self.assertEqual(len(review), 1)
        self.assertEqual(review.state, "auto_approved")

        so = review.sale_order_id
        self.assertTrue(so, "SO should be created")
        self.assertEqual(so.state, "sale", "SO should be confirmed (state=sale)")
        self.assertEqual(len(so.order_line), 1)

    def test_clean_order_no_auto_confirm_goes_pending(self):
        """
        With auto_confirm_clean=False, even clean orders go to pending_review.
        """
        self.trading_partner.auto_confirm_clean = False
        order = make_clean_parsed_order(po_number="PROC-NOAUTO-001")
        self._run(order)

        review = self.env["edi.order.review"].search([
            ("customer_po_number", "=", "PROC-NOAUTO-001"),
        ])
        self.assertEqual(review.state, "pending_review")
        so = review.sale_order_id
        self.assertEqual(so.state, "draft", "SO should remain draft")

    # ── Blocking issues ───────────────────────────────────────────────────

    def test_price_discrepancy_goes_pending(self):
        """
        An order with price discrepancy routes to pending_review with a blocking issue.
        """
        order = make_price_discrepancy_parsed_order(po_number="PROC-PRICE-001")
        self._run(order)

        review = self.env["edi.order.review"].search([
            ("customer_po_number", "=", "PROC-PRICE-001"),
        ])
        self.assertEqual(review.state, "pending_review")
        self.assertGreater(review.blocking_issue_count, 0)
        blocking = review.issue_ids.filtered(lambda i: i.severity == "blocking")
        self.assertTrue(
            any(i.issue_type == "price_discrepancy" for i in blocking),
            "Price discrepancy blocking issue expected",
        )

    def test_product_not_found_goes_pending(self):
        """
        An order with an unknown product code routes to pending_review.
        No SO line is created for the unknown product.
        """
        order = make_product_not_found_parsed_order(po_number="PROC-NOTFOUND-001")
        self._run(order)

        review = self.env["edi.order.review"].search([
            ("customer_po_number", "=", "PROC-NOTFOUND-001"),
        ])
        self.assertEqual(review.state, "pending_review")
        blocking = review.issue_ids.filtered(lambda i: i.issue_type == "product_not_found")
        self.assertEqual(len(blocking), 1)
        so = review.sale_order_id
        self.assertEqual(len(so.order_line), 0, "No SO line for unknown product")

    # ── Deduplication ─────────────────────────────────────────────────────

    def test_duplicate_file_hash_skipped(self):
        """
        A file with a hash already in edi.log as successfully processed is skipped.
        No second SO or review is created.
        """
        order = make_clean_parsed_order(po_number="PROC-DEDUP-001")
        file_hash = "aabbccdd" * 8  # 64 hex chars

        # Seed a log entry that marks this hash as already processed
        self.env["edi.log"].log(
            self.trading_partner, "inbound", "file_download", "success",
            "Previously downloaded",
            file_hash=file_hash,
        )

        # Process with same hash — should skip
        self.processor._process_file(
            b"content", file_hash, "test.edi", self.trading_partner
        )

        reviews = self.env["edi.order.review"].search([
            ("customer_po_number", "=", "PROC-DEDUP-001"),
        ])
        self.assertEqual(len(reviews), 0, "No review created for duplicate file")

    def test_duplicate_so_ref_skipped(self):
        """
        If an SO with the same client_order_ref already exists, the order is skipped
        and no second SO or review is created.
        """
        order = make_clean_parsed_order(po_number="PROC-DUPSO-001")

        # Create an existing SO with the same client ref
        self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "client_order_ref": "PROC-DUPSO-001",  # same as po_number (single mode)
        })

        self._run(order, file_hash="unique_hash_for_dupso_test_001")

        reviews = self.env["edi.order.review"].search([
            ("customer_po_number", "=", "PROC-DUPSO-001"),
        ])
        self.assertEqual(len(reviews), 0, "No review created for duplicate SO ref")

    # ── Change orders ─────────────────────────────────────────────────────

    def test_change_order_always_pending_review(self):
        """
        A change order always routes to pending_review, even with auto_confirm_clean=True.
        """
        # First create the original SO
        original = make_clean_parsed_order(po_number="PROC-CHANGE-001")
        self._run(original, file_hash="hash_original_001")

        original_review = self.env["edi.order.review"].search([
            ("customer_po_number", "=", "PROC-CHANGE-001"),
            ("document_type", "=", "new_order"),
        ])
        self.assertTrue(original_review.sale_order_id, "Original SO must exist")

        # Now process change order
        change = make_change_order_parsed_order(po_number="PROC-CHANGE-001", qty=20.0)
        self._run(change, file_hash="hash_change_001")

        change_review = self.env["edi.order.review"].search([
            ("customer_po_number", "=", "PROC-CHANGE-001"),
            ("document_type", "=", "change_order"),
        ])
        self.assertEqual(len(change_review), 1)
        self.assertEqual(change_review.state, "pending_review",
                         "Change orders must always go to pending_review")

    # ── Field passthrough ─────────────────────────────────────────────────

    def test_carton_qty_does_not_break_pipeline(self):
        """
        carton_qty on ParsedOrderLine passes through the pipeline without error.
        (Field stored on parsed line; no crash even without a dedicated SO field.)
        """
        order = ParsedOrder(
            po_number="PROC-CARTON-001",
            order_date=date.today(),
            lines=[
                ParsedOrderLine(
                    product_code="TEST001",
                    description="Test product with carton qty",
                    quantity=24.0,
                    unit_price=9.99,
                    line_number=1,
                    carton_qty=6.0,  # 6 units per carton
                )
            ],
            document_type="new_order",
            raw_data="MOCK_CARTON_EDI",
        )

        # Should not raise
        self._run(order)

        review = self.env["edi.order.review"].search([
            ("customer_po_number", "=", "PROC-CARTON-001"),
        ])
        self.assertEqual(len(review), 1, "Review should be created")
```

**Step 2: Run the new tests**

```bash
./odoo-bin --test-enable -d <db> --test-tags mml_edi.TestEDIProcessor
```
Expected: All tests PASS (no new code to write — this tests existing pipeline behaviour).

**Step 3: Run full suite — no regressions**

```bash
./odoo-bin --test-enable -d <db> --test-tags mml_edi
```
Expected: All tests PASS.

**Step 4: Commit**

```bash
git add tests/test_processor.py
git commit -m "test(edi): add test_processor.py covering full pipeline, dedup, change orders"
```

---

## Task 10: Update conftest + final verification

Ensure the conftest registers any new parser modules needed for standalone tests.

**Files:**
- Modify: `tests/conftest.py`

**Step 1: Check if `briscoes_idoc.py` needs conftest registration**

The conftest currently registers `edi_ftp.py` and `edi_service.py` for standalone (no-Odoo) tests. The new `briscoes_idoc.py` parser uses only dataclasses (no Odoo) — it should import cleanly.

Test:
```bash
python -c "
import sys, os
sys.path.insert(0, 'E:/ClaudeCode/projects/mml.odoo.apps/briscoes.edi/mml.edi')
from parsers.briscoes_idoc import BriscoesIDOCParser
print('import OK')
"
```

If this fails, add a `_register_module` call for `briscoes_idoc` in conftest after the existing `briscoes` module registration (if briscoes is registered there). Likely no change needed.

**Step 2: Run standalone pytest tests** (non-Odoo, via conftest)

```bash
cd "E:/ClaudeCode/projects/mml.odoo.apps/briscoes.edi/mml.edi"
python -m pytest tests/test_ftp_handler.py tests/test_edi_service.py -v
```
Expected: All PASS.

**Step 3: Final full Odoo test run**

```bash
./odoo-bin --test-enable -d <db> --test-tags mml_edi
```
Expected: All tests PASS — no failures, no errors.

**Step 4: Commit if any conftest changes were needed**

```bash
git add tests/conftest.py
git commit -m "test(edi): update conftest for briscoes_idoc parser registration"
```

If no changes, skip this commit.

---

## Task 11: Security — add wizard access rule

**Files:**
- Modify: `security/ir.model.access.csv`

**Step 1: Add wizard access rules**

Append to `security/ir.model.access.csv`:

```csv
access_edi_seed_stores_wizard_manager,edi.seed.stores.wizard manager,model_edi_seed_stores_wizard,mml_edi.group_edi_manager,1,1,1,1
```

(Transient wizards don't need user-level access — only manager can run the seeder.)

**Step 2: Verify CSV is valid**

```bash
python -c "
import csv
with open('security/ir.model.access.csv') as f:
    rows = list(csv.DictReader(f))
print(f'{len(rows)} access rules, last: {rows[-1][\"name\"]}')
"
```

**Step 3: Commit**

```bash
git add security/ir.model.access.csv
git commit -m "security(edi): add access rule for store seeder wizard"
```

---

## Task 12: Push to GitHub

**Step 1: Verify git log**

```bash
cd "E:/ClaudeCode/projects/mml.odoo.apps/briscoes.edi/mml.edi"
git log --oneline -12
```

**Step 2: Push**

```bash
git push origin master
```

**Step 3: Verify on GitHub**

Check https://github.com/JonaldM/mml.edi.odoo that all commits are present.

---

## Summary of All Files Changed

| File | Action |
|---|---|
| `parsers/base_parser.py` | Extend `ParsedOrderLine` with 3 new optional fields |
| `parsers/briscoes_idoc.py` | **NEW** — iDOC parser stub (Phase 1 mock) |
| `models/edi_trading_partner.py` | Add `idoc_xml` to `edi_format` selection |
| `models/edi_processor.py` | Cascade `_find_product()`, new `_lookup_by_strategy()`, set `sol.edi_matched_by` |
| `models/edi_order_issue.py` | Add `product_matched_by_fallback` issue type |
| `models/sale_order.py` | Add `edi_matched_by` to `SaleOrderLine` |
| `views/edi_trading_partner_views.xml` | Pricelist UX, store seeder button |
| `views/edi_order_review_views.xml` | Show `edi_matched_by` in SO lines tab |
| `wizards/edi_seed_stores.py` | **NEW** — store seeder wizard |
| `wizards/edi_seed_stores_views.xml` | **NEW** — wizard view |
| `wizards/__init__.py` | Import new wizard |
| `__manifest__.py` | Add wizard view XML to data list |
| `security/ir.model.access.csv` | Add wizard access rule |
| `tests/common.py` | Add `make_fallback_lookup_order()` helper |
| `tests/test_cascade_lookup.py` | **NEW** — 4 cascade lookup tests |
| `tests/test_processor.py` | **NEW** — 7 full pipeline tests |
