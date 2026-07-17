# mml_edi/tests/test_mobile_triage_format.py
"""Pure-Python unit tests for the mobile-triage card-action mapping.

No Odoo needed — exercises services/mobile_triage_format.py directly: the
primary/secondary one-tap action pair each attention-item kind maps to.
"""
from mml_edi.services.mobile_triage_format import card_actions


def _actions(item):
    return [(a["action"], a["variant"]) for a in card_actions(item)]


class TestCardActions:
    def test_blocking_is_open_then_reject(self):
        # A blocking review is never one-tap approved — Open (resolve) / Reject.
        assert _actions({"kind": "blocking"}) == [
            ("open", "accent"), ("reject", "danger")]

    def test_ack_failed_is_retry_then_open(self):
        assert _actions({"kind": "ack_failed"}) == [
            ("retry_ack", "accent"), ("open", "neutral")]

    def test_unknown_store_warning_is_open_then_reject(self):
        # A warning carrying the map_store action (unknown store) can't be
        # approved blind — Open to seed the map, or Reject.
        item = {"kind": "warning", "actions": ["map_store"]}
        assert _actions(item) == [("open", "accent"), ("reject", "danger")]

    def test_other_warning_is_approve_then_open(self):
        # A plain warning (stock shortfall etc.) offers one-tap Approve (clamped).
        item = {"kind": "warning", "actions": ["review"]}
        assert _actions(item) == [("approve", "approve"), ("open", "neutral")]

    def test_warning_without_actions_key_defaults_to_approve(self):
        # Missing 'actions' must not raise — defaults to the plain-warning pair.
        assert _actions({"kind": "warning"}) == [
            ("approve", "approve"), ("open", "neutral")]

    def test_every_card_has_exactly_two_actions(self):
        for item in (
            {"kind": "blocking"},
            {"kind": "ack_failed"},
            {"kind": "warning", "actions": ["map_store"]},
            {"kind": "warning", "actions": []},
        ):
            assert len(card_actions(item)) == 2

    def test_labels_are_present(self):
        labels = [a["label"] for a in card_actions({"kind": "blocking"})]
        assert labels == ["Open", "Reject"]
