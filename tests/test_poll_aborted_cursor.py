"""Pure tests for the aborted-cursor guard in run_scheduled_poll.

A Postgres-level failure inside poll_trading_partner leaves the cursor in
InFailedSqlTransaction. The recovery path is all SQL (breaker write, edi.log
INSERT, ir.config_parameter reads for the alert), so without a rollback the
FIRST recovery statement raises again from inside the except block, where
nothing catches it: the cron dies, the remaining partners are never polled,
the breaker is never incremented and no alert is sent.

Same bug class as stock_3pl_rohlig/models/inbound_cron.py and the dsv
token-refresh cron.
"""
from types import SimpleNamespace

from mml_edi.models.edi_processor import EDIProcessor


class _AbortedTransaction(Exception):
    """Stands in for psycopg2.errors.InFailedSqlTransaction."""


class _DatabaseError(Exception):
    """Stands in for a Postgres-level error (statement timeout, deadlock)."""


class _FakeCr:
    """Cursor that refuses every statement once the transaction is aborted,
    until somebody rolls it back."""

    def __init__(self):
        self.aborted = False
        self.rollbacks = 0

    def abort(self):
        self.aborted = True

    def rollback(self):
        self.aborted = False
        self.rollbacks += 1

    def guard(self):
        if self.aborted:
            raise _AbortedTransaction(
                "current transaction is aborted, commands ignored until end "
                "of transaction block"
            )


class _FakePartnerModel:

    def __init__(self, partners):
        self._partners = partners

    def search(self, domain):
        return self._partners


class _FakeLog:

    def __init__(self, cr, rows):
        self._cr = cr
        self._rows = rows

    def log(self, partner, direction, event_type, status, message, **kwargs):
        self._cr.guard()
        self._rows.append((partner.code, event_type, status))


class _FakeEnv:

    def __init__(self, cr, partners, log_rows):
        self.cr = cr
        self._models = {
            "edi.trading.partner": _FakePartnerModel(partners),
            "edi.log": _FakeLog(cr, log_rows),
        }

    def __getitem__(self, name):
        return self._models[name]


def _make_partner(cr, code, partner_id, failure_count=0, threshold=5):
    partner = SimpleNamespace(
        id=partner_id,
        code=code,
        circuit_failure_count=failure_count,
        circuit_failure_threshold=threshold,
        circuit_open_since=False,
        circuit_cooldown_minutes=60,
        writes=[],
    )

    def _write(vals):
        cr.guard()
        partner.writes.append(vals)

    partner.write = _write
    return partner


class _Processor(EDIProcessor):

    def __init__(self, cr, partners, outcomes):
        self.cr = cr
        self.log_rows = []
        self.env = _FakeEnv(cr, partners, self.log_rows)
        self._outcomes = outcomes
        self.polled = []
        self.alerts = []

    def poll_trading_partner(self, partner):
        self.polled.append(partner.code)
        outcome = self._outcomes[partner.code]
        if isinstance(outcome, Exception):
            # A DB-level failure poisons the cursor before it propagates.
            self.cr.abort()
            raise outcome
        return outcome

    def _send_cron_alert(self, module_name, subject, body):
        # The real one reads two ir.config_parameter rows before its own try.
        self.env.cr.guard()
        self.alerts.append((module_name, subject))


def _two_partners():
    cr = _FakeCr()
    first = _make_partner(cr, "AAA", 1)
    second = _make_partner(cr, "BBB", 2)
    return cr, first, second


class TestPollAbortedCursor:

    def test_db_level_failure_does_not_kill_the_cron_run(self):
        cr, first, second = _two_partners()
        proc = _Processor(
            cr, [first, second],
            {"AAA": _DatabaseError("canceling statement due to statement timeout"),
             "BBB": 0},
        )
        proc.run_scheduled_poll()
        assert proc.polled == ["AAA", "BBB"], (
            "a poisoned cursor on the first partner must not starve the rest"
        )

    def test_breaker_and_alert_survive_a_db_level_failure(self):
        cr, first, second = _two_partners()
        proc = _Processor(
            cr, [first, second],
            {"AAA": _DatabaseError("deadlock detected"), "BBB": 0},
        )
        proc.run_scheduled_poll()
        assert first.writes and first.writes[0]["circuit_failure_count"] == 1, (
            "the circuit breaker must still be incremented"
        )
        assert proc.alerts, "the cron alert email must still be sent"
        assert proc.log_rows, "the error must still reach edi.log"

    def test_recovery_rolls_back_before_touching_the_orm(self):
        cr, first, second = _two_partners()
        proc = _Processor(
            cr, [first, second],
            {"AAA": _DatabaseError("could not serialize access"), "BBB": 0},
        )
        proc.run_scheduled_poll()
        assert cr.rollbacks == 1
        assert not cr.aborted

    def test_clean_run_never_rolls_back(self):
        cr, first, second = _two_partners()
        proc = _Processor(cr, [first, second], {"AAA": 0, "BBB": 0})
        proc.run_scheduled_poll()
        assert cr.rollbacks == 0
