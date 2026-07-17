"""Pure-Python test for the DB constraint-name matcher in sscc_register.py.

No Odoo. The module itself imports odoo.* so cannot be imported normally
under pytest, but the race-detection helper is pure string matching — load
just that function via importlib.machinery.SourceFileLoader against the raw
file so this stays a genuine pure test (no Odoo stub needed for THIS check),
mirroring how tests/conftest.py registers Odoo-dependent modules individually
rather than executing the whole package tree.

Run: pytest tests/test_sscc_register_constraint_matching.py -q
"""
import importlib.util
import os
import re

_FILE = os.path.join(os.path.dirname(__file__), "..", "models", "sscc_register.py")


def _extract_function_source(name: str) -> str:
    with open(_FILE, encoding="utf-8") as fh:
        src = fh.read()
    # Grab the function body up to the next top-level def/class.
    pattern = re.compile(
        r"^def %s\(.*?\n(?:^(?:[ \t]+.*)?\n?)*" % re.escape(name),
        re.MULTILINE,
    )
    match = pattern.search(src)
    assert match, "could not locate function %r in %s" % (name, _FILE)
    return match.group(0)


def _extract_constant(name: str) -> str:
    with open(_FILE, encoding="utf-8") as fh:
        src = fh.read()
    match = re.search(r'^%s\s*=\s*"([^"]+)"' % re.escape(name), src, re.MULTILINE)
    assert match, "could not locate constant %r in %s" % (name, _FILE)
    return match.group(1)


def _load():
    """Execute just the constant + matcher function in an isolated namespace
    (no Odoo import needed — the matcher body is pure str-in checking)."""
    const_line = 'SSCC_PICKING_UNIT_UNIQUE_CONSTRAINT = "%s"\n' % _extract_constant(
        "SSCC_PICKING_UNIT_UNIQUE_CONSTRAINT"
    )
    func_src = _extract_function_source("_is_picking_unit_race_violation")
    ns = {}
    exec(const_line + func_src, ns)  # noqa: S102 - controlled, test-only
    return ns["_is_picking_unit_race_violation"], ns["SSCC_PICKING_UNIT_UNIQUE_CONSTRAINT"]


def test_constraint_name_matches_odoo_naming_convention():
    _, constant = _load()
    # Odoo's models.Constraint attribute-based API names DB constraints
    # <table_name>_<attribute_name>; table for sscc.register is sscc_register,
    # attribute is _picking_unit_unique -> sscc_register_picking_unit_unique.
    assert constant == "sscc_register_picking_unit_unique"


def test_matches_a_realistic_postgres_unique_violation_message():
    is_race, constant = _load()
    exc = Exception(
        'duplicate key value violates unique constraint '
        '"%s"\nDETAIL:  Key (picking_id, unit_key)=(4, carton-1) already exists.'
        % constant
    )
    assert is_race(exc) is True


def test_does_not_match_unrelated_error():
    is_race, _ = _load()
    exc = Exception("connection to server was lost")
    assert is_race(exc) is False


def test_does_not_match_the_sscc_unique_constraint():
    """A collision on the SSCC value itself (not picking/unit_key) is a
    different failure mode — must NOT be silently swallowed as a race."""
    is_race, _ = _load()
    exc = Exception(
        'duplicate key value violates unique constraint '
        '"sscc_register_sscc_unique"'
    )
    assert is_race(exc) is False
