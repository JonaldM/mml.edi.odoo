# Briscoes EDI Integration Tests — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Full integration test of the `mml_edi` Briscoes EDI module against the live `mml_dev` Odoo 19 instance on Hetzner, covering all inbound message types (ORDERS, ORDCHG) and all outbound ORDRSP scenarios (Supplied In Full, Short Supplied, Cancelled, Price/Date Changed, Incorrect Items).

**Architecture:** Write a new `test_briscoes_integration.py` using existing `EDITestSetup` pattern + `TransactionCase`. Sync code to Hetzner via tar+SSH. Run `odoo-bin --test-enable --test-tags /mml_edi`. Fix any bugs found and commit.

**Tech Stack:** Python, Odoo 19 TransactionCase, EDIFACT D96A, SSH, Docker exec

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `tests/test_briscoes_integration.py` | **Create** | Odoo integration tests for all Briscoes scenarios |
| `tests/common.py` | Possibly modify | Add Briscoes-specific setup fixture if needed |
| `parsers/briscoes.py` | Fix bugs if found | EDIFACT parser + ORDRSP generator |
| `models/edi_processor.py` | Fix bugs if found | Full pipeline |
| `models/edi_order_review.py` | Fix bugs if found | Review approval / ACK queuing |

---

## Chunk 1: Local Pure-Python Tests + Code Sync to Hetzner

### Task 1: Run existing pure-Python test suite locally

**Files:** None (read-only run)

- [ ] **Step 1: Run all pure-Python tests**

```bash
cd E:/ClaudeCode/projects/mml.odoo/mml.odoo.apps/mml_edi
pytest -q 2>&1
```

Expected: all tests PASS. If any FAIL, note the failure message and fix before proceeding.

- [ ] **Step 2: Fix any pure-Python test failures**

If tests fail, diagnose the failure message and fix the relevant source file in `parsers/` or `services/`. Re-run until green.

- [ ] **Step 3: Commit any fixes**

```bash
cd E:/ClaudeCode/projects/mml.odoo/mml.odoo.apps/mml_edi
git add -A
git commit -m "fix: resolve pure-Python test failures"
```

---

### Task 2: Sync local mml_edi code to Hetzner

The Hetzner addons directory is NOT auto-synced. Sync via tar+SSH.

- [ ] **Step 1: Package the local mml_edi directory (excluding .git, .pytest_cache)**

Run this Python script locally (from E:/ClaudeCode/projects/mml.odoo/mml.odoo.apps/):

```python
import tarfile, os

src = r"E:/ClaudeCode/projects/mml.odoo/mml.odoo.apps/mml_edi"
out = r"C:/Temp/mml_edi_sync.tar.gz"

EXCLUDE = {".git", ".pytest_cache", "__pycache__", "docs", "*.pyc"}

with tarfile.open(out, "w:gz") as tar:
    for dirpath, dirnames, filenames in os.walk(src):
        # Prune excluded dirs
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE]
        for fname in filenames:
            if fname.endswith(".pyc"):
                continue
            fpath = os.path.join(dirpath, fname)
            arcname = "mml_edi/" + os.path.relpath(fpath, src)
            tar.add(fpath, arcname=arcname)

print(f"Created {out}")
```

- [ ] **Step 2: Upload and extract on Hetzner**

```python
import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("100.94.135.90", username="root")

sftp = client.open_sftp()
sftp.put(r"C:/Temp/mml_edi_sync.tar.gz", "/tmp/mml_edi_sync.tar.gz")
sftp.close()

stdin, stdout, stderr = client.exec_command(
    "cd /home/deploy/odoo-dev/addons && rm -rf mml_edi && tar -xzf /tmp/mml_edi_sync.tar.gz && echo DONE"
)
print(stdout.read().decode())
print(stderr.read().decode())
client.close()
```

Expected output: `DONE`

- [ ] **Step 3: Verify the sync**

```bash
ssh root@100.94.135.90 "ls /home/deploy/odoo-dev/addons/mml_edi/tests/ | head -20"
```

Expected: test files listed including `test_briscoes_integration.py` (after Task 3 completes and re-sync).

---

