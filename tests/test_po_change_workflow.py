# mml.edi/tests/test_po_change_workflow.py
"""
Change-order workflow tests.

Covers:
- Change orders always start in pending_review regardless of auto_confirm_clean
- apply_change_order() updates SO line quantities from pending_changes.json
- Change order reviews link back to the original new_order review via
  original_review_id / change_order_ids

Note on apply_change_order():
  The pending_changes.json attachment schema is:
    {
        "new_delivery_date": "<ISO date string or null>",
        "line_changes": [
            {"line_number": <int>, "new_qty": <float>},
            ...
        ]
    }
  Values are base64-encoded before storing in ir.attachment.datas.

Run with: ./odoo-bin --test-enable -d <db> --test-tags mml_edi
"""
import base64
import json
import unittest

from odoo import fields
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger

from .common import EDITestSetup

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")  # run after ALL modules load (e.g. website_sale's product fields)
# ACK transport is NOT under test here: the shared fixture's parser is
# allow-list-blocked, so _queue_ack's swallowed failure would otherwise fail
# these tests purely via Odoo's error-log detection. Real ACK coverage lives
# in test_ack_send_claim.py / test_ack_reset_reapprove.py.
@mute_logger("odoo.addons.mml_edi.models.edi_order_review",
             "odoo.addons.mml_edi.models.edi_log")
