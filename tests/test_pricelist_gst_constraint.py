# mml.edi/tests/test_pricelist_gst_constraint.py
"""
GST-inclusive pricelist constraint on edi.trading.partner.

EDI prices from Briscoes (and similar retail partners) are quoted ex-GST.
If an Odoo pricelist resolves to GST-inclusive prices, every line will
show a systematic ~15% discrepancy and produce false-positive
price_discrepancy issues, blocking otherwise-clean orders.

These tests pin down the @api.constrains('pricelist_id') behaviour:
- Reject when the pricelist's items reference products whose taxes
  include any account.tax with price_include=True.
- Accept when no items reference GST-inclusive taxes.
- Accept when the pricelist is unset (price comparison is opt-in).

Pure Python — no Odoo runtime needed.
Run with:  pytest mml_edi/tests/test_pricelist_gst_constraint.py -v
"""
from unittest.mock import MagicMock

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_tax(price_include=False):
    tax = MagicMock()
    tax.price_include = price_include
    return tax


def _make_product(taxes=()):
    product = MagicMock()
    # Odoo recordsets behave like iterables; a list is good enough for the test.
    product.taxes_id = list(taxes)
    return product


def _make_pricelist_item(product=None, name="Item"):
    item = MagicMock()
    item.product_id = product
    item.product_tmpl_id = None
    item.display_name = name
    return item


def _make_pricelist(items=(), name="Test Pricelist"):
    pricelist = MagicMock()
    pricelist.id = 1
    pricelist.name = name
    pricelist.display_name = name
    pricelist.item_ids = list(items)
    # Bool() of an Odoo Many2one with a record is True; with empty/False it's False.
    pricelist.__bool__ = lambda self: True
    return pricelist


def _make_partner(pricelist=None):
    """Construct a mock edi.trading.partner with the constraint method bound."""
    from mml_edi.models.edi_trading_partner import EDITradingPartner

    partner = MagicMock()
    partner.pricelist_id = pricelist if pricelist is not None else False
    # Bind the unbound method so `self` resolves to this mock.
    partner._validate_pricelist_gst = (
        lambda: EDITradingPartner._validate_pricelist_gst(partner)
    )
    # Iteration over a recordset yields its records; for a single-record
    # mock, iterate over [partner] itself.
    partner.__iter__ = lambda self: iter([partner])
    return partner


# ── Tests ────────────────────────────────────────────────────────────────────


class TestPricelistGstConstraint:

    def test_no_pricelist_is_accepted(self):
        """A trading partner without a pricelist must not raise."""
        partner = _make_partner(pricelist=False)
        # Should not raise.
        partner._validate_pricelist_gst()

    def test_ex_gst_pricelist_is_accepted(self):
        """A pricelist whose items reference only ex-GST taxes is accepted."""
        ex_gst_tax = _make_tax(price_include=False)
        product = _make_product(taxes=[ex_gst_tax])
        pricelist = _make_pricelist(items=[_make_pricelist_item(product=product)])
        partner = _make_partner(pricelist=pricelist)

        # Should not raise.
        partner._validate_pricelist_gst()

    def test_gst_inclusive_pricelist_is_rejected(self):
        """A pricelist whose product taxes include price_include=True must
        raise ValidationError with a message naming the pricelist."""
        from odoo.exceptions import ValidationError

        gst_tax = _make_tax(price_include=True)
        product = _make_product(taxes=[gst_tax])
        pricelist = _make_pricelist(
            items=[_make_pricelist_item(product=product)],
            name="Briscoes Products (Inc. GST)",
        )
        partner = _make_partner(pricelist=pricelist)

        with pytest.raises(ValidationError) as excinfo:
            partner._validate_pricelist_gst()

        msg = str(excinfo.value)
        assert "GST-exclusive" in msg or "ex-GST" in msg
        assert "Briscoes Products (Inc. GST)" in msg
        assert "15%" in msg or "discrepancy" in msg

    def test_mixed_taxes_with_one_inclusive_is_rejected(self):
        """If any single product on the pricelist has a price-include tax,
        the entire pricelist is rejected (defence in depth)."""
        from odoo.exceptions import ValidationError

        ex_gst_tax = _make_tax(price_include=False)
        gst_tax = _make_tax(price_include=True)

        clean_product = _make_product(taxes=[ex_gst_tax])
        dirty_product = _make_product(taxes=[ex_gst_tax, gst_tax])

        pricelist = _make_pricelist(
            items=[
                _make_pricelist_item(product=clean_product, name="Clean SKU"),
                _make_pricelist_item(product=dirty_product, name="Dirty SKU"),
            ],
            name="Partial-GST Pricelist",
        )
        partner = _make_partner(pricelist=pricelist)

        with pytest.raises(ValidationError):
            partner._validate_pricelist_gst()

    def test_pricelist_with_template_only_item_is_checked(self):
        """Items that target a product template (not a variant) must also
        be inspected via product_tmpl_id.taxes_id."""
        from odoo.exceptions import ValidationError

        gst_tax = _make_tax(price_include=True)
        tmpl = _make_product(taxes=[gst_tax])

        item = MagicMock()
        item.product_id = None
        item.product_tmpl_id = tmpl
        item.display_name = "Template Item"

        pricelist = _make_pricelist(items=[item], name="Template Pricelist")
        partner = _make_partner(pricelist=pricelist)

        with pytest.raises(ValidationError):
            partner._validate_pricelist_gst()

    def test_pricelist_with_no_items_is_accepted(self):
        """An empty pricelist (no items) cannot resolve to GST-inclusive
        prices on its own, so it is accepted. The processor will fall back
        to product.list_price; that is treated as a separate concern."""
        pricelist = _make_pricelist(items=[], name="Empty Pricelist")
        partner = _make_partner(pricelist=pricelist)

        # Should not raise.
        partner._validate_pricelist_gst()
