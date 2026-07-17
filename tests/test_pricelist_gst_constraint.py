# mml.edi/tests/test_pricelist_gst_constraint.py
"""
GST-inclusive pricelist advisory on edi.trading.partner.

EDI prices are quoted ex-GST. A pricelist whose products carry a
GST-inclusive (``price_include``) tax USED to hard-fail the partner save.
That proved too aggressive: a product can legitimately carry both an
ex-GST and an inc-GST tax (retail vs wholesale / multi-company) while its
EDI pricelist value is ex-GST — e.g. the live Animates pricelist ($8.10
ex-GST on products that also hold an inc-GST retail tax). The price
comparison uses the raw pricelist value, and the per-line
``price_discrepancy`` check at order time is the authoritative guard.

So this is now a NON-BLOCKING advisory: ``_pricelist_gst_inclusive_message``
returns a notice string (surfaced as a form banner) instead of raising.

These tests pin down that behaviour:
- No pricelist                                  -> no notice (falsy).
- Pricelist with only ex-GST taxes              -> no notice.
- Pricelist with any inc-GST tax                -> notice naming the pricelist
                                                   (and it must NOT raise).
- Template-only items are inspected too.
- Empty pricelist                               -> no notice.

Pure Python — no Odoo runtime needed.
Run with:  pytest mml_edi/tests/test_pricelist_gst_constraint.py -v
"""
from unittest.mock import MagicMock


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_tax(price_include=False):
    tax = MagicMock()
    tax.price_include = price_include
    return tax


def _make_product(taxes=(), name="SKU"):
    product = MagicMock()
    # Odoo recordsets behave like iterables; a list is good enough for the test.
    product.taxes_id = list(taxes)
    product.display_name = name
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
    """Construct a mock edi.trading.partner with the advisory method bound."""
    from mml_edi.models.edi_trading_partner import EDITradingPartner

    partner = MagicMock()
    partner.pricelist_id = pricelist if pricelist is not None else False
    # ensure_one() is a no-op on a single-record mock.
    partner.ensure_one = lambda: partner
    partner._pricelist_gst_inclusive_message = (
        lambda: EDITradingPartner._pricelist_gst_inclusive_message(partner)
    )
    return partner


# ── Tests ────────────────────────────────────────────────────────────────────


class TestPricelistGstAdvisory:

    def test_no_pricelist_has_no_notice(self):
        partner = _make_partner(pricelist=False)
        assert not partner._pricelist_gst_inclusive_message()

    def test_ex_gst_pricelist_has_no_notice(self):
        product = _make_product(taxes=[_make_tax(price_include=False)])
        pricelist = _make_pricelist(items=[_make_pricelist_item(product=product)])
        partner = _make_partner(pricelist=pricelist)
        assert not partner._pricelist_gst_inclusive_message()

    def test_gst_inclusive_pricelist_warns_but_does_not_raise(self):
        product = _make_product(taxes=[_make_tax(price_include=True)])
        pricelist = _make_pricelist(
            items=[_make_pricelist_item(product=product)],
            name="Animates",
        )
        partner = _make_partner(pricelist=pricelist)

        msg = partner._pricelist_gst_inclusive_message()  # must NOT raise
        assert msg
        assert "Animates" in msg
        assert "ex-GST" in msg
        assert "GST-inclusive" in msg

    def test_mixed_taxes_with_one_inclusive_warns(self):
        """A product carrying BOTH an ex-GST and an inc-GST tax (the real
        Animates shape) still triggers the advisory — but does not block."""
        ex_gst = _make_tax(price_include=False)
        inc_gst = _make_tax(price_include=True)
        clean = _make_product(taxes=[ex_gst], name="Clean SKU")
        dual = _make_product(taxes=[ex_gst, inc_gst], name="Dual-tax SKU")
        pricelist = _make_pricelist(
            items=[
                _make_pricelist_item(product=clean),
                _make_pricelist_item(product=dual),
            ],
            name="Partial-GST Pricelist",
        )
        partner = _make_partner(pricelist=pricelist)

        msg = partner._pricelist_gst_inclusive_message()
        assert msg and "Partial-GST Pricelist" in msg

    def test_template_only_item_is_inspected(self):
        inc_gst = _make_tax(price_include=True)
        tmpl = _make_product(taxes=[inc_gst], name="Tmpl SKU")
        item = MagicMock()
        item.product_id = None
        item.product_tmpl_id = tmpl
        item.display_name = "Template Item"
        pricelist = _make_pricelist(items=[item], name="Template Pricelist")
        partner = _make_partner(pricelist=pricelist)

        assert partner._pricelist_gst_inclusive_message()

    def test_empty_pricelist_has_no_notice(self):
        pricelist = _make_pricelist(items=[], name="Empty Pricelist")
        partner = _make_partner(pricelist=pricelist)
        assert not partner._pricelist_gst_inclusive_message()
