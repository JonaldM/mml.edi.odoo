# mml_edi/tests/test_dashboard_ack_filename.py
"""Pure tests: the dashboard triage must resolve ACK status by the SAME
filename the sender uses.

edi.order.review._ack_exchange_filename appends "_a<n>" once ack_attempt >= 2
(IDEM-4: a manager reset AFTER the ORDRSP went out). A dashboard that
re-derives "ACK_<code>_<po>_<hash8>.edi" inline looks up attempt 1's name
instead, finds the OLD success row, and reports the exchange as acknowledged
while the re-send sits failed. The wall alarms and the mobile triage read the
same list, so all three go blind together.

No Odoo needed: _attention_items only touches env["edi.order.review"],
env["edi.log"] and its own sibling helpers, all of which are supplied here.
"""
from datetime import datetime, timedelta

from mml_edi.models.edi_dashboard import EdiDashboard
from mml_edi.models.edi_order_review import _ack_filename

NOW = datetime(2026, 3, 2, 9, 0)
WINDOW_START = NOW - timedelta(days=30)


class _Partner:
    def __init__(self, code):
        self.code = code


class _Review:
    """Enough of edi.order.review for the triage, including the real helper."""

    def __init__(self, rid, po, attempt, file_hash="ab12cd34ff"):
        self.id = rid
        self.name = "REV%03d" % rid
        self.trading_partner_id = _Partner("BRISCOES")
        self.customer_po_number = po
        self.edi_file_hash = file_hash
        self.ack_attempt = attempt
        self.state = "approved"
        self.received_date = NOW - timedelta(hours=6)
        self.reviewed_date = NOW - timedelta(hours=4)

    def _ack_exchange_filename(self):
        return _ack_filename(
            self.trading_partner_id.code, self.customer_po_number,
            (self.edi_file_hash or str(self.id))[:8], self.ack_attempt or 1)


class _Log:
    def __init__(self, filename, status):
        self.filename = filename
        self.status = status


class _RecordSet(list):
    """A list that answers the couple of recordset calls the triage makes."""

    def mapped(self, name):
        return [getattr(r, name) for r in self]


class _Model:
    def __init__(self, records, matcher=None):
        self._records = records
        self._matcher = matcher

    def search(self, domain, order=None, limit=None):
        if self._matcher is None:
            return _RecordSet(self._records)
        return _RecordSet([r for r in self._records if self._matcher(r, domain)])


def _filename_in(record, domain):
    for leaf in domain:
        if (isinstance(leaf, (list, tuple)) and len(leaf) == 3
                and leaf[0] == "filename" and leaf[1] == "in"):
            return record.filename in leaf[2]
    return True


class _Dash:
    """Unbound-call host for the dashboard methods under test."""

    _attention_items = EdiDashboard._attention_items
    _partner_domain = EdiDashboard._partner_domain
    _item_ack_failed = EdiDashboard._item_ack_failed
    _item_blocking = EdiDashboard._item_blocking
    _item_warning = EdiDashboard._item_warning
    _partner_tag = EdiDashboard._partner_tag
    _review_title = EdiDashboard._review_title
    _issue_summary = EdiDashboard._issue_summary
    _age_hours = EdiDashboard._age_hours

    def __init__(self, reviews, logs):
        self._pending = _Model([])
        self.env = {
            "edi.order.review": _Model(
                reviews,
                lambda r, dom: any(
                    isinstance(leaf, (list, tuple)) and leaf[0] == "state"
                    and leaf[1] == "in" for leaf in dom),
            ),
            "edi.log": _Model(logs, _filename_in),
        }


def _triage(reviews, logs):
    return _Dash(reviews, logs)._attention_items(NOW, WINDOW_START)


class TestAckFailedUsesTheExchangeFilename:

    def test_first_attempt_failure_is_surfaced(self):
        rec = _Review(1, "4500178971", 1)
        items = _triage([rec], [_Log(rec._ack_exchange_filename(), "error")])
        assert [i["kind"] for i in items] == ["ack_failed"]

    def test_resend_after_reset_is_surfaced(self):
        # IDEM-4: attempt 1 succeeded, the manager reset, attempt 2 errored.
        rec = _Review(2, "4500178971", 2)
        logs = [
            _Log(_ack_filename("BRISCOES", "4500178971", "ab12cd34"), "success"),
            _Log(rec._ack_exchange_filename(), "error"),
        ]
        items = _triage([rec], logs)
        assert [i["kind"] for i in items] == ["ack_failed"], (
            "the triage looked up the attempt-1 filename and read the old "
            "success row, so the failed re-send stayed invisible")

    def test_successful_resend_is_not_surfaced(self):
        rec = _Review(3, "4500178971", 2)
        logs = [
            _Log(_ack_filename("BRISCOES", "4500178971", "ab12cd34"), "error"),
            _Log(rec._ack_exchange_filename(), "success"),
        ]
        assert _triage([rec], logs) == []

    def test_no_ack_log_at_all_is_not_surfaced(self):
        rec = _Review(4, "4500178971", 2)
        assert _triage([rec], []) == []
