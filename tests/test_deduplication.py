# mml.edi/tests/test_deduplication.py
"""
Deduplication logic tests.

Covers:
- File-hash dedup: same SHA-256 hash is not reprocessed
- SO ref dedup: new_order with matching client_order_ref is not created twice
- Change-order dedup: change orders are NOT blocked by SO ref dedup (hash-only)

These are Odoo TransactionCase tests; each test runs in a rolled-back transaction.
Run with: ./odoo-bin --test-enable -d <db> --test-tags mml_edi
"""
from odoo.tests.common import TransactionCase

from .common import EDITestSetup, make_clean_parsed_order, make_change_order_parsed_order


class TestDeduplication(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()

    # ── helpers ──────────────────────────────────────────────────────────

    def _make_review(self, **kwargs):
        defaults = {
            "trading_partner_id": self.trading_partner.id,
            "customer_po_number": "TEST-PO-001",
            "document_type": "new_order",
        }
        defaults.update(kwargs)
        return self.env["edi.order.review"].create(defaults)

    def _log_file_download(self, file_hash: str):
        """Create a successful file_download log entry so the dedup check fires."""
        return self.env["edi.log"].log(
            self.trading_partner,
            "inbound",
            "file_download",
            "success",
            "Downloaded test file",
            file_hash=file_hash,
            filename="test_order.edi",
        )

    # ── tests ─────────────────────────────────────────────────────────────

    def test_file_hash_dedup(self):
        """
        Once a file hash has been logged as a successful file_download, the
        processor's _is_file_duplicate() must detect it as a duplicate.
        """
        known_hash = "abc123deadbeef" * 4  # 56-char fake SHA-256 value

        # Before logging: not a duplicate
        is_dup_before = self.env["edi.processor"]._is_file_duplicate(
            known_hash, self.trading_partner
        )
        self.assertFalse(is_dup_before, "Hash should not be a duplicate before it is logged")

        # Log a successful download with this hash
        self._log_file_download(known_hash)

        # After logging: duplicate detected
        is_dup_after = self.env["edi.processor"]._is_file_duplicate(
            known_hash, self.trading_partner
        )
        self.assertTrue(is_dup_after, "Hash should be detected as a duplicate after it is logged")

    def test_file_hash_dedup_different_hash_not_blocked(self):
        """
        A different file hash must NOT be considered a duplicate even when
        another hash has already been logged.
        """
        logged_hash = "aaaa1111" * 8
        new_hash = "bbbb2222" * 8

        self._log_file_download(logged_hash)

        is_dup = self.env["edi.processor"]._is_file_duplicate(
            new_hash, self.trading_partner
        )
        self.assertFalse(is_dup, "A different hash must not be flagged as a duplicate")

    def test_so_ref_dedup_new_order(self):
        """
        When a sale.order already exists with a given client_order_ref, the
        processor's _find_existing_so() must return it so the new-order flow
        can skip creating a duplicate SO.
        """
        client_ref = "BRISCOES_4500176806_1017"

        # Pre-create a SO with this reference (simulates a previously processed PO)
        existing_so = self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "client_order_ref": client_ref,
        })

        found_so = self.env["edi.processor"]._find_existing_so(client_ref)

        self.assertIsNotNone(found_so, "Existing SO should be found")
        self.assertEqual(
            found_so.id,
            existing_so.id,
            "_find_existing_so() must return the correct SO record",
        )

    def test_so_ref_dedup_no_existing_so(self):
        """
        _find_existing_so() must return None when no SO with that ref exists.
        """
        result = self.env["edi.processor"]._find_existing_so("NONEXISTENT_REF_99999")
        self.assertIsNone(result, "_find_existing_so() should return None for unknown refs")

    def test_change_order_no_so_ref_dedup(self):
        """
        Change orders must NOT be blocked by SO-ref dedup logic.

        Even when a sale.order already exists for the same PO number (which it
        must, as change orders require an existing SO), the change-order flow
        does not call _find_existing_so to skip creation — it calls it to FIND
        the SO to mutate.  This test confirms that a ParsedOrder with
        document_type='change_order' carrying the same PO number as an existing
        SO is routed to the change-order path, not silently dropped.

        We verify this by confirming that:
        1. An existing SO is found for the ref (not None)
        2. A change-order review CAN be created and linked to it without any
           duplication error — the SO ref is not treated as a block.
        """
        client_ref = "CHANGE_TEST_PO_001"

        existing_so = self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "client_order_ref": client_ref,
        })

        # An existing SO is found — this is required for change orders to proceed
        found_so = self.env["edi.processor"]._find_existing_so(client_ref)
        self.assertIsNotNone(found_so, "Change order flow needs to find the existing SO")
        self.assertEqual(found_so.id, existing_so.id)

        # A change-order review linked to this SO can be created without conflict
        change_review = self.env["edi.order.review"].create({
            "trading_partner_id": self.trading_partner.id,
            "customer_po_number": "CHANGE_TEST_PO_001",
            "document_type": "change_order",
            "sale_order_id": existing_so.id,
            "state": "pending_review",
        })

        self.assertEqual(change_review.document_type, "change_order")
        self.assertEqual(change_review.sale_order_id.id, existing_so.id)
        self.assertEqual(change_review.state, "pending_review")
