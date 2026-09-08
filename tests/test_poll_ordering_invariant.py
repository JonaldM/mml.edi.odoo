"""Mock-driven tests for the per-file poll ordering invariant (FIX IDEM-2).

Per file the poll loop MUST do, in this order:

    process -> write dedup marker (file_download/success) -> COMMIT ->
    FTP delete of the inbox file -> queue ACK / OOS summary

The DB must be durable BEFORE the delete (a worker death after the delete but
before a commit would silently lose the PO forever), and the ACK must go LAST
(Briscoes must never hold an ORDRSP for uncommitted state).

Also covered: the advisory lock is taken at the top of the poll AND re-taken
after each commit (pg_advisory_xact_lock is transaction-scoped, so every
commit releases it), commits are suppressed in test mode, and per-file
failures return a non-zero count + fire the rate-limited alert.
"""
import pytest
from contextlib import contextmanager
from types import SimpleNamespace

import mml_edi.models.edi_ftp as edi_ftp_mod
from mml_edi.models.edi_processor import EDI_POLL_LOCK_CLASS, EDIProcessor


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _AbortedTransaction(Exception):
    """Stands in for psycopg2.errors.InFailedSqlTransaction."""


class _FakeCr:

    def __init__(self, events):
        self._events = events
        self.aborted = False

    def abort(self):
        """Model a Postgres-level error: every later statement is refused
        until the transaction is rolled back."""
        self.aborted = True

    def _guard(self):
        if self.aborted:
            raise _AbortedTransaction(
                "current transaction is aborted, commands ignored until end "
                "of transaction block"
            )

    def execute(self, query, params=None):
        self._guard()
        if "pg_advisory_xact_lock" in query:
            self._events.append(("lock", params))

    def commit(self):
        self._guard()
        self._events.append(("commit",))

    def rollback(self):
        self.aborted = False
        self._events.append(("rollback",))

    @contextmanager
    def savepoint(self, flush=True):
        yield


class _FakeRegistry:

    def __init__(self, test_mode):
        self._test_mode = test_mode

    def in_test_mode(self):
        return self._test_mode


class _FakeLog:

    def __init__(self, events, cr=None):
        self._events = events
        self._cr = cr

    def log(self, partner, direction, event_type, status, message, **kw):
        if self._cr is not None:
            self._cr._guard()
        self._events.append(("log", event_type, status))


class _FakeEnv:

    def __init__(self, events, test_mode):
        self.cr = _FakeCr(events)
        self.registry = _FakeRegistry(test_mode)
        self.context = {}
        self._events = events

    def __getitem__(self, name):
        if name == "edi.log":
            return _FakeLog(self._events, self.cr)
        raise KeyError(name)


class _FakeHandler:

    def __init__(self, events, files):
        self._events = events
        self._files = files

    @contextmanager
    def connection(self):
        yield

    def list_files(self):
        return list(self._files)

    def download_file(self, filename):
        return b"<xml/>"

    def delete_file(self, filename):
        self._events.append(("delete", filename))


class _Processor(EDIProcessor):
    """EDIProcessor with env wiring + collaborator stubs for pure tests.

    Only the poll loop itself runs for real; file processing, dedup, alerting
    and the response dispatch are recorded stubs.
    """

    def __init__(self, events, test_mode=False, failures=None):
        self.env = _FakeEnv(events, test_mode)
        self._events = events
        self._failures = failures or []

    def with_context(self, **kwargs):
        self.env.context.update(kwargs)
        return self

    def _is_file_duplicate(self, file_hash, partner):
        return False

    def _process_file(self, content, file_hash, filename, partner):
        self._events.append(("process", filename))
        return list(self._failures)

    def _send_file_responses(self, partner, file_hash):
        self._events.append(("ack", file_hash))

    def _alert_file_failure(self, partner, filename, detail):
        self._events.append(("alert", filename))


@pytest.fixture
def partner():
    return SimpleNamespace(id=42, code="TESTP")


