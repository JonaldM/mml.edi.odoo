# mml.edi/tests/test_processor.py
"""
Full pipeline integration tests for EDI processing engine.

Tests process_parsed_order() and _process_file() end-to-end using mock
ParsedOrder objects constructed from common.py helpers.

Covers:
- Clean order + auto_confirm_clean=True → review state auto_approved, SO confirmed
- Clean order + auto_confirm_clean=False → review state pending_review, SO in draft
- Price discrepancy → pending_review with blocking price_discrepancy issue
- Product not found → pending_review with blocking product_not_found issue, 0 SO lines
- Duplicate file hash → _process_file skipped, no review created
- Duplicate SO ref → process_parsed_order skipped, no second review created
- Change order → always pending_review regardless of auto_confirm_clean
- ParsedOrderLine with carton_qty → pipeline runs without error, review created

Note on ACK: auto-approved reviews call _queue_ack() which attempts FTP upload.
All exceptions from _queue_ack() are swallowed by the caller, so FTP failures
in test environments do not affect state assertions.

Note on mml.event: _process_new_order() emits an 'edi.order.processed' event
via self.env['mml.event'].emit(). mml_base (which provides mml.event) is a
declared dependency so it is present in the test environment.

Run with: ./odoo-bin --test-enable -d <db> --test-tags mml_edi
"""
import hashlib
import unittest
from datetime import date

from odoo.tests.common import TransactionCase

from .common import (
    EDITestSetup,
    make_change_order_parsed_order,
    make_clean_parsed_order,
    make_price_discrepancy_parsed_order,
    make_product_not_found_parsed_order,
    make_parsed_line,
)
try:
    from odoo.addons.mml_edi.parsers.base_parser import ParsedOrder, ParsedOrderLine
except ImportError:
    from mml_edi.parsers.base_parser import ParsedOrder, ParsedOrderLine

