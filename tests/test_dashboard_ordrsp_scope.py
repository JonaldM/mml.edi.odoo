# mml_edi/tests/test_dashboard_ordrsp_scope.py
"""Pure tests: the ORDRSP metrics must count ORDRSP uploads only.

edi.log's ``ack_sent`` event_type is shared by three outbound documents:
the per-PO ORDRSP (ACK_<partner>_<po>_<key>.edi), the Animates DESADV
(DESADV_ANIMATES_...) and the Animates INVOIC (INVOIC_ANIMATES_...). The
'ORDRSP on-time' KPI, the pipeline's 'Acknowledged' stage and the pipeline
header's 'ORDRSP today' all claim to be per-PO ORDRSP numbers, so each must
be scoped to the ACK filename prefix the sender actually writes.

No Odoo needed: the three producers only need an edi.log that understands
search_count over the leaves they pass.
"""
from datetime import datetime, timedelta

from mml_edi.models.edi_dashboard import EdiDashboard, _ORDRSP_FILENAME_PATTERN

NOW = datetime(2026, 3, 2, 9, 0)
TODAY_START = NOW - timedelta(hours=9)
WINDOW_START = NOW - timedelta(days=30)

TARGETS = {
    "auto_approval_target": 80.0, "auto_approval_amber": 60.0,
    "ordrsp_on_time_target": 99.0, "ordrsp_on_time_amber": 95.0,
    "review_turnaround_target_h": 4.0, "review_turnaround_amber_h": 8.0,
    "exception_rate_target": 10.0, "exception_rate_amber": 20.0,
}


class _LogRow:
    def __init__(self, event_type, status, filename, timestamp=NOW):
        self.event_type = event_type
        self.status = status
        self.filename = filename
        self.timestamp = timestamp


def _like(value, pattern):
    """The subset of SQL LIKE the ORDRSP scope uses: a literal prefix whose
    own underscore is backslash-escaped, then '%'."""
    assert pattern.endswith("%")
    prefix = pattern[:-1].replace("\\_", "_")
    return (value or "").startswith(prefix)


def _matches(row, domain):
    for leaf in domain:
        if not (isinstance(leaf, (list, tuple)) and len(leaf) == 3):
            continue
        field, op, val = leaf
        actual = getattr(row, field, None)
        if op == "=" and actual != val:
            return False
        if op == ">=" and not (actual is not None and actual >= val):
            return False
        if op == "=like" and not _like(actual, val):
            return False
    return True


class _LogModel:
    def __init__(self, rows):
        self._rows = rows

    def search_count(self, domain):
        return len([r for r in self._rows if _matches(r, domain)])


class _ReviewRecords(list):
    def filtered(self, fn):
        return _ReviewRecords([r for r in self if fn(r)])

    def mapped(self, name):
        return [getattr(r, name) for r in self]


class _ReviewModel:
    def __init__(self, records):
        self._records = records

    def search(self, domain, order=None, limit=None):
        return _ReviewRecords(self._records)

    def search_count(self, domain):
        return len(self._records)


class _Dash:
    """Unbound-call host for the three ORDRSP producers."""

    _kpis = EdiDashboard._kpis
    _pipeline = EdiDashboard._pipeline
    _today_totals = EdiDashboard._today_totals
    _pending_review_count = EdiDashboard._pending_review_count
    _partner_domain = EdiDashboard._partner_domain
    _median_turnaround_h = EdiDashboard._median_turnaround_h
    _non_cancellation_domain = EdiDashboard._non_cancellation_domain
    _ordrsp_ack_domain = EdiDashboard._ordrsp_ack_domain

    def __init__(self, logs, reviews=()):
        class _Processor:
            CANCELLATION_MARKER = "EDI-CANCELLATION:"

        self.env = {
            "edi.log": _LogModel(logs),
            "edi.order.review": _ReviewModel(list(reviews)),
            "edi.processor": _Processor(),
        }


# One successful ORDRSP, one DESADV and one INVOIC, all logged ack_sent/success.
MIXED = [
    _LogRow("ack_sent", "success", "ACK_BRISCOES_4500178971_ab12cd34.edi"),
    _LogRow("ack_sent", "success", "DESADV_ANIMATES_PO7788_20260302.edi"),
    _LogRow("ack_sent", "success", "INVOIC_ANIMATES_INV991_20260302.edi"),
]


class TestOrdrspFilenameScope:

    def test_pattern_escapes_the_separator_underscore(self):
        # An unescaped '_' is a single-character SQL LIKE wildcard, so
        # 'ACK_%' would also match e.g. 'ACKX...'.
        assert _ORDRSP_FILENAME_PATTERN == "ACK\\_%"

    def test_domain_carries_the_event_type_and_the_prefix(self):
        assert _Dash([])._ordrsp_ack_domain() == [
            ("event_type", "=", "ack_sent"),
            ("filename", "=like", _ORDRSP_FILENAME_PATTERN),
        ]


class TestOrdrspOnTimeKpi:

    def test_desadv_and_invoic_do_not_inflate_the_denominator(self):
        rows = MIXED + [
            _LogRow("ack_sent", "error", "ACK_BRISCOES_4500178972_bb22cd34.edi"),
        ]
        kpis = _Dash(rows)._kpis(NOW, WINDOW_START, TARGETS)
        # One ORDRSP sent, one failed: 50%. Counting the DESADV and the
        # INVOIC as ORDRSPs reads 75% and hides half the failures.
        assert kpis["ordrsp_on_time"]["value"] == 50.0


class TestPipelineAcknowledged:

    def test_acknowledged_counts_ordrsp_only(self):
        assert _Dash(MIXED)._pipeline(TODAY_START)["acknowledged"] == 1


class TestTodayTotals:

    def test_ordrsp_today_counts_ordrsp_only(self):
        assert _Dash(MIXED)._today_totals(TODAY_START)["acks"] == 1