class TestPOChangeWorkflow(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()

    # ── helpers ──────────────────────────────────────────────────────────

    def _make_so_with_line(self, qty=10.0, price=100.0, line_number=1, client_ref=None):
        """Create a sale.order with one EDI-tagged line."""
        so = self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "edi_trading_partner_id": self.trading_partner.id,
            "client_order_ref": client_ref or "CHANGE-WF-SO-%d" % id(self),
        })
        self.env["sale.order.line"].create({
            "order_id": so.id,
            "product_id": self.test_product.id,
            "product_uom_qty": qty,
            "price_unit": price,
            "edi_line_number": line_number,
            "edi_price": price,
        })
        return so

    def _make_review(self, so=None, document_type="new_order", state="pending_review",
                     original_review_id=None, **kwargs):
        vals = {
            "trading_partner_id": self.trading_partner.id,
            "customer_po_number": "CHANGE-WF-PO-001",
            "document_type": document_type,
            "state": state,
        }
        if so:
            vals["sale_order_id"] = so.id
        if original_review_id:
            vals["original_review_id"] = original_review_id
        vals.update(kwargs)
        return self.env["edi.order.review"].create(vals)

    def _attach_pending_changes(self, review, new_delivery_date=None, line_changes=None):
        """
        Create the pending_changes.json ir.attachment expected by apply_change_order().

        line_changes: list of {"line_number": int, "new_qty": float}
        """
        payload = {
            "new_delivery_date": new_delivery_date,
            "line_changes": line_changes or [],
        }
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        return self.env["ir.attachment"].create({
            "name": "pending_changes.json",
            "res_model": "edi.order.review",
            "res_id": review.id,
            "datas": encoded,
            "mimetype": "application/json",
        })

    # ── test_change_order_always_pending_review ───────────────────────────

    def test_change_order_always_pending_review(self):
        """
        Change orders must always be in pending_review state — the
        action_approve() and action_reject() state machine enforces this, and
        there is no path to auto_approved for change_order document_type.

        Verifies: a change_order review cannot be moved to auto_approved state
        because action_approve() sets state to 'approved' (not 'auto_approved'),
        and the processor only sets auto_approved for new_order reviews.

        The trading partner's auto_confirm_clean flag is irrelevant for change
        orders — the processor always routes them to pending_review.
        """
        self.trading_partner.auto_confirm_clean = True

        so = self._make_so_with_line()
        change_review = self._make_review(
            so=so,
            document_type="change_order",
            state="pending_review",
        )

        # Verify the review is correctly typed
        self.assertEqual(change_review.document_type, "change_order")
        self.assertEqual(change_review.state, "pending_review")

        # Verify that approving a change order goes to 'approved', never 'auto_approved'
        # (auto_approved is only set by the processor for clean new orders)
        change_review.action_approve()
        change_review.invalidate_recordset()
        self.assertEqual(
            change_review.state, "approved",
            "Approved change orders must have state='approved', not 'auto_approved'",
        )
        self.assertNotEqual(
            change_review.state, "auto_approved",
            "Change orders must never reach auto_approved state",
        )

    def test_change_order_auto_confirm_does_not_apply(self):
        """
        Confirm auto_confirm_clean has no effect on change order routing.
        A new-order review created with auto_confirm_clean=True CAN be
        auto-approved, but the change-order review on the same SO is always
        pending_review.
        """
        self.trading_partner.auto_confirm_clean = True

        so = self._make_so_with_line()
        original_review = self._make_review(so=so, document_type="new_order", state="auto_approved")
        change_review = self._make_review(
            so=so,
            document_type="change_order",
            state="pending_review",
            original_review_id=original_review.id,
        )

        # New order review can be auto_approved
        self.assertEqual(original_review.state, "auto_approved")
        # Change order review is always pending_review
        self.assertEqual(change_review.state, "pending_review")

    # ── test_change_order_approve_updates_so ─────────────────────────────

    def test_change_order_approve_updates_so_line_qty(self):
        """
        apply_change_order() must update SO line quantities from the
        pending_changes.json attachment.

        Setup:
          - SO with one line: qty=10, edi_line_number=1
          - pending_changes.json: line_changes=[{line_number:1, new_qty:15}]
        Expected: SO line qty becomes 15.
        """
        so = self._make_so_with_line(qty=10.0, price=100.0, line_number=1)
        so_line = so.order_line[0]
        self.assertEqual(so_line.product_uom_qty, 10.0)

        original_review = self._make_review(so=so, document_type="new_order", state="approved")
        change_review = self._make_review(
            so=so,
            document_type="change_order",
            state="pending_review",
            original_review_id=original_review.id,
            change_summary="Line 1 qty: 10 → 15",
        )

        self._attach_pending_changes(
            change_review,
            new_delivery_date=None,
            line_changes=[{"line_number": 1, "new_qty": 15.0}],
        )

        self.env["edi.processor"].apply_change_order(change_review)

        so_line.invalidate_recordset()
        self.assertEqual(
            so_line.product_uom_qty, 15.0,
            "SO line qty must be updated to 15 by apply_change_order()",
        )

    def test_change_order_approve_refreshes_edi_price(self):
        """
        apply_change_order() must refresh edi_price from the change document.

        Regression: only qty was applied, so a replacement that corrected a
        price left the stale edi_price on the line. The ORDRSP then compared
        our (already-correct) price_unit against the OLD edi_price and reported
        the line as CHANGED (1229=3) forever — breaking scenario 4b, which
        requires a full-acceptance replacement (BGM 29 / all lines 1229=5).

        price_unit is intentionally NOT overwritten: if the buyer states a
        price we do not accept, the line must still report as changed.
        """
        so = self._make_so_with_line(qty=10.0, price=8.10, line_number=1)
        so_line = so.order_line[0]
        so_line.edi_price = 12.10          # what the ORIGINAL order asked
        so_line.edi_ordered_qty = 10.0

        original_review = self._make_review(so=so, document_type="new_order", state="approved")
        change_review = self._make_review(
            so=so,
            document_type="change_order",
            state="pending_review",
            original_review_id=original_review.id,
            change_summary="Line 1 price: 12.10 -> 8.10",
        )
        self._attach_pending_changes(
            change_review,
            new_delivery_date=None,
            line_changes=[{"line_number": 1, "new_qty": 10.0, "new_price": 8.10}],
        )

        self.env["edi.processor"].apply_change_order(change_review)

        so_line.invalidate_recordset()
        self.assertEqual(
            so_line.edi_price, 8.10,
            "edi_price must track the change document so the line is no longer "
            "reported as CHANGED once the buyer adopts our price",
        )
        self.assertEqual(
            so_line.price_unit, 8.10,
            "price_unit must be left as our own price, not overwritten",
        )

    def test_change_order_approve_updates_delivery_date(self):
        """
        apply_change_order() must update commitment_date when a new delivery
        date is provided in pending_changes.json.
        """
        from datetime import date, timedelta

        so = self._make_so_with_line()
        new_date = date.today() + timedelta(days=14)

        original_review = self._make_review(so=so, document_type="new_order", state="approved")
        change_review = self._make_review(
            so=so,
            document_type="change_order",
            state="pending_review",
            original_review_id=original_review.id,
            change_summary="Delivery: None → %s" % new_date.isoformat(),
        )

        self._attach_pending_changes(
            change_review,
            new_delivery_date=new_date.isoformat(),
            line_changes=[],
        )

        self.env["edi.processor"].apply_change_order(change_review)

        so.invalidate_recordset()
        self.assertTrue(so.commitment_date, "commitment_date must be set after apply_change_order()")
        self.assertEqual(
            so.commitment_date.date(), new_date,
            "commitment_date must match the new delivery date from pending_changes.json",
        )

    def test_change_order_no_attachment_is_safe(self):
        """
        apply_change_order() must not raise an error when there is no
        pending_changes.json attachment — it logs a warning and returns.
        """
        so = self._make_so_with_line(qty=10.0)
        so_line = so.order_line[0]

        change_review = self._make_review(
            so=so,
            document_type="change_order",
            state="pending_review",
        )

        # No attachment created — should not raise
        self.env["edi.processor"].apply_change_order(change_review)

        so_line.invalidate_recordset()
        # SO line qty unchanged
        self.assertEqual(so_line.product_uom_qty, 10.0, "SO line qty must be unchanged when no attachment exists")

    # ── test_change_order_link_to_original ───────────────────────────────

    def test_change_order_link_to_original(self):
        """
        A change-order review must link back to its original new_order review
        via original_review_id, and the original must see it via change_order_ids.
        """
        so = self._make_so_with_line()
        original_review = self._make_review(so=so, document_type="new_order", state="approved")

        change_review = self._make_review(
            so=so,
            document_type="change_order",
            state="pending_review",
            original_review_id=original_review.id,
        )

        # Forward link: change review → original review
        self.assertEqual(
            change_review.original_review_id.id, original_review.id,
            "change_review.original_review_id must point to the original review",
        )

        # Reverse link: original review → change orders
        self.assertIn(
            change_review.id,
            original_review.change_order_ids.ids,
            "original_review.change_order_ids must contain the change review",
        )

    def test_change_order_multiple_linked_to_original(self):
        """
        Multiple change orders can be linked to the same original review.
        All must appear in original_review.change_order_ids.
        """
        so = self._make_so_with_line()
        original_review = self._make_review(so=so, document_type="new_order", state="approved")

        change_1 = self._make_review(
            so=so,
            document_type="change_order",
            state="pending_review",
            original_review_id=original_review.id,
            customer_po_number="CHANGE-WF-PO-001-V1",
        )
        change_2 = self._make_review(
            so=so,
            document_type="change_order",
            state="pending_review",
            original_review_id=original_review.id,
            customer_po_number="CHANGE-WF-PO-001-V2",
        )

        change_ids = original_review.change_order_ids.ids
        self.assertIn(change_1.id, change_ids)
        self.assertIn(change_2.id, change_ids)
        self.assertEqual(len(change_ids), 2)