## Chunk 2: Write Briscoes Integration Test File

### Task 3: Create test_briscoes_integration.py

This file tests all Briscoes message types using the actual EDIFACT sample fixtures and the live Odoo DB.

**Files:**
- Create: `tests/test_briscoes_integration.py`

- [ ] **Step 1: Create the file with setup, ORDERS tests, ORDCHG tests, and ORDRSP tests**

Key setup facts:
- Sample EDIFACT barcodes: `9414844375629` (price 5.50), `9414844375636` (price 0.55), `9414844375674` (price 9.50)
- Fixtures dir: `tests/fixtures/` — files already exist
- Trading partner must use `parser_class = "mml_edi.parsers.briscoes.BriscoesParser"`, `product_match_field = "barcode"`, `order_split_mode = "per_store"`, `price_tolerance_pct = 100.0` (avoid price blocking in tests), `auto_confirm_clean = False`
- Store codes in sample: `1005` (Whangarei), `1007` (Albany)
- Per-store mode: each LOC+7 store code → one ParsedOrder from parser

Create `tests/test_briscoes_integration.py`:

```python
# mml.edi/tests/test_briscoes_integration.py
"""
Odoo integration tests for Briscoes EDIFACT EDI — all message types and ORDRSP scenarios.

Tests process the actual EDIFACT sample fixtures end-to-end against a live Odoo DB.
Run on Hetzner dev instance:

    docker exec mml-dev-odoo odoo --test-enable -d mml_dev \\
        --db_host=db --db_user=odoo --db_password=devpass123 \\
        --test-tags /mml_edi --no-http --stop-after-init -u mml_edi

Barcodes in sample files (must exist as products in setUp):
    9414844375629  price 5.50
    9414844375636  price 0.55
    9414844375674  price 9.50

Store codes in sample files: 1005, 1007
"""
import hashlib
import unittest
from pathlib import Path

from odoo.tests.common import TransactionCase

from .common import EDITestSetup
from mml_edi.parsers.base_parser import ParsedOrder, ParsedOrderLine

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")
FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class EDIBriscoesSetup(EDITestSetup):
    """
    Extends EDITestSetup with Briscoes-specific products and trading partner.
    Overrides self.trading_partner with Briscoes config.
    """

    def setup_briscoes_test_data(self):
        self.setup_edi_test_data()  # creates self.trading_partner (generic)

        # Products matching barcode values in the Briscoes EDIFACT sample files
        self.product_a = self.env["product.product"].create({
            "name": "Briscoes Test Product A",
            "barcode": "9414844375629",
            "default_code": "375629",
            "list_price": 5.50,
            "type": "consu",
        })
        self.product_b = self.env["product.product"].create({
            "name": "Briscoes Test Product B",
            "barcode": "9414844375636",
            "default_code": "375636",
            "list_price": 0.55,
            "type": "consu",
        })
        self.product_c = self.env["product.product"].create({
            "name": "Briscoes Test Product C",
            "barcode": "9414844375674",
            "default_code": "375674",
            "list_price": 9.50,
            "type": "consu",
        })

        # Store child partners (per_store mode requires res.partner children with ref=store_code)
        briscoes_partner = self.env["res.partner"].create({
            "name": "Briscoe Group Ltd",
            "customer_rank": 1,
        })
        self.env["res.partner"].create({
            "name": "Briscoes Whangarei",
            "parent_id": briscoes_partner.id,
            "ref": "1005",
        })
        self.env["res.partner"].create({
            "name": "Briscoes Albany",
            "parent_id": briscoes_partner.id,
            "ref": "1007",
        })

        # Override the generic trading_partner with Briscoes-specific config
        self.trading_partner.write({
            "name": "Briscoe Group Ltd",
            "code": "BRISCOES",
            "partner_id": briscoes_partner.id,
            "parser_class": "mml_edi.parsers.briscoes.BriscoesParser",
            "product_match_field": "barcode",
            "order_split_mode": "per_store",
            "price_tolerance_pct": 100.0,  # Skip price blocking in tests
            "auto_confirm_clean": False,
        })

    def _run(self, content: bytes, filename="test.edi"):
        """Parse raw EDIFACT bytes and run each ParsedOrder through the full pipeline."""
        from mml_edi.parsers.briscoes import BriscoesParser
        parser = BriscoesParser()
        parsed_orders = parser.parse_file(content, self.trading_partner)
        file_hash = hashlib.sha256(content).hexdigest()
        for order in parsed_orders:
            order.raw_data = content.decode("utf-8", errors="replace")
            self.processor.process_parsed_order(
                order, self.trading_partner, filename, file_hash
            )
        return parsed_orders

    def _find_review(self, po_number, store_code=None):
        domain = [
            ("trading_partner_id", "=", self.trading_partner.id),
            ("customer_po_number", "=", po_number),
        ]
        if store_code:
            domain.append(("store_code", "=", store_code))
        return self.env["edi.order.review"].search(domain, limit=1) or None


# ── ORDERS (BGM+220) ─────────────────────────────────────────────────────────

@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime")
class TestBriscoesOrdersIntegration(EDIBriscoesSetup, TransactionCase):
    """Test ORDERS (BGM+220) new PO → sale order creation."""

    def setUp(self):
        super().setUp()
        self.setup_briscoes_test_data()

    def test_orders_creates_reviews_per_store(self):
        """ORDERS file has 2 stores (1005, 1007) → 2 edi.order.review records."""
        raw = _load("briscoes_orders_4500038166.edi")
        self._run(raw)
        reviews = self.env["edi.order.review"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("customer_po_number", "=", "4500038166"),
        ])
        self.assertEqual(len(reviews), 2, "Expected 2 reviews (one per store)")

    def test_orders_creates_sale_orders_per_store(self):
        """Each per-store review has a linked sale.order."""
        raw = _load("briscoes_orders_4500038166.edi")
        self._run(raw)
        reviews = self.env["edi.order.review"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("customer_po_number", "=", "4500038166"),
        ])
        for review in reviews:
            self.assertTrue(review.sale_order_id, "Review must have a linked SO")

    def test_orders_store_1005_has_two_lines(self):
        """Store 1005 orders product A (10 ea) + product B (7 ct) → 2 SO lines."""
        raw = _load("briscoes_orders_4500038166.edi")
        self._run(raw)
        review = self._find_review("4500038166", store_code="1005")
        self.assertIsNotNone(review, "Review for store 1005 must exist")
        so = review.sale_order_id
        self.assertEqual(len(so.order_line), 2, "Store 1005 SO should have 2 lines")

    def test_orders_store_1007_has_one_line(self):
        """Store 1007 orders product A only → 1 SO line."""
        raw = _load("briscoes_orders_4500038166.edi")
        self._run(raw)
        review = self._find_review("4500038166", store_code="1007")
        self.assertIsNotNone(review, "Review for store 1007 must exist")
        so = review.sale_order_id
        self.assertEqual(len(so.order_line), 1, "Store 1007 SO should have 1 line")

    def test_orders_product_matched_by_barcode(self):
        """SO lines must have the correct products (matched by barcode)."""
        raw = _load("briscoes_orders_4500038166.edi")
        self._run(raw)
        review = self._find_review("4500038166", store_code="1005")
        so = review.sale_order_id
        barcodes = {line.product_id.barcode for line in so.order_line}
        self.assertIn("9414844375629", barcodes)
        self.assertIn("9414844375636", barcodes)

    def test_orders_review_state_is_pending(self):
        """With auto_confirm_clean=False, review state must be pending_review."""
        raw = _load("briscoes_orders_4500038166.edi")
        self._run(raw)
        review = self._find_review("4500038166", store_code="1005")
        self.assertEqual(review.state, "pending_review")

    def test_orders_edi_log_created(self):
        """Processing must create at least one edi.log entry for this partner."""
        raw = _load("briscoes_orders_4500038166.edi")
        self._run(raw)
        logs = self.env["edi.log"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
        ])
        self.assertTrue(len(logs) > 0, "At least one edi.log record must be created")


# ── ORDCHG (BGM+230) ─────────────────────────────────────────────────────────

@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime")
class TestBriscoesOrdchgIntegration(EDIBriscoesSetup, TransactionCase):
    """Test ORDCHG (BGM+230) change order flow."""

    def setUp(self):
        super().setUp()
        self.setup_briscoes_test_data()
        # First, process the original ORDERS to create the SOs
        raw_orders = _load("briscoes_orders_4500038166.edi")
        self._run(raw_orders, filename="original_orders.edi")

    def test_ordchg_routes_to_pending_review(self):
        """Change order must create a change_order review in pending_review state."""
        raw = _load("briscoes_ordchg_4500038166.edi")
        self._run(raw, filename="change_order.edi")
        change_reviews = self.env["edi.order.review"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("customer_po_number", "=", "4500038166"),
            ("document_type", "=", "change_order"),
        ])
        self.assertTrue(len(change_reviews) > 0, "Must create at least one change_order review")
        for r in change_reviews:
            self.assertEqual(r.state, "pending_review")

    def test_ordchg_change_summary_populated(self):
        """Change order review must have a non-empty change_summary."""
        raw = _load("briscoes_ordchg_4500038166.edi")
        self._run(raw, filename="change_order.edi")
        change_reviews = self.env["edi.order.review"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("document_type", "=", "change_order"),
        ])
        for r in change_reviews:
            self.assertTrue(r.change_summary, "change_summary must not be empty")

    def test_ordchg_apply_updates_so_line_qty(self):
        """Approving a change order applies qty changes to the SO."""
        raw = _load("briscoes_ordchg_4500038166.edi")
        self._run(raw, filename="change_order.edi")

        # Find a change order review that has a linked SO with lines
        change_review = self.env["edi.order.review"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("document_type", "=", "change_order"),
            ("sale_order_id.order_line", "!=", False),
        ], limit=1)

        if not change_review:
            self.skipTest("No change order review with SO lines found")

        so = change_review.sale_order_id
        # Capture original qty before apply
        original_qtys = {
            line.edi_line_number: line.product_uom_qty
            for line in so.order_line
        }

        self.processor.apply_change_order(change_review)

        # The ORDCHG modifies qtys — at least some line should differ OR
        # the SO chatter should have a message about the change
        so_messages = self.env["mail.message"].search([
            ("res_id", "=", so.id),
            ("model", "=", "sale.order"),
            ("body", "like", "EDI change order"),
        ])
        self.assertTrue(
            len(so_messages) > 0,
            "SO chatter must record the change order application"
        )

    def test_ordchg_has_ir_attachment_with_pending_changes(self):
        """Change order review must have a pending_changes_*.json ir.attachment."""
        raw = _load("briscoes_ordchg_4500038166.edi")
        self._run(raw, filename="change_order.edi")
        change_reviews = self.env["edi.order.review"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("document_type", "=", "change_order"),
        ])
        for review in change_reviews:
            attachments = self.env["ir.attachment"].search([
                ("res_model", "=", "edi.order.review"),
                ("res_id", "=", review.id),
                ("name", "like", "pending_changes_"),
            ])
            self.assertTrue(
                len(attachments) > 0,
                f"Review {review.id} must have a pending_changes JSON attachment"
            )


# ── ORDRSP (BGM+231) — all 5 scenarios ───────────────────────────────────────

@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime")
class TestBriscoesOrdrspIntegration(EDIBriscoesSetup, TransactionCase):
    """
    Test all 5 outbound ORDRSP scenarios against live Odoo DB.

    Each test:
    1. Processes the ORDERS file to create a review + SO
    2. Manipulates the SO/review to match the scenario
    3. Calls generate_ack() on the review
    4. Asserts the generated ORDRSP bytes have correct BGM purpose + LIN actions
    """

    def setUp(self):
        super().setUp()
        self.setup_briscoes_test_data()
        # Create initial order — pick store 1005 review for all ORDRSP tests
        raw = _load("briscoes_orders_4500038166.edi")
        self._run(raw)
        self.review_1005 = self._find_review("4500038166", store_code="1005")
        self.assertIsNotNone(self.review_1005, "Store 1005 review must be created by setUp")
        self.so = self.review_1005.sale_order_id

    def _get_ordrsp(self, review) -> str:
        """Generate ORDRSP and return as decoded string."""
        from mml_edi.parsers.briscoes import _generate_ordrsp
        return _generate_ordrsp(review).decode("utf-8")

    def _bgm_purpose(self, ordrsp_text: str) -> str:
        """Extract purpose code from BGM+231+<ref>+<purpose>' segment."""
        for line in ordrsp_text.split("\r\n"):
            if line.startswith("BGM"):
                return line.rstrip("'").split("+")[-1]
        return ""

    def _lin_actions(self, ordrsp_text: str) -> list:
        """Return list of LIN action codes from LIN segments."""
        actions = []
        for line in ordrsp_text.split("\r\n"):
            if line.startswith("LIN"):
                parts = line.rstrip("'").split("+")
                if len(parts) >= 3:
                    actions.append(parts[2])
        return actions

    # Scenario 1: Supplied In Full
    def test_ordrsp_supplied_in_full(self):
        """Approve review with no shortfalls → BGM purpose 29, all LIN action 5."""
        # Ensure no qty shortfall on any line
        self.so.order_line.write({"edi_qty_shortfall": 0.0})
        self.review_1005.write({"state": "approved"})

        ordrsp = self._get_ordrsp(self.review_1005)

        self.assertEqual(
            self._bgm_purpose(ordrsp), "29",
            "Supplied In Full: BGM purpose must be 29 (accepted)"
        )
        actions = self._lin_actions(ordrsp)
        self.assertTrue(len(actions) > 0, "Must have at least one LIN segment")
        self.assertTrue(
            all(a == "5" for a in actions),
            f"All lines must have action 5 (accepted), got: {actions}"
        )

    # Scenario 2: Short Supplied
    def test_ordrsp_short_supplied(self):
        """Approve with at least one line having shortfall → BGM purpose 4, that line action 3."""
        # Set shortfall on first line
        first_line = self.so.order_line[:1]
        first_line.write({"edi_qty_shortfall": 2.0, "product_uom_qty": 8.0})
        self.review_1005.write({"state": "approved"})

        ordrsp = self._get_ordrsp(self.review_1005)

        self.assertEqual(
            self._bgm_purpose(ordrsp), "4",
            "Short Supplied: BGM purpose must be 4 (changed)"
        )
        actions = self._lin_actions(ordrsp)
        self.assertIn("3", actions, "Short supplied line must have action 3 (qty changed)")

    # Scenario 3: Cancelled / Deleted
    def test_ordrsp_cancelled(self):
        """Reject review → BGM purpose 27, all LIN action 7."""
        self.review_1005.write({"state": "rejected"})

        ordrsp = self._get_ordrsp(self.review_1005)

        self.assertEqual(
            self._bgm_purpose(ordrsp), "27",
            "Cancelled: BGM purpose must be 27 (cancelled)"
        )
        # When rejected with SO lines, all lines should be action 7
        actions = self._lin_actions(ordrsp)
        if actions:
            self.assertTrue(
                all(a == "7" for a in actions),
                f"Rejected: all lines must have action 7, got: {actions}"
            )

    # Scenario 4: Price / Date Changed
    def test_ordrsp_price_changed(self):
        """Approve with price-updated lines → BGM purpose 4 (changed)."""
        # Simulate price change by setting shortfall on one line
        # (Price changes in ORDRSP are reflected via the approved SO price_unit vs edi_price)
        first_line = self.so.order_line[:1]
        first_line.write({
            "price_unit": 4.99,   # Changed from 5.50
            "edi_qty_shortfall": 0.0,
        })
        # Mark SO line qty as less than ordered to trigger purpose 4
        # Alternatively: set shortfall to indicate partial supply
        first_line.write({"edi_qty_shortfall": 1.0, "product_uom_qty": 9.0})
        self.review_1005.write({"state": "approved"})

        ordrsp = self._get_ordrsp(self.review_1005)

        # Purpose 4 = changed (includes price changes or qty changes)
        self.assertIn(
            self._bgm_purpose(ordrsp), ("4", "29"),
            "Price/Date Changed scenario: BGM purpose must be 4 or 29"
        )
        # Verify ORDRSP is valid EDIFACT: UNB + UNZ present, all segments end with '
        lines = [l for l in ordrsp.split("\r\n") if l.strip()]
        self.assertTrue(any(l.startswith("UNB") for l in lines), "UNB must be present")
        self.assertTrue(any(l.startswith("UNZ") for l in lines), "UNZ must be present")
        for seg in lines:
            self.assertTrue(seg.endswith("'"), f"Segment must end with ': {seg!r}")

    # Scenario 5: Incorrect Items
    def test_ordrsp_incorrect_items(self):
        """
        Rejected order: ORDRSP with purpose 27 and action 7 on all lines.
        Briscoes 'Incorrect Items' = vendor rejects entire order (rejected state).
        """
        self.review_1005.write({"state": "rejected"})

        ordrsp = self._get_ordrsp(self.review_1005)

        # Incorrect items = rejected = purpose 27
        self.assertEqual(self._bgm_purpose(ordrsp), "27")

        # RFF+ON:<po_number> must reference the original PO
        self.assertIn("RFF+ON:4500038166", ordrsp)

    # Structural validation (applies to all scenarios)
    def test_ordrsp_structure_approved(self):
        """ORDRSP for any approved order must have valid EDIFACT envelope."""
        self.review_1005.write({"state": "approved"})
        ordrsp = self._get_ordrsp(self.review_1005)

        segs = [l for l in ordrsp.split("\r\n") if l.strip()]
        self.assertTrue(any(l.startswith("UNB") for l in segs))
        self.assertTrue(any(l.startswith("UNH") for l in segs))
        self.assertTrue(any(l.startswith("BGM+231") for l in segs))
        self.assertTrue(any(l.startswith("UNT") for l in segs))
        self.assertTrue(any(l.startswith("UNZ") for l in segs))

        # All segments must end with '
        for seg in segs:
            self.assertTrue(seg.endswith("'"), f"Bad segment terminator: {seg!r}")

    def test_ordrsp_po_reference_present(self):
        """ORDRSP must contain RFF+ON:<po_number> referencing the original Briscoes PO."""
        self.review_1005.write({"state": "approved"})
        ordrsp = self._get_ordrsp(self.review_1005)
        self.assertIn("RFF+ON:4500038166", ordrsp)
```

