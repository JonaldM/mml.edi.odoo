# mml_edi/tests/test_wall_format.py
"""Pure-Python unit tests for the wall-display shaping helpers.

No Odoo needed — exercises services/wall_format.py directly: alarm-count tone,
poll-freshness tone, and the service-level bar (value / target / clamped width /
marker / RAG tone).
"""
from mml_edi.services.wall_format import (
    alarm_count_tone,
    poll_tone,
    service_bar,
)


class TestAlarmCountTone:
    def test_zero_is_calm(self):
        assert alarm_count_tone(0) == "calm"

    def test_one_and_two_are_amber(self):
        assert alarm_count_tone(1) == "amber"
        assert alarm_count_tone(2) == "amber"

    def test_three_or_more_is_red(self):
        assert alarm_count_tone(3) == "red"
        assert alarm_count_tone(9) == "red"


class TestPollTone:
    def test_never_polled_is_red(self):
        assert poll_tone(None) == "red"

    def test_fresh_within_hour_is_calm(self):
        assert poll_tone(0) == "calm"
        assert poll_tone(60) == "calm"

    def test_up_to_three_hours_is_amber(self):
        assert poll_tone(61) == "amber"
        assert poll_tone(180) == "amber"

    def test_beyond_three_hours_is_red(self):
        assert poll_tone(181) == "red"
        assert poll_tone(1440) == "red"


class TestServiceBar:
    def test_value_and_target_formatting(self):
        bar = service_bar("Auto-approval rate", 78.4, 75, 60)
        assert bar["label"] == "Auto-approval rate"
        assert bar["value"] == "78.4%"
        assert bar["target"] == "target 75"

    def test_meeting_target_is_green(self):
        assert service_bar("x", 78.4, 75, 60)["tone"] == "green"

    def test_below_target_above_amber_is_amber(self):
        assert service_bar("x", 96.5, 98, 95)["tone"] == "amber"

    def test_below_amber_floor_is_red(self):
        assert service_bar("x", 40.0, 75, 60)["tone"] == "red"

    def test_none_value_renders_dash_with_zero_fill(self):
        bar = service_bar("x", None, 100, 95)
        assert bar["value"] == "—"
        assert bar["pct"] == "0.0%"
        assert bar["tone"] == "none"

    def test_over_target_width_clamped_to_track(self):
        # 100% against a 75 target must not overflow the 0-100 track.
        bar = service_bar("x", 100.0, 75, 60)
        assert bar["pct"] == "100.0%"
        assert bar["mark"] == "75.0%"

    def test_marker_sits_at_target_percent(self):
        bar = service_bar("ORDRSP on-time", 99.2, 100, 95)
        assert bar["pct"] == "99.2%"
        assert bar["mark"] == "100.0%"