def _run_poll(monkeypatch, partner, test_mode=False, failures=None,
              files=("PO_1.xml",)):
    events = []
    handler = _FakeHandler(events, files)
    monkeypatch.setattr(edi_ftp_mod, "EDIFTPHandler", lambda p: handler)
    proc = _Processor(events, test_mode=test_mode, failures=failures)
    result = proc.poll_trading_partner(partner)
    return events, result


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPollOrderingInvariant:

    def test_clean_file_marker_commit_delete_ack_order(self, monkeypatch, partner):
        events, failed = _run_poll(monkeypatch, partner)
        assert failed == 0
        marker = events.index(("log", "file_download", "success"))
        commit = events.index(("commit",))
        delete = next(i for i, e in enumerate(events) if e[0] == "delete")
        ack = next(i for i, e in enumerate(events) if e[0] == "ack")
        assert marker < commit < delete < ack, (
            "ordering invariant violated: marker -> COMMIT -> delete -> ACK, "
            "got %r" % events
        )

    def test_advisory_lock_taken_first_and_retaken_after_commit(
            self, monkeypatch, partner):
        events, _ = _run_poll(monkeypatch, partner)
        locks = [i for i, e in enumerate(events) if e[0] == "lock"]
        commit = events.index(("commit",))
        assert locks and locks[0] == 0, "lock must be the FIRST poll action"
        assert events[locks[0]][1] == (EDI_POLL_LOCK_CLASS, partner.id)
        # pg_advisory_xact_lock is released by the commit — it must be re-taken
        assert any(i > commit for i in locks), (
            "advisory lock must be re-acquired after each per-file commit"
        )

    def test_failed_file_commits_partial_progress_but_never_deletes(
            self, monkeypatch, partner):
        events, failed = _run_poll(
            monkeypatch, partner, failures=[("1001", "boom")])
        assert failed == 1
        assert ("commit",) in events, (
            "succeeded stores must be committed even when siblings failed"
        )
        assert not any(e[0] == "delete" for e in events), (
            "a failed file must stay in the inbox for retry"
        )
        assert not any(e[0] == "ack" for e in events)
        assert any(e[0] == "alert" for e in events), (
            "per-file failures must fire the rate-limited alert (OPS-25)"
        )

    def test_no_commit_inside_test_mode(self, monkeypatch, partner):
        events, failed = _run_poll(monkeypatch, partner, test_mode=True)
        assert failed == 0
        assert ("commit",) not in events
        # the rest of the pipeline still runs
        assert any(e[0] == "delete" for e in events)
        assert any(e[0] == "ack" for e in events)

    def test_download_exception_counts_alerts_and_continues(
            self, monkeypatch, partner):
        events = []
        handler = _FakeHandler(events, ["A.xml", "B.xml"])
        orig_download = handler.download_file

        def _boom_once(filename):
            if filename == "A.xml":
                raise ValueError("poisoned file")
            return orig_download(filename)

        handler.download_file = _boom_once
        monkeypatch.setattr(edi_ftp_mod, "EDIFTPHandler", lambda p: handler)
        proc = _Processor(events)
        failed = proc.poll_trading_partner(partner)
        assert failed == 1
        assert any(e == ("alert", "A.xml") for e in events)
        # B.xml still processed cleanly
        assert ("process", "B.xml") in events

    def test_db_level_file_failure_rolls_back_and_keeps_polling(
            self, monkeypatch, partner):
        """A Postgres-level error on one file poisons the cursor. The per-file
        recovery path writes edi.log immediately, so without a rollback it
        raises again from inside its own except block and aborts the whole
        poll: every remaining file is skipped and the caller never learns the
        file failed."""
        events = []
        handler = _FakeHandler(events, ["A.xml", "B.xml"])
        proc = _Processor(events)
        orig_process = proc._process_file

        def _poison_once(content, file_hash, filename, partner_):
            if filename == "A.xml":
                proc.env.cr.abort()
                raise _AbortedTransaction("deadlock detected")
            return orig_process(content, file_hash, filename, partner_)

        proc._process_file = _poison_once
        monkeypatch.setattr(edi_ftp_mod, "EDIFTPHandler", lambda p: handler)

        failed = proc.poll_trading_partner(partner)

        assert failed == 1
        assert ("rollback",) in events, (
            "the recovery path must roll back before it writes edi.log"
        )
        assert ("process", "B.xml") in events, (
            "one poisoned file must not skip the rest of the inbox"
        )
        rollback = events.index(("rollback",))
        assert any(
            e[0] == "lock" for e in events[rollback:]
        ), "the rollback releases the advisory lock, so it must be re-taken"