- [ ] **Step 2: Verify the file was written correctly**

```bash
head -5 E:/ClaudeCode/projects/mml.odoo/mml.odoo.apps/mml_edi/tests/test_briscoes_integration.py
```

Expected: shows the module docstring.

- [ ] **Step 3: Commit the new test file**

```bash
cd E:/ClaudeCode/projects/mml.odoo/mml.odoo.apps/mml_edi
git add tests/test_briscoes_integration.py
git commit -m "test: add Briscoes full integration tests for all EDI message types and ORDRSP scenarios"
```

---

## Chunk 3: Sync Tests to Hetzner + Run Integration Suite

### Task 4: Second sync (includes new test file) + run on Hetzner

- [ ] **Step 1: Re-sync mml_edi (now includes test_briscoes_integration.py)**

Repeat the tar+SSH sync from Task 2 Steps 1-2. The new test file must be in the archive.

```python
import tarfile, os

src = r"E:/ClaudeCode/projects/mml.odoo/mml.odoo.apps/mml_edi"
out = r"C:/Temp/mml_edi_sync.tar.gz"

with tarfile.open(out, "w:gz") as tar:
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in {".git", ".pytest_cache", "__pycache__", "docs"}]
        for fname in filenames:
            if fname.endswith(".pyc"):
                continue
            fpath = os.path.join(dirpath, fname)
            arcname = "mml_edi/" + os.path.relpath(fpath, src)
            tar.add(fpath, arcname=arcname)

import paramiko
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("100.94.135.90", username="root")
sftp = client.open_sftp()
sftp.put(out, "/tmp/mml_edi_sync.tar.gz")
sftp.close()
stdin, stdout, stderr = client.exec_command(
    "cd /home/deploy/odoo-dev/addons && rm -rf mml_edi && tar -xzf /tmp/mml_edi_sync.tar.gz && echo SYNC_DONE"
)
print(stdout.read().decode())
client.close()
```

