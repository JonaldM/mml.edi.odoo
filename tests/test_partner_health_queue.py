# mml_edi/tests/test_partner_health_queue.py
"""Pure tests: the 'ACK queued' lane must count the claim rows that exist.

edi.order.review._queue_ack writes its pre-upload claim as
event_type='ack_sending', status='success' (edi.log declares 'ack_sending' as
its own event type). The lane counted ack_sent/warning, a pair nothing in the
module ever writes, so a claimed-but-unconfirmed upload was invisible and the
lane sat at zero forever.

No Odoo needed: _exchange_queue only calls search_count on edi.log.
"""
from datetime import datetime, timedelta

from mml_edi.models.edi_partner_health import EdiPartnerHealth

NOW = datetime(2026, 3, 2, 9, 0)


class _Row:
    def __init__(self, event_type, status, timestamp=None):
        self.event_type = event_type
        self.status = status
        self.timestamp = timestamp or (NOW - timedelta(hours=1))


class _LogModel:
    def __init__(self, rows):
        self._rows = rows

    def search_count(self, domain):
        n = 0
        for row in self._rows:
            if all(self._match(row, leaf) for leaf in domain):
                n += 1
        return n

    @staticmethod
    def _match(row, leaf):
        field, op, val = leaf
        actual = getattr(row, field, None)
        if op == "=":
            return actual == val
        if op == ">=":
            return actual >= val
        raise AssertionError("unexpected operator %r" % op)


class _Health:
    _exchange_queue = EdiPartnerHealth._exchange_queue

    def __init__(self, rows):
        self.env = {"edi.log": _LogModel(rows)}


def _lane(rows, label):
    lanes = _Health(rows)._exchange_queue(NOW)
    return [l for l in lanes if l["label"] == label][0]["n"]


class TestAckQueuedLane:

    def test_counts_the_pre_upload_claim_row(self):
        rows = [_Row("ack_sending", "success"), _Row("ack_sending", "success")]
        assert _lane(rows, "ACK queued") == 2

    def test_is_zero_with_no_claims(self):
        assert _lane([_Row("ack_sent", "success")], "ACK queued") == 0

    def test_does_not_swallow_the_sent_or_failed_lanes(self):
        rows = [
            _Row("ack_sending", "success"),
            _Row("ack_sent", "success"),
            _Row("ack_sent", "error"),
        ]
        assert _lane(rows, "ACK queued") == 1
        assert _lane(rows, "ACK sent") == 1
        assert _lane(rows, "Failed") == 1

    def test_older_than_24h_is_out_of_the_window(self):
        stale = _Row("ack_sending", "success", NOW - timedelta(hours=30))
        assert _lane([stale], "ACK queued") == 0
