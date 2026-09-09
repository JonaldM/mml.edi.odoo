# mml_edi/tests/test_partner_health_throughput_tz.py
"""Pure tests: the 7-day throughput bars are labelled with weekday names, so
they must be bucketed on the operator's calendar day.

New Zealand runs UTC+12/+13, so everything between local midnight and local
noon falls in the PREVIOUS UTC calendar day. Bucketing on UTC while printing
'%a' put a Monday-morning poll under "Sun". Same class as the connector-health
UTC-vs-NZ bug already fixed elsewhere in the stack.

No Odoo needed: the day list, the read_group timezone and the labels are all
driven through a fake edi.log.
"""
from datetime import date, datetime, timedelta

import pytz

from mml_edi.models.edi_partner_health import EdiPartnerHealth

NZ = pytz.timezone("Pacific/Auckland")


class _Partner:
    def __init__(self, pid):
        self.id = pid


class _Partners(list):
    @property
    def ids(self):
        return [p.id for p in self]


class _PidKey:
    def __init__(self, pid):
        self.id = pid


class _LogModel:
    """Groups on whatever timezone the context asked for, as read_group does."""

    def __init__(self, rows, tz="UTC"):
        self._rows = rows
        self._tz = tz

    def with_context(self, **kwargs):
        return _LogModel(self._rows, kwargs.get("tz", self._tz))

    def _read_group(self, domain, groupby, aggregates):
        wanted = dict(
            (leaf[0], leaf[2]) for leaf in domain if leaf[1] == "=")
        cutoff = dict(
            (leaf[0], leaf[2]) for leaf in domain if leaf[1] == ">=")["timestamp"]
        tz = pytz.timezone(self._tz)
        out = {}
        for pid, event_type, stamp in self._rows:
            if event_type != wanted["event_type"] or stamp < cutoff:
                continue
            local_day = pytz.utc.localize(stamp).astimezone(tz).date()
            out[(pid, local_day)] = out.get((pid, local_day), 0) + 1
        return [(_PidKey(pid), day, cnt) for (pid, day), cnt in out.items()]


class _User:
    tz = "Pacific/Auckland"


class _Health:
    _batched_throughput = EdiPartnerHealth._batched_throughput

    def __init__(self, rows):
        self.env = _Env(rows)


class _Env(dict):
    def __init__(self, rows):
        super().__init__({"edi.log": _LogModel(rows)})
        self.user = _User()


def _utc(local_naive):
    """The naive-UTC instant of an NZ wall-clock time."""
    return NZ.localize(local_naive).astimezone(pytz.utc).replace(tzinfo=None)


# 09:00 NZ on Monday 2 March 2026 is 20:00 UTC on Sunday 1 March.
MONDAY_MORNING_NZ = datetime(2026, 3, 2, 9, 0)
NOW = _utc(datetime(2026, 3, 2, 17, 0))


def _series(rows):
    partners = _Partners([_Partner(7)])
    return _Health(rows)._batched_throughput(NOW, partners)[7]


class TestThroughputBucketsOnTheLocalDay:

    def test_local_morning_activity_lands_on_the_local_day(self):
        series = _series([(7, "file_download", _utc(MONDAY_MORNING_NZ))])
        today = series[-1]
        assert today["day"] == "Mon"
        assert today["files"] == 1

    def test_the_previous_bar_stays_empty(self):
        series = _series([(7, "file_download", _utc(MONDAY_MORNING_NZ))])
        assert series[-2]["files"] == 0

    def test_orders_bucket_the_same_way(self):
        series = _series([(7, "order_created", _utc(MONDAY_MORNING_NZ))])
        assert series[-1]["orders"] == 1

    def test_the_window_is_seven_local_days_ending_today(self):
        series = _series([])
        assert len(series) == 7
        labels = [b["day"] for b in series]
        expected = [
            (date(2026, 3, 2) - timedelta(days=6 - i)).strftime("%a")
            for i in range(7)
        ]
        assert labels == expected

    def test_activity_before_the_window_is_dropped(self):
        stale = _utc(datetime(2026, 2, 20, 9, 0))
        assert _series([(7, "file_download", stale)])[-1]["files"] == 0