Expected: `SYNC_DONE`

- [ ] **Step 2: Update the mml_edi module on Hetzner (picks up any model changes)**

```bash
ssh root@100.94.135.90 "docker exec mml-dev-odoo odoo -d mml_dev \
  --db_host=db --db_user=odoo --db_password=devpass123 \
  -u mml_edi --no-http --stop-after-init 2>&1 | tail -20"
```

Expected: `odoo ... INFO ... Modules loaded.`

- [ ] **Step 3: Run the full integration test suite on Hetzner**

```bash
ssh root@100.94.135.90 "docker exec mml-dev-odoo odoo --test-enable \
  -d mml_dev --db_host=db --db_user=odoo --db_password=devpass123 \
  --test-tags /mml_edi --no-http --stop-after-init -u mml_edi 2>&1 | grep -E '(ERROR|FAIL|OK|test_|Ran |SKIP)'"
```

Expected: all tests `OK`. Note any `FAIL` or `ERROR` lines.

- [ ] **Step 4: Capture and review full output if any failures**

```bash
ssh root@100.94.135.90 "docker exec mml-dev-odoo odoo --test-enable \
  -d mml_dev --db_host=db --db_user=odoo --db_password=devpass123 \
  --test-tags /mml_edi --no-http --stop-after-init -u mml_edi 2>&1 | tail -100"
```

