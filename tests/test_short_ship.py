"""Tests for the OOS short-ship split math (_short_ship_split).

Given an ordered qty and the DC-available qty, the processor ships the
available qty and acknowledges the rest as a shortfall — it never backorders.
"""


class TestShortShipSplit:

    def test_partial_availability_ships_available_and_shorts_rest(self):
        from mml_edi.models.edi_processor import _short_ship_split
        ship, short = _short_ship_split(12.0, 5.0)
        assert ship == 5.0
        assert short == 7.0

    def test_zero_availability_ships_nothing(self):
        from mml_edi.models.edi_processor import _short_ship_split
        ship, short = _short_ship_split(12.0, 0.0)
        assert ship == 0.0
        assert short == 12.0

    def test_full_availability_ships_all_no_shortfall(self):
        from mml_edi.models.edi_processor import _short_ship_split
        ship, short = _short_ship_split(12.0, 20.0)
        assert ship == 12.0
        assert short == 0.0

    def test_exact_availability_ships_all_no_shortfall(self):
        from mml_edi.models.edi_processor import _short_ship_split
        ship, short = _short_ship_split(12.0, 12.0)
        assert ship == 12.0
        assert short == 0.0

    def test_negative_availability_treated_as_zero(self):
        """Defensive: a negative free_qty (over-reservation) ships nothing,
        never a negative qty."""
        from mml_edi.models.edi_processor import _short_ship_split
        ship, short = _short_ship_split(12.0, -3.0)
        assert ship == 0.0
        assert short == 12.0
