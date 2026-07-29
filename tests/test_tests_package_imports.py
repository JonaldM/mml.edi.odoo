# mml.edi/tests/test_tests_package_imports.py
"""Guard test: catches a silently-broken `tests/__init__.py` import block.

Root cause this guards against (gate-repair, fixture-reseed repair round):
``tests/__init__.py`` wraps its ``from . import (...)`` tuples in a bare
``except (ImportError, ModuleNotFoundError): pass``. A single stale/typo'd
name in that tuple (e.g. a file that was renamed on disk but not updated in
the tuple) raises inside the ``from . import (...)`` statement, which aborts
the *entire* tuple import silently — every other module in that tuple then
never gets registered as an attribute on the ``tests`` package. Plain
``pytest`` still discovers/collects each ``test_*.py`` file independently
(submodules don't need their parent package's ``__init__.py`` to name them),
so the fast tier does not notice. But ``odoo-bin --test-enable`` relies on
those names actually resolving, so the whole Odoo-integration tier for the
affected modules silently stops being registered.

This test parses ``tests/__init__.py`` via ``ast`` (no need to run it) to
recover every name listed in its ``from . import (...)`` tuples, then
independently imports each one directly — bypassing the swallowing
``except`` — so a stale name fails LOUDLY here instead of silently there.
"""
import ast
import importlib
import pathlib

import pytest

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
_INIT_PATH = _TESTS_DIR / "__init__.py"

# Files in this directory that are not test modules addressed by the
# tests/__init__.py import tuples (support modules, this guard itself, and
# the package marker file). Not asserted against — only test_*.py files that
# ARE named in __init__.py are exercised below; this list exists purely so a
# future contributor extending this guard to a "every test_*.py file must be
# referenced somewhere" check has one place to start.
_NON_TEST_SUPPORT_FILES = {"__init__.py", "conftest.py", "common.py"}


def _names_from_import_tuples(init_source: str) -> list[str]:
    """Return every module name referenced by a `from . import (...)`
    statement at the top level of tests/__init__.py, in source order."""
    tree = ast.parse(init_source, filename=str(_INIT_PATH))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None:
            names.extend(alias.name for alias in node.names)
    return names


def _collect_import_tuple_names() -> list[str]:
    source = _INIT_PATH.read_text(encoding="utf-8")
    return _names_from_import_tuples(source)


@pytest.mark.parametrize("module_name", _collect_import_tuple_names())
def test_init_import_tuple_name_resolves(module_name):
    """Every name listed in tests/__init__.py's `from . import (...)`
    tuples must correspond to a real, importable tests/<name>.py module.

    This is exactly the check tests/__init__.py's own bare
    `except (ImportError, ModuleNotFoundError): pass` swallows — run it here,
    outside that except, so a stale/typo'd name fails the fast tier instead
    of silently disabling odoo-bin test registration.
    """
    file_path = _TESTS_DIR / f"{module_name}.py"
    assert file_path.exists(), (
        f"tests/__init__.py imports '{module_name}' but "
        f"tests/{module_name}.py does not exist on disk. This is the exact "
        "failure mode that silently disabled the whole mml_edi "
        "odoo-bin test-registration tier once already — see this file's "
        "module docstring."
    )
    # Actually import it (relative to this package) rather than merely
    # checking file existence, so a genuine ImportError deeper in the module
    # (not just a missing file) is caught too.
    importlib.import_module(f".{module_name}", package=__package__)


def test_import_tuple_names_are_not_empty():
    """Sanity check on the parser itself: if tests/__init__.py's import
    shape ever changes (e.g. tuples replaced with a different construct),
    make sure this guard doesn't silently start parametrizing zero cases
    (which would make test_init_import_tuple_name_resolves vacuously pass
    on everything)."""
    names = _collect_import_tuple_names()
    assert len(names) >= 30, (
        f"Expected tests/__init__.py's `from . import (...)` tuples to name "
        f"at least 30 modules (the two known blocks), found {len(names)}. "
        "If tests/__init__.py's import style changed intentionally, update "
        "_names_from_import_tuples() to match; do not just raise this "
        "threshold without checking why the count dropped."
    )
