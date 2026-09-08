"""Pure tests for the "Reject line" issue action.

The button was annotation-only: it wrote resolution='rejected' on the
edi.order.issue row and nothing else, so the SO line still confirmed at the
EDI quantity and the outbound ORDRSP acknowledged it as ACCEPTED in full.
These tests pin the real state change the ACK generators read.
"""
import pytest
from odoo.exceptions import UserError

from mml_edi.models.edi_order_issue import EDIOrderIssue


class _FakeUser:

    id = 7


class _FakeEnv:

    def __init__(self):
        self.user = _FakeUser()


class _FakeOrder:

    def __init__(self, state="draft", name="S00042"):
        self.state = state
        self.name = name


class _FakeLine:

    def __init__(self, order, qty=10.0, ordered=10.0, shortfall=0.0):
        self.order_id = order
        self.product_uom_qty = qty
        self.edi_ordered_qty = ordered
        self.edi_qty_shortfall = shortfall

    def write(self, vals):
        for key, value in vals.items():
            setattr(self, key, value)


class _Issue(EDIOrderIssue):

    def __init__(self, line):
        self.env = _FakeEnv()
        self.sale_order_line_id = line
        self.writes = []

    def __iter__(self):
        return iter([self])

    def write(self, vals):
        self.writes.append(vals)


class TestRejectLine:

    def test_reject_zeroes_the_line_and_records_the_shortfall(self):
        line = _FakeLine(_FakeOrder(), qty=10.0, ordered=10.0)
        _Issue(line).action_reject_issue()
        assert line.product_uom_qty == 0.0, (
            "a rejected line must not confirm at the EDI quantity"
        )
        assert line.edi_qty_shortfall == 10.0, (
            "the whole ordered qty is the shortfall, which is what the ACK "
            "generators read to emit a line rejection"
        )
        assert line.edi_ordered_qty == 0.0, (
            "the ordered basis must be zeroed or the approve-time re-clamp "
            "resurrects the line"
        )

    def test_reject_uses_the_shipped_qty_when_no_ordered_qty_is_stored(self):
        line = _FakeLine(_FakeOrder(), qty=6.0, ordered=0.0)
        _Issue(line).action_reject_issue()
        assert line.product_uom_qty == 0.0
        assert line.edi_qty_shortfall == 6.0

    def test_reject_still_records_the_resolution(self):
        line = _FakeLine(_FakeOrder())
        issue = _Issue(line)
        issue.action_reject_issue()
        assert len(issue.writes) == 1
        assert issue.writes[0]["resolution"] == "rejected"
        assert issue.writes[0]["resolved_by"] == 7

    def test_reject_refuses_once_the_order_is_confirmed(self):
        line = _FakeLine(_FakeOrder(state="sale"), qty=10.0, ordered=10.0)
        issue = _Issue(line)
        with pytest.raises(UserError):
            issue.action_reject_issue()
        assert line.product_uom_qty == 10.0, (
            "a confirmed order must not be silently mutated"
        )
        assert issue.writes == [], (
            "the chip must not claim a rejection that did not happen"
        )

    def test_reject_without_a_line_only_records_the_resolution(self):
        issue = _Issue(None)
        issue.action_reject_issue()
        assert issue.writes[0]["resolution"] == "rejected"