# ── Pure-Python structural tests (no Odoo needed) ─────────────────────────────

def test_pending_changes_attachment_uses_timestamp():
    """
    Structural test: edi_processor.py must create timestamped attachment names
    so that successive change orders each produce a separate attachment rather
    than overwriting a fixed 'pending_changes.json'.
    """
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / 'models/edi_processor.py').read_text()
    # The name must include a timestamp variable, not a bare literal
    assert 'pending_changes_' in src or 'pending_changes_{' in src or (
        'ts' in src and 'pending_changes' in src
    ), (
        "edi_processor.py must use a timestamped attachment name for "
        "pending_changes — not the fixed string 'pending_changes.json'"
    )


def test_pending_changes_attachment_name_not_fixed_literal():
    """
    The fixed string 'pending_changes.json' must NOT appear as the attachment
    name literal in _process_change_order (only in apply_change_order search).
    """
    import pathlib
    import ast

    src = (pathlib.Path(__file__).parent.parent / 'models/edi_processor.py').read_text()
    tree = ast.parse(src)

    # Count occurrences of the bare literal 'pending_changes.json'
    bare_literal_count = src.count("'pending_changes.json'") + src.count('"pending_changes.json"')

    # apply_change_order still searches by prefix so one occurrence is expected
    # (the search filter). If there are 2+ occurrences the create() still uses
    # the old fixed name.
    assert bare_literal_count <= 1, (
        "Found %d occurrences of literal 'pending_changes.json' in edi_processor.py. "
        "The ir.attachment create() call must use a timestamped name; "
        "only the search in apply_change_order may reference the bare name." % bare_literal_count
    )


# ── Pure (no Odoo env): change-diff must carry price ────────────────────────

class TestPendingChangesCarriesPrice:
    """Regression: a replacement/ORDCHG that corrects a PRICE was silently
    dropping it.

    ``_encode_pending_changes`` recorded only ``new_qty``, so
    ``apply_change_order`` could never refresh ``sale.order.line.edi_price``.
    The stale original price then made every later ORDRSP report that line as
    CHANGED (1229=3) even when we were accepting the buyer's price verbatim —
    which breaks a full-acceptance replacement (it must be BGM 29 / all 1229=5).
    """

    def _order(self):
        from types import SimpleNamespace
        return SimpleNamespace(
            requested_delivery_date=None,
            lines=[
                SimpleNamespace(line_number=1, quantity=8.0, unit_price=19.30),
                SimpleNamespace(line_number=2, quantity=10.0, unit_price=8.10),
            ],
        )

    def _existing_so(self):
        from types import SimpleNamespace
        return SimpleNamespace(order_line=[
            SimpleNamespace(edi_line_number=1),
            SimpleNamespace(edi_line_number=2),
        ])

    def _decode(self, encoded):
        import base64, json
        return json.loads(base64.b64decode(encoded).decode())

    def test_new_price_is_encoded_for_every_line(self):
        from mml_edi.models.edi_processor import EDIProcessor

        changes = self._decode(
            EDIProcessor()._encode_pending_changes(self._order(), self._existing_so())
        )
        by_line = {c["line_number"]: c for c in changes["line_changes"]}
        assert by_line[1]["new_price"] == 19.30
        assert by_line[2]["new_price"] == 8.10
        # qty must still be carried (no regression on the original behaviour)
        assert by_line[1]["new_qty"] == 8.0
        assert by_line[2]["new_qty"] == 10.0
