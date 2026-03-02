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

# 5. mml_edi.services — no Odoo dependency, safe to execute __init__.py
services_dir = os.path.join(_ROOT, "services")
mml_edi_services = _register_package("mml_edi.services", services_dir, parent=mml_edi)

# 6. mml_edi.services.edi_service — the service class under test
_register_module(
    "mml_edi.services.edi_service",
    os.path.join(services_dir, "edi_service.py"),
    mml_edi_services,
)
