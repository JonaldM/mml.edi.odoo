"""Pure tests for per-record isolation in edi.bulk.action.action_approve_all.

action_approve does substantial DB work before it can raise (re-clamped SO
line quantities, chatter, an ORDCHG diff written over an existing SO), and
Python unwinding does not undo ORM writes. The loop also reaches a real
cr.commit() through _queue_ack -> _commit_ack_progress, so a failing
record's half-applied state used to be made permanent by a LATER record's
commit, while the operator was told "N approved, 1 skipped".
"""
from mml_edi.wizards.edi_bulk_action import EDIBulkAction


class _FakeCr:

    def __init__(self, journal):
        self._journal = journal

    def commit(self):
        self._journal.append(("commit", None))

    def rollback(self):
        self._journal.append(("rollback", None))


class _FakeRegistry:
    """A LIVE registry: commits and rollbacks are real, so the wizard must
    isolate each record for itself."""

    @staticmethod
    def in_test_mode():
        return False


class _FakeEnv:

    def __init__(self, journal):
        self.cr = _FakeCr(journal)
        self.registry = _FakeRegistry()


class _FakeReview:

    def __init__(self, journal, name, blocking=0, boom=False):
        self._journal = journal
        self.id = abs(hash(name)) % 1000
        self.display_name = name
        self.blocking_issue_count = blocking
        self._boom = boom

    def action_approve(self):
        # Stands in for the SO line rewrites / chatter / ORDCHG diff that
        # action_approve applies before it can raise.
        self._journal.append(("wrote", self.display_name))
        if self._boom:
            raise ValueError("locked fiscal period")
        self._journal.append(("approved", self.display_name))


class _Wizard(EDIBulkAction):

    def __init__(self, journal, reviews):
        self.env = _FakeEnv(journal)
        self.review_ids = reviews

    def ensure_one(self):
        return self


def _batch(names_and_booms):
    journal = []
    reviews = [
        _FakeReview(journal, name, boom=boom) for name, boom in names_and_booms
    ]
    return journal, _Wizard(journal, reviews)


class TestBulkApproveIsolation:

    def test_failed_record_is_rolled_back_before_the_next_one_runs(self):
        journal, wiz = _batch([("R-1", False), ("R-BOOM", True), ("R-3", False)])
        wiz.action_approve_all()
        boom = journal.index(("wrote", "R-BOOM"))
        nxt = journal.index(("wrote", "R-3"))
        assert ("rollback", None) in journal[boom:nxt], (
            "the failed record's partial writes must be discarded before the "
            "next record can commit them: %r" % journal
        )

    def test_approved_records_are_made_durable(self):
        journal, wiz = _batch([("R-1", False), ("R-BOOM", True)])
        wiz.action_approve_all()
        first = journal.index(("approved", "R-1"))
        boom = journal.index(("wrote", "R-BOOM"))
        assert ("commit", None) in journal[first:boom], (
            "an approved record must be durable before a later record's "
            "rollback could take it with it: %r" % journal
        )

    def test_clean_batch_never_rolls_back(self):
        journal, wiz = _batch([("R-1", False), ("R-2", False)])
        wiz.action_approve_all()
        assert ("rollback", None) not in journal

    def test_notification_names_the_failed_records(self):
        journal, wiz = _batch([("R-1", False), ("R-BOOM", True)])
        params = wiz.action_approve_all()["params"]
        assert "1 record(s) approved" in params["message"]
        assert "R-BOOM" in params["message"], (
            "a corrupted-then-rolled-back record must be named, not folded "
            "into a generic 'skipped' count"
        )
        assert params["type"] == "warning"

    def test_blocking_records_are_still_skipped_without_a_rollback(self):
        journal = []
        blocked = _FakeReview(journal, "R-BLOCK", blocking=2)
        clean = _FakeReview(journal, "R-OK")
        wiz = _Wizard(journal, [blocked, clean])
        params = wiz.action_approve_all()["params"]
        assert ("wrote", "R-BLOCK") not in journal
        assert "1 record(s) skipped" in params["message"]
        assert ("rollback", None) not in journal
