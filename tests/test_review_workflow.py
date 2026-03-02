# mml.edi/tests/test_review_workflow.py
"""
edi.order.review state-machine tests (new_order path).

Covers:
- action_approve(): pending_review → approved, SO confirmed, issues accepted
- action_reject(): pending_review → rejected, SO cancelled
- action_reset_to_review(): approved → pending_review (edi_manager group required)
- action_approve() with blocking issues: issues accepted, review approved

Note on ACK: action_approve() calls _queue_ack() which attempts FTP upload.
The review model already catches all exceptions from _queue_ack(), so FTP
failures in test environments are swallowed and the state change still happens.
Tests assert on state outcomes, not on FTP behaviour.

Run with: ./odoo-bin --test-enable -d <db> --test-tags mml_edi
"""
from odoo import fields
from odoo.tests.common import TransactionCase

from .common import EDITestSetup


class TestReviewWorkflow(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()

    # ── helpers ──────────────────────────────────────────────────────────

    def _make_so(self, state="draft"):
        """Create a minimal draft sale.order for the test customer."""
        so = self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "edi_trading_partner_id": self.trading_partner.id,
            "client_order_ref": "TEST-WF-SO-%s" % fields.Datetime.now(),
        })
        # Add a required order line so action_confirm() does not fail
        self.env["sale.order.line"].create({
            "order_id": so.id,
            "product_id": self.test_product.id,
            "product_uom_qty": 1.0,
            "price_unit": 9.99,
        })
        if state == "sale":
            so.action_confirm()
        elif state == "cancel":
            so.action_cancel()
        return so

    def _make_review(self, so=None, state="pending_review", document_type="new_order", **kwargs):
        vals = {
            "trading_partner_id": self.trading_partner.id,
            "customer_po_number": "TEST-WF-PO-001",
            "document_type": document_type,
            "state": state,
        }
        if so:
            vals["sale_order_id"] = so.id
        vals.update(kwargs)
        return self.env["edi.order.review"].create(vals)

    def _make_blocking_issue(self, review):
        return self.env["edi.order.issue"].create({
            "review_id": review.id,
            "issue_type": "price_discrepancy",
            "severity": "blocking",
            "description": "Blocking price discrepancy for workflow test",
            "edi_price": 110.0,
            "system_price": 100.0,
        })

    # ── test_approve_new_order ────────────────────────────────────────────

    def test_approve_new_order(self):
        """
        action_approve() on a pending_review new_order review must:
        - Set state to 'approved'
        - Record reviewed_by = current user
        - Set reviewed_date
        - Confirm the linked SO (draft → sale)
        """
        so = self._make_so(state="draft")
        review = self._make_review(so=so, state="pending_review")

        self.assertEqual(review.state, "pending_review")
        self.assertEqual(so.state, "draft")
        self.assertFalse(review.reviewed_by)
        self.assertFalse(review.reviewed_date)

        review.action_approve()

        self.assertEqual(review.state, "approved", "Review must be approved")
        self.assertEqual(review.reviewed_by, self.env.user, "reviewed_by must be the current user")
        self.assertTrue(review.reviewed_date, "reviewed_date must be set after approval")
        self.assertEqual(so.state, "sale", "SO must be confirmed on review approval")

    def test_approve_raises_if_not_pending(self):
        """
        action_approve() must raise UserError if the review is not pending_review.
        """
        from odoo.exceptions import UserError
        so = self._make_so()
        review = self._make_review(so=so, state="approved")

        with self.assertRaises(UserError):
            review.action_approve()

    # ── test_reject_new_order ─────────────────────────────────────────────

    def test_reject_new_order(self):
        """
        action_reject() on a pending_review new_order review must:
        - Set state to 'rejected'
        - Cancel the linked SO (draft → cancel)
        """
        so = self._make_so(state="draft")
        review = self._make_review(so=so, state="pending_review")

        self.assertEqual(review.state, "pending_review")
        self.assertEqual(so.state, "draft")

        review.action_reject()

        self.assertEqual(review.state, "rejected", "Review must be rejected")
        self.assertIn(
            so.state, ("cancel",),
            "SO must be cancelled on review rejection",
        )

    def test_reject_raises_if_not_pending(self):
        """
        action_reject() must raise UserError if the review is not pending_review.
        """
        from odoo.exceptions import UserError
        so = self._make_so()
        review = self._make_review(so=so, state="rejected")

        with self.assertRaises(UserError):
            review.action_reject()

    # ── test_reset_to_review ──────────────────────────────────────────────

    def test_reset_to_review(self):
        """
        action_reset_to_review() must:
        - Return the state to 'pending_review'
        - Clear reviewed_by and reviewed_date

        The method requires the edi_manager group.  The admin user (uid=1)
        has all groups by default in test mode, so we run as admin.
        """
        so = self._make_so(state="draft")
        admin_user = self.env.ref("base.user_admin")

        # Create an approved review as admin (admin has all groups)
        review = self._make_review(so=so, state="approved")
        review.write({
            "reviewed_by": admin_user.id,
            "reviewed_date": fields.Datetime.now(),
        })

        # Reset to review as admin
        review_as_admin = review.with_user(admin_user)
        review_as_admin.action_reset_to_review()

        review.invalidate_recordset()
        self.assertEqual(review.state, "pending_review", "State must revert to pending_review")
        self.assertFalse(review.reviewed_by, "reviewed_by must be cleared on reset")
        self.assertFalse(review.reviewed_date, "reviewed_date must be cleared on reset")

    def test_reset_to_review_requires_manager_group(self):
        """
        action_reset_to_review() must raise UserError for a non-manager user.
        """
        from odoo.exceptions import UserError

        so = self._make_so(state="draft")
        review = self._make_review(so=so, state="approved")

        # Create a plain internal user without the edi_manager group
        plain_user = self.env["res.users"].create({
            "name": "Plain EDI User",
            "login": "plain_edi_user_test@test.local",
            "groups_id": [(4, self.env.ref("base.group_user").id)],
        })

        review_as_plain = review.with_user(plain_user)
        with self.assertRaises(UserError):
            review_as_plain.action_reset_to_review()

    # ── test_approve_blocked_by_issues ────────────────────────────────────

    def test_approve_blocked_by_issues(self):
        """
        action_approve() must succeed even when blocking issues exist.
        All pending issues are resolved as 'accepted' during approval.
        """
        so = self._make_so(state="draft")
        review = self._make_review(so=so, state="pending_review")
        blocking_issue = self._make_blocking_issue(review)

        self.assertEqual(blocking_issue.resolution, "pending")
        self.assertEqual(review.blocking_issue_count, 1)

        review.action_approve()

        self.assertEqual(review.state, "approved", "Review must be approved despite blocking issue")
        blocking_issue.invalidate_recordset()  # flush ORM cache after write in action_approve chain
        self.assertEqual(
            blocking_issue.resolution, "accepted",
            "Blocking issue must be resolved as 'accepted' on approval",
        )
        self.assertEqual(so.state, "sale", "SO must be confirmed")