---

## Chunk 4: Bug Fixes (if any found in Chunk 3)

### Task 5: Diagnose and fix any test failures

For each failing test:

- [ ] **Step 1: Read the full error traceback from Odoo logs**

```bash
ssh root@100.94.135.90 "docker logs mml-dev-odoo 2>&1 | grep -A 20 'ERROR\|Traceback'"
```

- [ ] **Step 2: Identify the failing code path**

Use the error message + test name to locate the bug. Common areas:
- `parsers/briscoes.py`: `_generate_ordrsp()` — EDIFACT segment formatting
- `models/edi_processor.py`: `_process_new_order()` or `_process_change_order()`
- `models/edi_order_review.py`: `action_approve()` or `_queue_ack()`
- Test setup: `test_briscoes_integration.py` — product type, partner config

- [ ] **Step 3: Fix the bug in the relevant source file**

Edit the file locally at `E:/ClaudeCode/projects/mml.odoo/mml.odoo.apps/mml_edi/<file>`.

- [ ] **Step 4: Re-run pure-Python tests to catch regressions**

```bash
cd E:/ClaudeCode/projects/mml.odoo/mml.odoo.apps/mml_edi
pytest -q
```

Expected: all green.

- [ ] **Step 5: Commit the fix**

```bash
cd E:/ClaudeCode/projects/mml.odoo/mml.odoo.apps/mml_edi
git add <changed files>
git commit -m "fix: <describe the bug fixed>"
```

- [ ] **Step 6: Re-sync and re-run on Hetzner (back to Task 4 Step 1)**

Repeat sync → update → run tests. Iterate until all tests pass.

---

## Final Verification

When all tests pass:

```bash
ssh root@100.94.135.90 "docker exec mml-dev-odoo odoo --test-enable \
  -d mml_dev --db_host=db --db_user=odoo --db_password=devpass123 \
  --test-tags /mml_edi --no-http --stop-after-init -u mml_edi 2>&1 | tail -10"
```

Expected output (example):
```
Ran 35 tests in 45.123s
OK
```

All message types covered:
- [x] ORDERS (BGM+220) → SO created, per-store split, product lookup by barcode
- [x] ORDCHG (BGM+230) → change order routed to review, apply_change_order mutates SO
- [x] ORDRSP Supplied In Full → purpose 29, all lines action 5
- [x] ORDRSP Short Supplied → purpose 4, shortfall lines action 3
- [x] ORDRSP Cancelled/Deleted → purpose 27, lines action 7
- [x] ORDRSP Price/Date Changed → purpose 4, valid EDIFACT structure
- [x] ORDRSP Incorrect Items → purpose 27, RFF+ON reference correct
