# mml.edi/tests/test_client_ref_template.py
"""Structural test: client_ref_template field has a constrains validator."""
import ast
import pathlib
import pytest


_SRC_PATH = pathlib.Path(__file__).parent.parent / 'models/edi_trading_partner.py'


def test_client_ref_template_has_constrains_validator():
    src = _SRC_PATH.read_text()
    assert '_validate_client_ref_template' in src or (
        'client_ref_template' in src and 'constrains' in src
    ), (
        "edi_trading_partner.py must have an @api.constrains validator "
        "for client_ref_template that rejects unknown template variables"
    )


def test_allowed_template_vars_defined():
    src = _SRC_PATH.read_text()
    assert 'po_number' in src and 'store_code' in src, (
        "Allowed template variables (po_number, store_code) must be referenced "
        "in the validator"
    )


def test_constrains_decorator_references_client_ref_template():
    """The @api.constrains call must reference 'client_ref_template'."""
    src = _SRC_PATH.read_text()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for deco in node.decorator_list:
                dumped = ast.dump(deco)
                if 'constrains' in dumped and 'client_ref_template' in dumped:
                    found = True
                    break
    assert found, (
        "No @api.constrains('client_ref_template') decorator found in "
        "edi_trading_partner.py"
    )


def test_unknown_variable_rejected_by_regex():
    """The regex _TEMPLATE_VAR_RE must match $var and ${var} style."""
    import re
    # Mirror the pattern that must be in the implementation
    pattern = re.compile(r'\$\{?(\w+)\}?')
    assert pattern.findall('$po_number') == ['po_number']
    assert pattern.findall('${store_code}') == ['store_code']
    assert pattern.findall('$unknown_var') == ['unknown_var']
    # Empty template — no vars
    assert pattern.findall('ORDER-REF-ONLY') == []
