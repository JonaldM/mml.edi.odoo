# mml.edi/tests/conftest.py
"""
pytest configuration for standalone (no-Odoo) unit tests.

The module directory is named `mml.edi` (with a dot), which Python cannot
import as a package directly.  This conftest registers the necessary
sub-packages under the `mml_edi` alias before any test module is collected,
without loading Odoo-dependent models.
"""
import importlib.util
import os
import sys
import types

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _register_package(pkg_name: str, pkg_dir: str, parent=None) -> types.ModuleType:
    """
    Register a directory as a Python package under pkg_name.

    Executes the package's __init__.py only if it has no Odoo imports.
    Falls back to a bare namespace package otherwise so submodules can
    still be imported individually.
    """
    init_path = os.path.join(pkg_dir, "__init__.py")
    spec = importlib.util.spec_from_file_location(
        pkg_name,
        init_path,
        submodule_search_locations=[pkg_dir],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = pkg_name
    mod.__path__ = [pkg_dir]
    sys.modules[pkg_name] = mod
    if parent is not None:
        leaf = pkg_name.split(".")[-1]
        setattr(parent, leaf, mod)
    try:
        spec.loader.exec_module(mod)
    except (ImportError, ModuleNotFoundError):
        # __init__.py pulls in Odoo — leave as a bare namespace, fine for tests
        pass
    return mod


def _register_module(mod_name: str, file_path: str, package_mod: types.ModuleType) -> types.ModuleType:
    """Load a single .py file as a named module inside an already-registered package."""
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = mod_name.rsplit(".", 1)[0]
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    leaf = mod_name.split(".")[-1]
    setattr(package_mod, leaf, mod)
    return mod


# ── Odoo stubs (allow Odoo-importing modules to be loaded without Odoo) ───────

def _ensure_odoo_stubs() -> None:
    """
    Inject minimal stubs for ``odoo`` and its most-used sub-modules so that
    model files (which import ``from odoo import models``) can be loaded by
    the standalone test runner without a running Odoo instance.

    Only called once; subsequent calls are no-ops because the stubs are already
    registered in sys.modules.
    """
    if "odoo" in sys.modules:
        return

    odoo_mod = types.ModuleType("odoo")
    odoo_models = types.ModuleType("odoo.models")
    odoo_fields = types.ModuleType("odoo.fields")
    odoo_api = types.ModuleType("odoo.api")
    odoo_exceptions = types.ModuleType("odoo.exceptions")

    # Minimal AbstractModel stub so EDIProcessor.__new__ works in tests
    class AbstractModel:
        _name = ""
        _description = ""

        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__(**kwargs)

    class UserError(Exception):
        pass

    # Field descriptor stub: callable (so ``fields.Char(...)``) and also
    # exposes class-level attributes (e.g. ``fields.Datetime.now``).
    class _FieldStub:
        now = None
        today = None
        context_today = None

        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__(**kwargs)

        def __call__(self, *args, **kwargs):
            return None

        def __class_getitem__(cls, item):
            return cls

    # Make each field type its own class so ``fields.Datetime.now`` works.
    class _Char(_FieldStub):
        pass

    class _Boolean(_FieldStub):
        pass

    class _Integer(_FieldStub):
        pass

    class _Float(_FieldStub):
        pass

    class _Date(_FieldStub):
        pass

    class _Datetime(_FieldStub):
        pass

    class _Many2one(_FieldStub):
        pass

    class _One2many(_FieldStub):
        pass

    class _Many2many(_FieldStub):
        pass

    class _Selection(_FieldStub):
        pass

    class _Text(_FieldStub):
        pass

    class _Html(_FieldStub):
        pass

    odoo_models.AbstractModel = AbstractModel
    odoo_models.Model = AbstractModel
    odoo_models.TransientModel = AbstractModel
    odoo_fields.Char = _Char()
    odoo_fields.Boolean = _Boolean()
    odoo_fields.Integer = _Integer()
    odoo_fields.Float = _Float()
    odoo_fields.Date = _Date()
    odoo_fields.Datetime = _Datetime()
    odoo_fields.Many2one = _Many2one()
    odoo_fields.One2many = _One2many()
    odoo_fields.Many2many = _Many2many()
    odoo_fields.Selection = _Selection()
    odoo_fields.Text = _Text()
    odoo_fields.Html = _Html()
    odoo_exceptions.UserError = UserError
    odoo_exceptions.ValidationError = UserError

    def _make_decorator(*_args, **_kwargs):
        """
        Flexible stub for Odoo API decorators.

        Handles two calling conventions:
        - @api.model              → called with the function directly
        - @api.depends("field")   → called with strings, returns a decorator
        """
        # If the single positional arg is callable it's being used as a bare
        # decorator: @api.model  (no parentheses).
        if len(_args) == 1 and callable(_args[0]):
            return _args[0]
        # Otherwise it was called with arguments, e.g. @api.depends("a", "b").
        # Return a pass-through decorator.
        return lambda func: func

    odoo_api.model = _make_decorator
    odoo_api.depends = _make_decorator
    odoo_api.onchange = _make_decorator
    odoo_api.constrains = _make_decorator
    odoo_api.multi = _make_decorator
    odoo_api.returns = _make_decorator
    odoo_api.model_create_multi = _make_decorator

    # odoo._ (translation) — just return the string unchanged
    odoo_mod._ = lambda s: s
    odoo_mod.api = odoo_api
    odoo_mod.fields = odoo_fields
    odoo_mod.models = odoo_models
    odoo_mod.exceptions = odoo_exceptions

    # odoo.tests / odoo.tests.common — stub TransactionCase so Odoo-only test
    # files can be *collected* by pytest without errors. The actual test classes
    # are decorated with @unittest.skipUnless(_ODOO_AVAILABLE, ...) so they are
    # skipped gracefully when Odoo is not installed.
    #
    # Must inherit from unittest.TestCase so pytest honours the
    # @unittest.skipUnless decorator on the subclasses.
    import unittest as _unittest

    class TransactionCase(_unittest.TestCase):  # type: ignore[no-redef]
        pass

    odoo_tests_common = types.ModuleType("odoo.tests.common")
    odoo_tests_common.TransactionCase = TransactionCase
    odoo_tests = types.ModuleType("odoo.tests")
    odoo_tests.common = odoo_tests_common
    odoo_mod.tests = odoo_tests

    for name, mod in [
        ("odoo", odoo_mod),
        ("odoo.models", odoo_models),
        ("odoo.fields", odoo_fields),
        ("odoo.api", odoo_api),
        ("odoo.exceptions", odoo_exceptions),
        ("odoo.tests", odoo_tests),
        ("odoo.tests.common", odoo_tests_common),
    ]:
        sys.modules[name] = mod


_ensure_odoo_stubs()

# ── Bootstrap ────────────────────────────────────────────────────────────────

# 1. Top-level mml_edi package (bare — __init__.py has Odoo relative imports)
mml_edi = _register_package("mml_edi", _ROOT)

# 2. mml_edi.parsers — no Odoo dependency, __init__.py is safe to execute
parsers_dir = os.path.join(_ROOT, "parsers")
mml_edi_parsers = _register_package("mml_edi.parsers", parsers_dir, parent=mml_edi)

# 3. mml_edi.models — bare namespace (full __init__.py imports Odoo models)
models_dir = os.path.join(_ROOT, "models")
mml_edi_models = _register_package("mml_edi.models", models_dir, parent=mml_edi)

# 4. mml_edi.models.edi_ftp — the module under test
_register_module(
    "mml_edi.models.edi_ftp",
    os.path.join(models_dir, "edi_ftp.py"),
    mml_edi_models,
)

# 4b. mml_edi.models.edi_processor — needed by TestPricelistCompat
#     Odoo stubs (registered above) allow the module-level Odoo imports to
#     succeed so the class body can be inspected and methods called via mocks.
_register_module(
    "mml_edi.models.edi_processor",
    os.path.join(models_dir, "edi_processor.py"),
    mml_edi_models,
)

# 5. mml_edi.services — no Odoo dependency, safe to execute __init__.py
services_dir = os.path.join(_ROOT, "services")
mml_edi_services = _register_package("mml_edi.services", services_dir, parent=mml_edi)

# 6. mml_edi.services.edi_service — the service class under test
_register_module(
    "mml_edi.services.edi_service",
    os.path.join(services_dir, "edi_service.py"),
    mml_edi_services,
)
