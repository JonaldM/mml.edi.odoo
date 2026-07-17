"""Pure tests for FIX OPS-25: per-file failures must count toward (never
reset) the circuit breaker in run_scheduled_poll.

Previously a poll that downloaded files but failed to process one still hit
the success path and RESET circuit_failure_count — a poisoned file re-failed
silently every 15 minutes forever.
"""
from types import SimpleNamespace

from mml_edi.models.edi_processor import EDIProcessor


class _FakePartnerModel:

    def __init__(self, partners):
        self._partners = partners

    def search(self, domain):
        return self._partners


class _FakeLog:

    def __init__(self, rows):
        self._rows = rows

    def log(self, *args, **kwargs):
        self._rows.append(args)


class _FakeEnv:

    def __init__(self, partners, log_rows):
        self._models = {
            "edi.trading.partner": _FakePartnerModel(partners),
            "edi.log": _FakeLog(log_rows),
        }

    def __getitem__(self, name):
        return self._models[name]


def _make_partner(failure_count=0, threshold=5):
    partner = SimpleNamespace(
        id=42,
        code="TESTP",
        circuit_failure_count=failure_count,
        circuit_failure_threshold=threshold,
        circuit_open_since=False,
        circuit_cooldown_minutes=60,
        writes=[],
    )
    partner.write = partner.writes.append
    return partner


class _Processor(EDIProcessor):

    def __init__(self, partner, poll_result):
        self._log_rows = []
        self.env = _FakeEnv([partner], self._log_rows)
        self._poll_result = poll_result
        self.alerts = []

    def poll_trading_partner(self, partner):
        if isinstance(self._poll_result, Exception):
            raise self._poll_result
        return self._poll_result

    def _send_cron_alert(self, module_name, subject, body):
        self.alerts.append((module_name, subject))


class TestFileFailureBreaker:

    def test_clean_poll_resets_breaker(self):
        partner = _make_partner(failure_count=3)
        _Processor(partner, poll_result=0).run_scheduled_poll()
        assert partner.writes == [
            {"circuit_failure_count": 0, "circuit_open_since": False}
        ]

    def test_file_failures_increment_breaker_instead_of_resetting(self):
        partner = _make_partner(failure_count=0)
        _Processor(partner, poll_result=2).run_scheduled_poll()
        assert len(partner.writes) == 1
        assert partner.writes[0]["circuit_failure_count"] == 1
        assert "circuit_open_since" not in partner.writes[0]

    def test_file_failures_trip_breaker_at_threshold(self):
        partner = _make_partner(failure_count=4, threshold=5)
        _Processor(partner, poll_result=1).run_scheduled_poll()
        assert partner.writes[0]["circuit_failure_count"] == 5
        assert partner.writes[0].get("circuit_open_since"), (
            "reaching the threshold must open the circuit"
        )

    def test_poll_exception_still_increments_and_alerts(self):
        partner = _make_partner(failure_count=0)
        proc = _Processor(partner, poll_result=RuntimeError("FTP down"))
        proc.run_scheduled_poll()
        assert partner.writes[0]["circuit_failure_count"] == 1
        assert proc.alerts, "poll-level failure must send the cron alert"
