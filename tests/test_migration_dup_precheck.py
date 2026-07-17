"""Tests for the 19.0.1.0.3 migration (FIX IDEM-1b DB backstop).

The migration MUST pre-check existing duplicate live EDI SOs and abort with a
clear listing rather than half-create the partial unique index against a
dirty table.
"""
import importlib.util
import pathlib

import pytest

_MIG_DIR = pathlib.Path(__file__).parent.parent / "migrations" / "19.0.1.0.3"


def _load(filename):
    path = _MIG_DIR / filename
    name = "mml_edi_mig_" + filename.replace("-", "_").replace(".py", "")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeCr:

    def __init__(self, dup_rows):
        self._dup_rows = dup_rows
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append(query)

    def fetchall(self):
        return self._dup_rows


_DUPS = [(3, 1, "4500180080_1080", ["S00042", "S00043"], 2)]


class TestDuplicatePrecheck:

    def test_pre_migration_aborts_with_clear_listing(self):
        pre = _load("pre-migration.py")
        with pytest.raises(Exception) as excinfo:
            pre.migrate(_FakeCr(_DUPS), "19.0.1.0.2")
        msg = str(excinfo.value)
        assert "4500180080_1080" in msg
        assert "S00042" in msg and "S00043" in msg

    def test_pre_migration_passes_on_clean_table(self):
        pre = _load("pre-migration.py")
        pre.migrate(_FakeCr([]), "19.0.1.0.2")  # must not raise

    def test_post_migration_aborts_on_duplicates(self):
        post = _load("post-migration.py")
        cr = _FakeCr(_DUPS)
        with pytest.raises(Exception):
            post.migrate(cr, "19.0.1.0.2")
        assert not any("CREATE UNIQUE INDEX" in q for q in cr.queries), (
            "the index must never be half-created against a dirty table"
        )

    def test_post_migration_creates_partial_unique_index_when_clean(self):
        post = _load("post-migration.py")
        cr = _FakeCr([])
        post.migrate(cr, "19.0.1.0.2")
        create = [q for q in cr.queries if "CREATE UNIQUE INDEX" in q]
        assert create, "post-migration must create the backstop index"
        sql = create[0]
        assert "sale_order_edi_client_ref_uniq" in sql
        assert "edi_trading_partner_id" in sql
        assert "state != 'cancel'" in sql, (
            "cancelled SOs must be excluded so re-order-after-cancellation "
            "stays allowed"
        )
