"""Pure-Python tests for the 19.0.1.3.0 config-extraction migration.

19.0.1.3.0 removed every account-specific literal from mml_edi's runtime paths
and replaced it with an ir.config_parameter read carrying NO code default. Two
contracts have to hold at once, and this module pins both:

  (a) UPGRADE path — a deployment that was relying on a removed code default
      keeps the identical value, seeded once by the migration.
  (b) FRESH install — no account-specific value is present anywhere, and the
      product code carries none to fall back on.

The migration runs before the registry is loaded, so it talks raw SQL to a
psycopg cursor. The fake cursor below is a dict-backed stand-in for the one
table it touches (ir_config_parameter), which keeps this in the fast tier.

Run: pytest mml_edi/tests/test_config_extraction_migration.py -q
"""
import importlib.util
import pathlib

import pytest

_MIGRATION_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "migrations" / "19.0.1.3.0"
)


def _load(name: str, filename: str):
    """Import a migration file by path (the names contain a '-')."""
    path = _MIGRATION_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


post_migration = _load("mml_edi_19_0_1_3_0_post", "post-migration.py")


class _FakeCursor:
    """Dict-backed stand-in for ir_config_parameter under raw SQL."""

    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self._result = None

    def execute(self, sql, params=()):
        normalised = " ".join(sql.split()).upper()
        if normalised.startswith("SELECT VALUE FROM IR_CONFIG_PARAMETER"):
            key = params[0]
            self._result = (
                (self.rows[key],) if key in self.rows else None
            )
        elif normalised.startswith("INSERT INTO IR_CONFIG_PARAMETER"):
            key, value = params
            self.rows[key] = value
            self._result = None
        else:  # pragma: no cover - the migration issues no other statement
            raise AssertionError("unexpected SQL: %s" % sql)

    def fetchone(self):
        return self._result


# --- (a) upgrade path -------------------------------------------------------

def test_upgrade_seeds_every_extracted_key_from_the_deployment_overlay():
    """Every literal this version removed is restored from the overlay, so the
    deployment that owned those defaults behaves identically after `-u`."""
    overlay = post_migration._legacy_defaults()
    if not overlay:
        pytest.skip("no deployment overlay in this tree — nothing to seed")

    cr = _FakeCursor()
    post_migration.migrate(cr, "19.0.1.2.2")

    for key in post_migration.EXTRACTED_PARAM_KEYS:
        if key in overlay:
            assert cr.rows[key] == overlay[key], key


def test_upgrade_never_overwrites_an_operator_configured_value():
    """A key an operator already set keeps its value — the seed is a backfill,
    not a reset."""
    overlay = post_migration._legacy_defaults()
    if not overlay:
        pytest.skip("no deployment overlay in this tree — nothing to seed")

    key = next(iter(overlay))
    cr = _FakeCursor({key: "OPERATOR-CHOSEN"})
    post_migration.migrate(cr, "19.0.1.2.2")
    assert cr.rows[key] == "OPERATOR-CHOSEN"


def test_blank_stored_value_counts_as_unset_and_is_seeded():
    overlay = post_migration._legacy_defaults()
    if not overlay:
        pytest.skip("no deployment overlay in this tree — nothing to seed")

    key = next(iter(overlay))
    cr = _FakeCursor({key: "   "})
    post_migration.migrate(cr, "19.0.1.2.2")
    assert cr.rows[key] == overlay[key]


def test_migration_is_idempotent():
    cr = _FakeCursor()
    post_migration.migrate(cr, "19.0.1.2.2")
    first = dict(cr.rows)
    post_migration.migrate(cr, "19.0.1.2.2")
    assert cr.rows == first


def test_migration_is_a_noop_without_a_version():
    """Fresh installs never reach a migration; guard anyway."""
    cr = _FakeCursor()
    post_migration.migrate(cr, None)
    assert cr.rows == {}


# --- (b) fresh install ------------------------------------------------------

def test_seeder_writes_nothing_when_the_overlay_is_absent(monkeypatch):
    """The overlay is excluded from the productised tree. Without it the
    migration must seed NOTHING — no other deployment inherits these values."""
    monkeypatch.setattr(post_migration, "_legacy_defaults", lambda: {})
    cr = _FakeCursor()
    post_migration.migrate(cr, "19.0.1.2.2")
    assert cr.rows == {}


def test_no_runtime_code_path_carries_an_account_specific_default():
    """The extraction itself: every constant the migration replaces is empty in
    the shipped module code."""
    from mml_edi.parsers.kestrelby_asn import DEFAULT_BUYER_GLN
    from mml_edi.parsers.nimbrel_edifact import (
        DEFAULT_RECIPIENT_ID, DEFAULT_RECIPIENT_TEST_ID,
    )

    assert DEFAULT_BUYER_GLN == ""
    assert DEFAULT_RECIPIENT_ID == ""
    assert DEFAULT_RECIPIENT_TEST_ID == ""


def test_extracted_key_list_matches_the_overlay_it_seeds():
    """The overlay may not carry a key the migration does not know about —
    otherwise a value would silently never be seeded."""
    overlay = post_migration._legacy_defaults()
    unknown = set(overlay) - set(post_migration.EXTRACTED_PARAM_KEYS)
    assert not unknown, unknown
