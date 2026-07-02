"""Pure tests for the duplicate-SO guards (go-live FIX IDEM-1 / IDEM major 3):

- the per-partner poll advisory-lock key,
- the unique-violation detector that converts a losing create-race into the
  standard duplicate_skipped path,
- the partner + company scoping of _find_existing_so.
"""
import pathlib
from types import SimpleNamespace

from mml_edi.models.edi_processor import (
    EDI_POLL_LOCK_CLASS,
    SO_EDI_CLIENT_REF_INDEX,
    EDIProcessor,
    _is_duplicate_so_violation,
)

_SRC = pathlib.Path(__file__).parent.parent / "models" / "edi_processor.py"


class TestAdvisoryLockKey:

    def test_lock_class_is_a_stable_int4(self):
        """pg_advisory_xact_lock(class, id) takes two signed int4 halves —
        the class constant must fit and must never change across releases."""
        assert isinstance(EDI_POLL_LOCK_CLASS, int)
        assert 0 < EDI_POLL_LOCK_CLASS < 2 ** 31

    def test_poll_takes_the_advisory_xact_lock(self):
        src = _SRC.read_text()
        assert "pg_advisory_xact_lock" in src, (
            "poll_trading_partner must serialize concurrent polls of the same "
            "partner with a per-partner pg_advisory_xact_lock"
        )


class TestDuplicateViolationDetector:

    def test_matches_unique_violation_from_backstop_index(self):
        exc = Exception(
            'duplicate key value violates unique constraint "%s"'
            % SO_EDI_CLIENT_REF_INDEX
        )
        assert _is_duplicate_so_violation(exc) is True

    def test_ignores_unrelated_errors(self):
        assert _is_duplicate_so_violation(Exception("connection reset")) is False
        assert _is_duplicate_so_violation(
            Exception('duplicate key value violates unique constraint '
                      '"sale_order_pkey"')
        ) is False


class _FakeSaleOrderModel:

    def __init__(self):
        self.last_domain = None

    def search(self, domain, limit=None):
        self.last_domain = domain
        return None  # falsy -> helper returns None


class _FakeEnv:

    def __init__(self, models, company_id):
        self._models = models
        self.company = SimpleNamespace(id=company_id)

    def __getitem__(self, name):
        return self._models[name]


class TestFindExistingSoScoping:

    def test_search_is_scoped_to_partner_and_company(self):
        """A colliding non-EDI (or other-partner) client_order_ref must never
        suppress a real order as duplicate_skipped — the dedup search must
        carry the EDI discriminator and company, not the bare ref."""
        proc = EDIProcessor()
        so_model = _FakeSaleOrderModel()
        proc.env = _FakeEnv({"sale.order": so_model}, company_id=7)
        partner = SimpleNamespace(id=3)

        result = proc._find_existing_so("4500180080_1080", partner)

        assert result is None
        assert ("client_order_ref", "=", "4500180080_1080") in so_model.last_domain
        assert ("edi_trading_partner_id", "=", 3) in so_model.last_domain
        assert ("company_id", "=", 7) in so_model.last_domain
