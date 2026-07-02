"""Tests for the approve-time availability re-clamp (go-live FIX 4 / SS-1).

Pure parts only: the per-line clamp decision (_reclamp_line) and the
oos_policy gate of reclamp_order_lines. The clamp math must be identical to
the parse-time gate (_short_ship_split): ship = min(ordered, max(avail, 0)),
shortfall = ordered - ship.
"""
from types import SimpleNamespace


class TestReclampLineMath:

    def test_stock_dropped_reclamps_down(self):
        from mml_edi.models.edi_processor import _reclamp_line
        new_qty, shortfall, changed = _reclamp_line(10.0, 1.0, 4.0)
        assert new_qty == 1.0
        assert shortfall == 9.0
        assert changed is True

    def test_stock_arrived_reclamps_back_up_to_ordered(self):
        from mml_edi.models.edi_processor import _reclamp_line
        new_qty, shortfall, changed = _reclamp_line(10.0, 100.0, 4.0)
        assert new_qty == 10.0
        assert shortfall == 0.0
        assert changed is True

    def test_unchanged_when_availability_matches_current(self):
        from mml_edi.models.edi_processor import _reclamp_line
        new_qty, shortfall, changed = _reclamp_line(10.0, 4.0, 4.0)
        assert new_qty == 4.0
        assert shortfall == 6.0
        assert changed is False

    def test_negative_availability_clamps_to_zero_never_negative(self):
        from mml_edi.models.edi_processor import _reclamp_line
        new_qty, shortfall, changed = _reclamp_line(10.0, -3.0, 4.0)
        assert new_qty == 0.0
        assert shortfall == 10.0
        assert changed is True

    def test_full_supply_line_stays_untouched(self):
        from mml_edi.models.edi_processor import _reclamp_line
        new_qty, shortfall, changed = _reclamp_line(10.0, 50.0, 10.0)
        assert new_qty == 10.0
        assert shortfall == 0.0
        assert changed is False


class TestReclampCapToCurrent:
    """Approve-with-Corrections (must-fix 2): an operator's manual reduction
    is deliberate — cap_to_current=True may only move the line DOWN from it,
    never restore it toward the customer's ordered qty. Shortfall stays
    relative to the ORDERED qty (the ORDRSP reports against the request)."""

    def test_operator_reduction_is_preserved_when_stock_is_plentiful(self):
        from mml_edi.models.edi_processor import _reclamp_line
        # operator cut 10 -> 4; 10 in stock; WITHOUT the cap this would
        # restore to 10 (the bug: shipping the qty the human rejected)
        new_qty, shortfall, changed = _reclamp_line(
            10.0, 10.0, 4.0, cap_to_current=True)
        assert new_qty == 4.0
        assert shortfall == 6.0
        assert changed is False

    def test_operator_reduction_still_clamps_further_down_on_shortage(self):
        from mml_edi.models.edi_processor import _reclamp_line
        new_qty, shortfall, changed = _reclamp_line(
            10.0, 2.0, 4.0, cap_to_current=True)
        assert new_qty == 2.0
        assert shortfall == 8.0
        assert changed is True

    def test_cap_never_raises_above_ordered_even_if_operator_did(self):
        from mml_edi.models.edi_processor import _reclamp_line
        # operator (or a bad edit) set 15 with 12 ordered: basis caps at
        # min(ordered, current) = 12, availability allows it
        new_qty, shortfall, changed = _reclamp_line(
            12.0, 100.0, 15.0, cap_to_current=True)
        assert new_qty == 12.0
        assert shortfall == 0.0
        assert changed is True

    def test_default_path_still_restores_up_when_stock_arrived(self):
        from mml_edi.models.edi_processor import _reclamp_line
        # regression: plain approve keeps the ordered-basis up-restore
        new_qty, shortfall, changed = _reclamp_line(
            10.0, 100.0, 4.0, cap_to_current=False)
        assert new_qty == 10.0
        assert shortfall == 0.0
        assert changed is True


class TestReclampPolicyGate:

    def test_backorder_partner_returns_empty_without_touching_lines(self):
        """Contract: for oos_policy == 'backorder' the helper must return []
        WITHOUT reading or writing any SO line. A bare object() sale order
        would raise AttributeError on any access — proving nothing is touched.
        """
        from mml_edi.models.edi_processor import EDIProcessor
        proc = EDIProcessor()
        partner = SimpleNamespace(
            _fields={"oos_policy": object()}, oos_policy="backorder")
        assert proc.reclamp_order_lines(object(), partner) == []
