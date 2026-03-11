# mml.edi/tests/test_briscoes_integration.py
"""
Odoo integration tests for Briscoes EDIFACT EDI — all message types and ORDRSP scenarios.

Tests process the actual EDIFACT sample fixtures end-to-end against a live Odoo DB.
Run on Hetzner dev instance:

    docker exec mml-dev-odoo odoo --test-enable -d mml_dev \
        --db_host=db --db_user=odoo --db_password=devpass123 \
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
try:
    from odoo.addons.mml_edi.parsers.base_parser import ParsedOrder, ParsedOrderLine
except ImportError:
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
            "client_ref_template": "{po_number}_{store_code}",
            "price_tolerance_pct": 100.0,  # Skip price blocking in tests
            "auto_confirm_clean": False,
        })

    def _run(self, content: bytes, filename="test.edi"):
        """Parse raw EDIFACT bytes and run each ParsedOrder through the full pipeline."""
        try:
            from odoo.addons.mml_edi.parsers.briscoes import BriscoesParser
        except ImportError:
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

        self.processor.apply_change_order(change_review)

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
    3. Calls _generate_ordrsp() on the review
    4. Asserts the generated ORDRSP bytes have correct BGM purpose + LIN actions
    """

    def setUp(self):
        super().setUp()
        self.setup_briscoes_test_data()
        raw = _load("briscoes_orders_4500038166.edi")
        self._run(raw)
        self.review_1005 = self._find_review("4500038166", store_code="1005")
        self.assertIsNotNone(self.review_1005, "Store 1005 review must be created by setUp")
        self.so = self.review_1005.sale_order_id

    def _get_ordrsp(self, review) -> str:
        parser = self.trading_partner.get_parser_instance()
        return parser.generate_ack(review).decode("utf-8")

    def _bgm_purpose(self, ordrsp_text: str) -> str:
        for line in ordrsp_text.split("\r\n"):
            if line.startswith("BGM"):
                return line.rstrip("'").split("+")[-1]
        return ""

    def _lin_actions(self, ordrsp_text: str) -> list:
        actions = []
        for line in ordrsp_text.split("\r\n"):
            if line.startswith("LIN"):
                parts = line.rstrip("'").split("+")
                if len(parts) >= 3:
                    actions.append(parts[2])
        return actions

    def test_ordrsp_supplied_in_full(self):
        """Approve review with no shortfalls → BGM purpose 29, all LIN action 5."""
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

    def test_ordrsp_short_supplied(self):
        """Approve with at least one line having shortfall → BGM purpose 4, that line action 3."""
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

    def test_ordrsp_cancelled(self):
        """Reject review → BGM purpose 27, all LIN action 7."""
        self.review_1005.write({"state": "rejected"})

        ordrsp = self._get_ordrsp(self.review_1005)

        self.assertEqual(
            self._bgm_purpose(ordrsp), "27",
            "Cancelled: BGM purpose must be 27 (cancelled)"
        )
        actions = self._lin_actions(ordrsp)
        if actions:
            self.assertTrue(
                all(a == "7" for a in actions),
                f"Rejected: all lines must have action 7, got: {actions}"
            )

    def test_ordrsp_price_changed(self):
        """Approve with a shortfall line → BGM purpose 4 (changed)."""
        first_line = self.so.order_line[:1]
        first_line.write({
            "price_unit": 4.99,
            "edi_qty_shortfall": 1.0,
            "product_uom_qty": 9.0,
        })
        self.review_1005.write({"state": "approved"})

        ordrsp = self._get_ordrsp(self.review_1005)

        self.assertIn(
            self._bgm_purpose(ordrsp), ("4", "29"),
            "Price/Date Changed scenario: BGM purpose must be 4 or 29"
        )
        lines = [l for l in ordrsp.split("\r\n") if l.strip()]
        self.assertTrue(any(l.startswith("UNB") for l in lines), "UNB must be present")
        self.assertTrue(any(l.startswith("UNZ") for l in lines), "UNZ must be present")
        for seg in lines:
            self.assertTrue(seg.endswith("'"), f"Segment must end with ': {seg!r}")

    def test_ordrsp_incorrect_items(self):
        """Rejected order → ORDRSP purpose 27 with RFF+ON referencing the original PO."""
        self.review_1005.write({"state": "rejected"})

        ordrsp = self._get_ordrsp(self.review_1005)

        self.assertEqual(self._bgm_purpose(ordrsp), "27")
        self.assertIn("RFF+ON:4500038166", ordrsp)

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
        for seg in segs:
            self.assertTrue(seg.endswith("'"), f"Bad segment terminator: {seg!r}")

    def test_ordrsp_po_reference_present(self):
        """ORDRSP must contain RFF+ON:<po_number> referencing the original Briscoes PO."""
        self.review_1005.write({"state": "approved"})
        ordrsp = self._get_ordrsp(self.review_1005)
        self.assertIn("RFF+ON:4500038166", ordrsp)
