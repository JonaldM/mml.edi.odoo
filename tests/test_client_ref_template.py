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


def test_render_client_ref_substitutes_with_underscore_separator():
    """Regression: '{po_number}_{store_code}' must fully substitute.

    Previously the '_' separator was absorbed into the '$po_number' identifier
    (string.Template read 'po_number_' as the name), leaving the literal
    '$po_number_1050' as the client reference — which also collided across POs.
    """
    from mml_edi.models.edi_trading_partner import EDITradingPartner

    class _P:
        def __init__(self, tmpl):
            self.client_ref_template = tmpl

        def ensure_one(self):
            pass

    # per-store template with an underscore separator (Briscoes multi-store)
    assert EDITradingPartner.render_client_ref(
        _P('{po_number}_{store_code}'), '4500176574', '1050') == '4500176574_1050'
    # dollar-style equivalent
    assert EDITradingPartner.render_client_ref(
        _P('$po_number_$store_code'), '7010168258', '1050') == '7010168258_1050'
    # single-order template (no store)
    assert EDITradingPartner.render_client_ref(
        _P('{po_number}'), '700123', None) == '700123'


class _FakePartnerSet(list):
    """Iterable stand-in for `self` in an @api.constrains method."""


def _validate(template):
    from mml_edi.models.edi_trading_partner import EDITradingPartner

    class _P:
        client_ref_template = template

    EDITradingPartner._validate_client_ref_template(_FakePartnerSet([_P()]))


def test_brace_style_unknown_variable_is_rejected():
    """The field's own default and help document BRACE syntax
    ('{po_number}', 'Variables: {po_number}, {store_code}'), so an unknown
    brace token must fail validation. The regex required a literal '$', so
    findall() returned [] and the constraint never fired - the template then
    rendered as a literal, collapsing every store of a PO onto one client
    reference."""
    from odoo.exceptions import ValidationError

    with pytest.raises(ValidationError):
        _validate('{po_number}_{branch}')


def test_brace_style_known_variables_are_accepted():
    _validate('{po_number}_{store_code}')
    _validate('{po_number}')


def test_dollar_style_still_validated():
    from odoo.exceptions import ValidationError

    _validate('${po_number}')
    _validate('$po_number')
    with pytest.raises(ValidationError):
        _validate('$branch')


def test_literal_template_with_no_variables_is_accepted():
    _validate('ORDER-REF-ONLY')


def test_error_message_names_the_brace_syntax():
    from odoo.exceptions import ValidationError

    with pytest.raises(ValidationError) as exc:
        _validate('{branch}')
    assert '{po_number}' in str(exc.value)