# True only when the real Odoo TransactionCase (with .env) is present.
# The conftest stub has no 'env' attribute, so this correctly gates the tests.
_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
class TestEDIProcessor(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.trading_partner.write({
            "auto_confirm_clean": True,
            "price_tolerance_pct": 0.0,
        })

    # ── Convenience helpers ───────────────────────────────────────────────

    def _run(self, order, filename="test.edi", file_hash=None):
        """
        Process one ParsedOrder through the full pipeline.
        Computes a deterministic hash from order.raw_data if not provided.
        """
        if file_hash is None:
            raw = order.raw_data or "test"
            file_hash = hashlib.sha256(raw.encode()).hexdigest()
        self.processor.process_parsed_order(
            order, self.trading_partner, filename, file_hash
        )

    def _find_review(self, po_number):
        """Return the edi.order.review for the given PO number, or None if not found.

        Returns Python None (not an empty recordset) so assertIsNotNone() guards
        work correctly. Not suitable for PO numbers with multiple reviews
        (e.g., original + change order) — use a direct search in those cases.
        """
        return self.env["edi.order.review"].search(
            [("customer_po_number", "=", po_number)], limit=1
        ) or None

    def _log_file_download(self, file_hash, filename="test.edi"):
        """Seed a successful file_download log entry (triggers dedup check)."""
        return self.env["edi.log"].log(
            self.trading_partner,
            "inbound",
            "file_download",
            "success",
            "Downloaded test file",
            file_hash=file_hash,
            filename=filename,
        )

    # ── Test 1: Clean order auto-approved ─────────────────────────────────

    def test_clean_order_auto_approved(self):
        """
        A clean order (no issues) with auto_confirm_clean=True must:
        - Create an edi.order.review with state='auto_approved'
        - Confirm the linked SO (state='sale')
        """
        order = make_clean_parsed_order(po_number="PROC-CLEAN-001")
        self._run(order)

        review = self._find_review("PROC-CLEAN-001")
        self.assertIsNotNone(review, "A review must be created for the clean order")
        self.assertEqual(
            review.state,
            "auto_approved",
            "Clean order with auto_confirm_clean=True must be auto_approved",
        )

        so = review.sale_order_id
        self.assertTrue(so, "Review must be linked to a sale order")
        self.assertEqual(
            so.state,
            "sale",
            "SO must be confirmed (state='sale') when auto-approved",
        )

    # ── Test 2: Clean order without auto-confirm goes pending ─────────────

    def test_clean_order_no_auto_confirm_goes_pending(self):
        """
        A clean order with auto_confirm_clean=False must:
        - Create an edi.order.review with state='pending_review'
        - Leave the SO in draft
        """
        self.trading_partner.write({"auto_confirm_clean": False})

        order = make_clean_parsed_order(po_number="PROC-NOAUTO-001")
        self._run(order)

        review = self._find_review("PROC-NOAUTO-001")
        self.assertIsNotNone(review, "A review must be created even without auto-confirm")
        self.assertEqual(
            review.state,
            "pending_review",
            "Review must be pending_review when auto_confirm_clean=False",
        )

        so = review.sale_order_id
        self.assertTrue(so, "Review must be linked to a sale order")
        self.assertEqual(
            so.state,
            "draft",
            "SO must remain in draft when not auto-confirmed",
        )

    # ── Test 3: Price discrepancy goes to pending review ──────────────────

    def test_price_discrepancy_goes_pending(self):
        """
        An order with a price that doesn't match the pricelist must:
        - Create an edi.order.review with state='pending_review'
        - Have at least one blocking issue of type 'price_discrepancy'
        """
        # price_tolerance_pct=0.0 from setUp — any deviation is blocking
        order = make_price_discrepancy_parsed_order(po_number="PROC-PRICE-001")
        self._run(order)

        review = self._find_review("PROC-PRICE-001")
        self.assertIsNotNone(review, "A review must be created for a price discrepancy order")
        self.assertEqual(
            review.state,
            "pending_review",
            "Price discrepancy must route review to pending_review",
        )

        blocking_issues = review.issue_ids.filtered(
            lambda i: i.issue_type == "price_discrepancy" and i.severity == "blocking"
        )
        self.assertTrue(
            len(blocking_issues) > 0,
            "At least one blocking price_discrepancy issue must be created",
        )

    # ── Test 4: Product not found goes to pending review ──────────────────

    def test_product_not_found_goes_pending(self):
        """
        An order with an unknown product code must:
        - Create an edi.order.review with state='pending_review'
        - Have a blocking 'product_not_found' issue
        - Create 0 SO lines (no SO line when product is not matched)
        """
        order = make_product_not_found_parsed_order(po_number="PROC-NOTFOUND-001")
        self._run(order)

        review = self._find_review("PROC-NOTFOUND-001")
        self.assertIsNotNone(review, "A review must be created even when product is not found")
        self.assertEqual(
            review.state,
            "pending_review",
            "Product not found must route review to pending_review",
        )

        not_found_issues = review.issue_ids.filtered(
            lambda i: i.issue_type == "product_not_found" and i.severity == "blocking"
        )
        self.assertTrue(
            len(not_found_issues) > 0,
            "At least one blocking product_not_found issue must be created",
        )

        so = review.sale_order_id
        self.assertTrue(so, "Review must be linked to a sale order even on product-not-found")
        self.assertEqual(
            len(so.order_line),
            0,
            "No SO lines must be created when the product is not found",
        )

    # ── Test 5: Duplicate file hash is skipped ────────────────────────────

    def test_duplicate_file_hash_skipped(self):
        """
        When a file hash has already been logged as a successful file_download,
        _process_file() must skip processing and create no new review.

        The dedup check in _process_file looks for edi.log entries with:
            event_type='file_download', status='success', file_hash=<hash>
        """
        known_hash = hashlib.sha256(b"DUPLICATE_FILE_CONTENT").hexdigest()

        # Seed the log entry so the dedup check fires
        self._log_file_download(known_hash, filename="duplicate.edi")

        # Count reviews before
        reviews_before = self.env["edi.order.review"].search_count(
            [("trading_partner_id", "=", self.trading_partner.id)]
        )

        # Attempt to process a file with the same hash
        self.processor._process_file(
            b"DUPLICATE_FILE_CONTENT",
            known_hash,
            "duplicate.edi",
            self.trading_partner,
        )

        reviews_after = self.env["edi.order.review"].search_count(
            [("trading_partner_id", "=", self.trading_partner.id)]
        )

        self.assertEqual(
            reviews_before,
            reviews_after,
            "_process_file must not create any review when file hash is already logged",
        )

    # ── Test 6: Duplicate SO ref is skipped ───────────────────────────────

    def test_duplicate_so_ref_skipped(self):
        """
        When a sale.order already exists with a matching client_order_ref,
        process_parsed_order() must skip creation of a duplicate review and SO.

        The client_ref is rendered by partner.render_client_ref(po_number, store_code).
        With client_ref_template='{po_number}' and order_split_mode='single',
        the ref equals the po_number directly.
        """
        po_number = "PROC-DEDUP-SO-001"

        # Pre-create an SO with the ref that would be generated for this PO
        existing_so = self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "client_order_ref": po_number,
            "edi_trading_partner_id": self.trading_partner.id,
        })

        # Count reviews before
        reviews_before = self.env["edi.order.review"].search_count(
            [("trading_partner_id", "=", self.trading_partner.id)]
        )

        order = make_clean_parsed_order(po_number=po_number)
        self._run(order, filename="dedup_so.edi")

        reviews_after = self.env["edi.order.review"].search_count(
            [("trading_partner_id", "=", self.trading_partner.id)]
        )

        self.assertEqual(
            reviews_before,
            reviews_after,
            "process_parsed_order must not create a review when an SO with the same ref already exists",
        )

    # ── Test 7: Change order always goes to pending review ─────────────────

    def test_change_order_always_pending_review(self):
        """
        Change orders must always route to pending_review — even when
        auto_confirm_clean=True.

        Steps:
        1. Process the original new_order → SO is auto_approved and confirmed
        2. Process a change order for the same PO number
        3. The change order review must be pending_review
        """
        po_number = "PROC-CHANGE-001"

        # Step 1: Process original order
        original_order = make_clean_parsed_order(po_number=po_number)
        self._run(original_order, filename="original.edi")

        original_review = self._find_review(po_number)
        self.assertIsNotNone(original_review, "Original review must be created")
        self.assertEqual(
            original_review.state,
            "auto_approved",
            "Original order must be auto_approved (prerequisite for change order test)",
        )

        # Step 2: Process change order
        change_order = make_change_order_parsed_order(po_number=po_number, qty=20.0)
        self._run(change_order, filename="change.edi")

        # Step 3: Find the change order review (document_type='change_order')
        change_reviews = self.env["edi.order.review"].search([
            ("customer_po_number", "=", po_number),
            ("document_type", "=", "change_order"),
        ])

        self.assertTrue(
            len(change_reviews) > 0,
            "A change_order review must be created",
        )

        change_review = change_reviews[0]
        self.assertEqual(
            change_review.state,
            "pending_review",
            "Change orders must always be routed to pending_review, "
            "regardless of auto_confirm_clean",
        )
        self.assertEqual(
            change_review.document_type,
            "change_order",
            "Change order review must have document_type='change_order'",
        )

    # ── Test 8: carton_qty field does not break pipeline ──────────────────

    def test_carton_qty_does_not_break_pipeline(self):
        """
        A ParsedOrderLine with carton_qty set must be processed without error.
        The pipeline should create a review and SO line normally — carton_qty
        is stored on the dataclass but is currently informational only.
        """
        order = ParsedOrder(
            po_number="PROC-CARTON-001",
            order_date=date.today(),
            lines=[
                ParsedOrderLine(
                    product_code="9780000000002",   # matches self.test_product.barcode
                    description="Carton Test Product",
                    quantity=12.0,
                    unit_price=9.99,
                    line_number=1,
                    carton_qty=6.0,           # 2 cartons of 6 units
                )
            ],
            document_type="new_order",
            raw_data="MOCK_CARTON_EDI",
        )

        # Must not raise
        self._run(order, filename="carton_test.edi")

        review = self._find_review("PROC-CARTON-001")
        self.assertIsNotNone(review, "A review must be created for a line with carton_qty")

        so = review.sale_order_id
        self.assertTrue(so, "Review must be linked to a sale order")

        so_lines = so.order_line
        self.assertTrue(
            len(so_lines) > 0,
            "At least one SO line must be created when carton_qty is present",
        )
        self.assertAlmostEqual(
            so_lines[0].product_uom_qty,
            12.0,
            places=2,
            msg="SO line qty must reflect the ordered quantity, not carton_qty",
        )
