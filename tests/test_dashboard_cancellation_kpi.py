# mml_edi/tests/test_dashboard_cancellation_kpi.py
"""Pure tests: cancellation reviews must not pollute the human-review KPIs.

edi.processor._process_cancellation creates its audit review already in a
terminal state, with reviewed_date set to the same moment as received_date.
Left in the review-turnaround population every cancellation contributes a
~0.0h sample, dragging the median toward zero, and counts as an order that
"needed a decision" in the exception rate.

No Odoo needed: the domain fragment is a module-level function and
_median_turnaround_h is driven through a fake environment.
"""
from datetime import datetime, timedelta

from mml_edi.models.edi_dashboard import EdiDashboard, _not_cancellation_domain

MARKER = "EDI-CANCELLATION:"


class _Review:
    def __init__(self, received, reviewed, change_summary=False):
        self.received_date = received
        self.reviewed_date = reviewed
        self.change_summary = change_summary


class _ReviewModel:
    """Applies only the leaves this test cares about: the cancellation
    exclusion. Everything else in the domain is already satisfied by the
    records the test builds."""

    def __init__(self, records):
        self._records = records

    def search(self, domain):
        excludes = any(
            isinstance(leaf, (list, tuple)) and len(leaf) == 3
            and leaf[0] == "change_summary" and leaf[1] == "not like"
            for leaf in domain
        )
        if not excludes:
            return list(self._records)
        return [
            r for r in self._records
            if not (r.change_summary or "").startswith(MARKER)
        ]


class _Processor:
    CANCELLATION_MARKER = MARKER


class _Env(dict):
    pass


class _Dash:
    """Unbound-call host for the dashboard methods under test."""

    _non_cancellation_domain = EdiDashboard._non_cancellation_domain

    def __init__(self, records):
        self.env = _Env({
            "edi.order.review": _ReviewModel(records),
            "edi.processor": _Processor(),
        })


def _median(records):
    dash = _Dash(records)
    return EdiDashboard._median_turnaround_h(dash, [], datetime(2026, 1, 1))


class TestNotCancellationDomain:
    def test_fragment_keeps_reviews_without_a_change_summary(self):
        assert _not_cancellation_domain(MARKER) == [
            "|", ("change_summary", "=", False),
            ("change_summary", "not like", MARKER),
        ]

    def test_fragment_is_bound_to_the_processor_marker(self):
        dash = _Dash([])
        assert dash._non_cancellation_domain() == _not_cancellation_domain(MARKER)


class TestMedianTurnaroundExcludesCancellations:
    def _human(self, hours):
        received = datetime(2026, 2, 1, 8, 0)
        return _Review(received, received + timedelta(hours=hours))

    def _cancellation(self):
        # Exactly what _process_cancellation writes: terminal on creation, so
        # received_date and reviewed_date are the same instant.
        stamp = datetime(2026, 2, 1, 9, 0)
        return _Review(stamp, stamp,
                       "%s PO 123 cancelled by customer (BGM 1225=1)" % MARKER)

    def test_median_ignores_cancellation_samples(self):
        records = [self._human(4.0), self._human(6.0)] + [
            self._cancellation() for _ in range(4)]
        # Without the exclusion the median of [0, 0, 0, 0, 4, 6] is 0.0.
        assert _median(records) == 5.0

    def test_median_of_human_reviews_only_is_unchanged(self):
        assert _median([self._human(2.0), self._human(8.0)]) == 5.0

    def test_median_is_none_when_only_cancellations_resolved(self):
        assert _median([self._cancellation(), self._cancellation()]) is None
