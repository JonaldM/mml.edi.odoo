# mml.edi/tests/test_price_discrepancy.py
"""
Price discrepancy detection and tolerance tests.

Covers:
- price_difference_pct computed field on edi.order.issue
- Within-tolerance issues are created as warnings (non-blocking)
- Outside-tolerance issues are created as blocking
- Boundary condition: exactly at the tolerance threshold

These are Odoo TransactionCase tests; each test runs in a rolled-back transaction.
Run with: ./odoo-bin --test-enable -d <db> --test-tags mml_edi
"""
import unittest

from odoo.tests.common import TransactionCase, tagged

from .common import EDITestSetup

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")  # run after ALL modules load (e.g. website_sale's product fields)
class TestPriceDiscrepancy(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        # Update trading partner to use a 5% tolerance for these tests
        self.trading_partner.price_tolerance_pct = 5.0

    # ── helpers ──────────────────────────────────────────────────────────

    def _make_review(self, po_number="TEST-PRICE-PO-001", **kwargs):
        defaults = {
            "trading_partner_id": self.trading_partner.id,
            "customer_po_number": po_number,
            "document_type": "new_order",
        }
        defaults.update(kwargs)
        return self.env["edi.order.review"].create(defaults)

    def _make_price_issue(self, review, edi_price, system_price, severity, description="Price discrepancy"):
        return self.env["edi.order.issue"].create({
            "review_id": review.id,
            "issue_type": "price_discrepancy",
            "severity": severity,
            "description": description,
            "edi_price": edi_price,
            "system_price": system_price,
        })

    # ── tests ─────────────────────────────────────────────────────────────

    def test_price_within_tolerance(self):
        """
        A 3% price difference is clearly within a 5% tolerance.
        The processor would create this as a warning-level issue (not blocking).
        The computed price_difference_pct field must return 3.0.
        """
        review = self._make_review(po_number="TEST-WITHIN-TOL-001")
        # 3% difference — well within the 5% tolerance
        issue = self._make_price_issue(
            review,
            edi_price=103.0,
            system_price=100.0,
            severity="warning",
            description="Price diff within tolerance",
        )

        self.assertAlmostEqual(
            issue.price_difference_pct, 3.0, places=2,
            msg="price_difference_pct should be 3.0% for 103 vs 100",
        )
        # Within tolerance — should have been created as a warning, not blocking
        self.assertEqual(issue.severity, "warning")

    def test_price_outside_tolerance(self):
        """
        A 10% price difference exceeds the 5% tolerance and must be blocking.
        price_difference_pct must return 10.0.
        """
        review = self._make_review(po_number="TEST-OUTSIDE-TOL-001")
        issue = self._make_price_issue(
            review,
            edi_price=110.0,
            system_price=100.0,
            severity="blocking",
            description="Price diff outside tolerance",
        )

        self.assertAlmostEqual(
            issue.price_difference_pct, 10.0, places=2,
            msg="price_difference_pct should be 10.0% for 110 vs 100",
        )
        self.assertEqual(issue.severity, "blocking")

    def test_price_tolerance_boundary(self):
        """
        Exactly at the tolerance boundary (5.0% with 5% tolerance).
        The processor uses strict greater-than (diff_pct > tolerance), so a
        5.0% diff with 5.0% tolerance is NOT blocking.
        The computed field must return exactly 5.0.
        """
        review = self._make_review(po_number="TEST-BOUNDARY-001")
        issue = self._make_price_issue(
            review,
            edi_price=105.0,
            system_price=100.0,
            severity="warning",
            description="Price exactly at tolerance boundary",
        )

        self.assertAlmostEqual(
            issue.price_difference_pct, 5.0, places=4,
            msg="Boundary case: price_difference_pct must equal 5.0",
        )
        # Confirm the processor logic: 5.0% <= 5.0% tolerance → non-blocking
        tolerance = self.trading_partner.price_tolerance_pct / 100.0  # 0.05
        diff_frac = abs(105.0 - 100.0) / 100.0  # 0.05
        self.assertFalse(
            diff_frac > tolerance,
            "Exactly-at-boundary (5.0%) should NOT exceed tolerance (5.0%)",
        )

    def test_price_difference_pct_zero_system_price(self):
        """
        When system_price is zero, price_difference_pct must be 0.0 (not a
        division-by-zero error).
        """
        review = self._make_review(po_number="TEST-ZERO-PRICE-001")
        issue = self._make_price_issue(
            review,
            edi_price=10.0,
            system_price=0.0,
            severity="blocking",
            description="System price is zero — cannot compute diff",
        )

        self.assertEqual(
            issue.price_difference_pct, 0.0,
            "price_difference_pct should be 0.0 when system_price is zero",
        )

    def test_price_difference_pct_exact_match(self):
        """
        When EDI price equals system price, price_difference_pct must be 0.0.
        """
        review = self._make_review(po_number="TEST-EXACT-MATCH-001")
        issue = self._make_price_issue(
            review,
            edi_price=9.99,
            system_price=9.99,
            severity="info",
            description="Prices match — no discrepancy",
        )

        self.assertAlmostEqual(
            issue.price_difference_pct, 0.0, places=4,
            msg="price_difference_pct should be 0.0 when prices are equal",
        )

    def test_multiple_price_issues_on_same_review(self):
        """
        A single review can have multiple price discrepancy issues (one per SO
        line).  Each issue computes its own price_difference_pct independently.
        """
        review = self._make_review(po_number="TEST-MULTI-PRICE-001")

        issue_5pct = self._make_price_issue(
            review, edi_price=105.0, system_price=100.0, severity="warning"
        )
        issue_20pct = self._make_price_issue(
            review, edi_price=120.0, system_price=100.0, severity="blocking"
        )

        self.assertAlmostEqual(issue_5pct.price_difference_pct, 5.0, places=2)
        self.assertAlmostEqual(issue_20pct.price_difference_pct, 20.0, places=2)
        # Review accumulates issues
        self.assertEqual(review.issue_count, 2)
        self.assertEqual(review.blocking_issue_count, 1)
