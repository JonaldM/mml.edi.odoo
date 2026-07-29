# mml.edi/tests/test_nimbrel_invoice_service.py
"""Pure-Python tests for services.nimbrel_invoice (Wave2-E, AN-03 INVOIC half).

No live Odoo env — uses minimal fake recordset-shaped objects (not MagicMock,
so a wrong attribute access/filter/slice fails loudly rather than returning
another mock). Run: python -m pytest tests/test_nimbrel_invoice_service.py -q

Covers:
- qty-invoiced == qty-shipped (never SO ordered qty) — scenario 5A/5B partial
  shipment derivation.
- GST ex-tax math (MOA 128 EX tax / MOA 369 GST / MOA 203 INCL tax) built from
  price_subtotal/price_total so 128 + 369 == 203 holds by construction.
- Fail-closed on missing/ambiguous tax.
- Envelope identity (buyer/supplier/ship_to) sourced from the move + partner,
  never a hardcoded placeholder.
"""
import pytest

from mml_edi.services.nimbrel_invoice import (
    NimbrelInvoiceError,
    build_invoic_payload_from_move,
    shipped_qty_by_sale_line,
    _line_moa,
    _line_tax_rate,
)


# ── Minimal fake Odoo recordset shapes ──────────────────────────────────────

class FakeRecordset(list):
    """List that also supports Odoo's .filtered()/.mapped()/[:n] recordset idiom."""

    def filtered(self, fn):
        return FakeRecordset([r for r in self if fn(r)])

    def mapped(self, attr):
        return FakeRecordset([getattr(r, attr) for r in self])

    def __getitem__(self, item):
        result = super().__getitem__(item)
        if isinstance(item, slice):
            return FakeRecordset(result)
        return result


class FakeTax:
    def __init__(self, amount=15.0, amount_type="percent", name="GST 15%"):
        self.amount = amount
        self.amount_type = amount_type
        self.name = name


class FakeProduct:
    def __init__(self, default_code="MML-001", display_name="Test Product",
                 x_articleno="ISC-001"):
        self.default_code = default_code
        self.display_name = display_name
        # Nimbrel ISC (buyer item code) — MML stores it in x_articleno.
        self.x_articleno = x_articleno


class FakeMove:
    """stock.move stand-in (state + quantity + sale_line_id + picking_id)."""

    def __init__(self, state, quantity, sale_line_id, picking_id=None):
        self.state = state
        self.quantity = quantity
        self.sale_line_id = sale_line_id
        self.picking_id = picking_id


class FakePicking:
    def __init__(self, name, date_done=None, carrier_tracking_ref=None):
        self.name = name
        self.date_done = date_done
        self.write_date = date_done
        self.create_date = date_done
        self.carrier_tracking_ref = carrier_tracking_ref
        self.move_ids = FakeRecordset([])


class FakeSOL:
    """sale.order.line stand-in."""

    def __init__(self, id, edi_line_number, order_id=None, move_ids=None):
        self.id = id
        self.edi_line_number = edi_line_number
        self.order_id = order_id
        self.move_ids = FakeRecordset(move_ids or [])


class FakeSaleOrder:
    def __init__(self, name, client_order_ref=None, picking_ids=None, edi_review_id=None):
        self.name = name
        self.client_order_ref = client_order_ref
        self.picking_ids = FakeRecordset(picking_ids or [])
        self.edi_review_id = edi_review_id
        # order_id[:1] pattern needs order_id to be self-referential-ish;
        # handled by wrapping in FakeRecordset at call sites.


class FakeMoveLine:
    def __init__(self, name, quantity, price_unit, price_subtotal, price_total,
                 tax_ids, sale_line_ids, product_id, display_type="product"):
        self.name = name
        self.quantity = quantity
        self.price_unit = price_unit
        self.price_subtotal = price_subtotal
        self.price_total = price_total
        self.tax_ids = FakeRecordset(tax_ids)
        self.sale_line_ids = FakeRecordset(sale_line_ids)
        self.product_id = product_id
        self.display_type = display_type


class FakeCountry:
    def __init__(self, code):
        self.code = code


class FakePartner:
    def __init__(self, name="Nimbrel NZ Holding LTD", street="", city="",
                 zip="", country_code="NZ", vat="", ref=""):
        self.name = name
        self.street = street
        self.city = city
        self.zip = zip
        self.country_id = FakeCountry(country_code) if country_code else None
        self.state_id = None
        self.vat = vat
        self.ref = ref


class FakeCompany:
    def __init__(self, name="M&M Pty Ltd", street="", city="", zip="",
                 country_code="AU", vat="", phone="", email=""):
        self.name = name
        self.street = street
        self.city = city
        self.zip = zip
        self.country_id = FakeCountry(country_code) if country_code else None
        self.state_id = None
        self.vat = vat
        self.phone = phone
        self.email = email


class FakeConfigParam:
    def __init__(self, values=None):
        self.values = values or {}

    def sudo(self):
        return self

    def get_param(self, key, default=None):
        return self.values.get(key, default)


class FakeEnv:
    def __init__(self, config_values=None, company=None):
        self._config = FakeConfigParam(config_values)
        self.company = company or FakeCompany()

    def __getitem__(self, key):
        if key == "ir.config_parameter":
            return self._config
        raise KeyError(key)


class FakeMoveHeader:
    """account.move stand-in (the invoice header)."""

    def __init__(self, name, invoice_line_ids, partner_id, company_id, currency_name="NZD",
                 invoice_date=None, date=None, ref=None, partner_shipping_id=None,
                 env=None):
        self.name = name
        self.invoice_line_ids = FakeRecordset(invoice_line_ids)
        self.partner_id = partner_id
        self.company_id = company_id
        self.currency_id = type("C", (), {"name": currency_name})()
        self.invoice_date = invoice_date
        self.date = date
        self.ref = ref
        self.partner_shipping_id = partner_shipping_id or partner_id
        self.env = env or FakeEnv(company=company_id)

    def ensure_one(self):
        pass


# ── Fixtures ────────────────────────────────────────────────────────────────

def _basic_setup(qty_shipped=2.0, qty_invoiced=2.0):
    from datetime import date

    picking = FakePicking("WH/OUT/00042", date_done=date(2026, 7, 1),
                           carrier_tracking_ref="CONNOTE-99")
    sol = FakeSOL(id=1, edi_line_number=1)
    the_move = FakeMove("done", qty_shipped, sol, picking_id=picking)
    sol.move_ids = FakeRecordset([the_move])
    picking.move_ids = FakeRecordset([the_move])
    order = FakeSaleOrder(
        "S00042", client_order_ref="POR169603", picking_ids=[picking],
    )
    sol.order_id = FakeRecordset([order])

    product = FakeProduct(default_code="5101000")
    tax = FakeTax(amount=15.0)
    move_line = FakeMoveLine(
        name="Product Description",
        quantity=qty_invoiced,
        price_unit=132.44,
        price_subtotal=264.88,
        price_total=304.61,
        tax_ids=[tax],
        sale_line_ids=[sol],
        product_id=product,
    )
    buyer_partner = FakePartner(vat="9429040432250")
    company = FakeCompany(vat="12345678901")
    env = FakeEnv(company=company)
    move = FakeMoveHeader(
        name="INV566343",
        invoice_line_ids=[move_line],
        partner_id=buyer_partner,
        company_id=company,
        invoice_date=date(2026, 7, 2),
        env=env,
    )
    return move, sol, order, picking, move_line


class FakeTradingPartner:
    def __init__(self, nimbrel_vendor_code="V1058", code="NIMBREL"):
        self.nimbrel_vendor_code = nimbrel_vendor_code
        self.code = code

    def get_unb_sender(self):
        return "0200000000004", "ZZZ"

    def get_unb_recipient(self):
        return "NIMBREL", "ZZZ"


# ── shipped_qty_by_sale_line ────────────────────────────────────────────────

def test_shipped_qty_by_sale_line_only_counts_done_moves():
    picking = FakePicking("WH/OUT/1")
    sol = FakeSOL(id=1, edi_line_number=1)
    sol.move_ids = FakeRecordset([
        FakeMove("done", 5.0, sol, picking_id=picking),
        FakeMove("cancel", 999.0, sol, picking_id=picking),
        FakeMove("waiting", 999.0, sol, picking_id=picking),
    ])
    order = FakeSaleOrder("S1", picking_ids=[picking])
    order.picking_ids = FakeRecordset([picking])
    picking.move_ids = sol.move_ids  # picking.move_ids is what the function reads
    totals = shipped_qty_by_sale_line(order)
    assert totals[sol] == 5.0


def test_shipped_qty_by_sale_line_sums_multiple_pickings():
    sol = FakeSOL(id=1, edi_line_number=1)
    picking1 = FakePicking("WH/OUT/1")
    picking2 = FakePicking("WH/OUT/2")
    picking1.move_ids = FakeRecordset([FakeMove("done", 2.0, sol, picking_id=picking1)])
    picking2.move_ids = FakeRecordset([FakeMove("done", 3.0, sol, picking_id=picking2)])
    order = FakeSaleOrder("S1", picking_ids=[picking1, picking2])
    totals = shipped_qty_by_sale_line(order)
    assert totals[sol] == 5.0


def test_shipped_qty_by_sale_line_ignores_moves_without_sale_line():
    picking = FakePicking("WH/OUT/1")
    move = FakeMove("done", 5.0, sale_line_id=None, picking_id=picking)
    picking.move_ids = FakeRecordset([move])
    order = FakeSaleOrder("S1", picking_ids=[picking])
    totals = shipped_qty_by_sale_line(order)
    assert totals == {}


# ── GST math ────────────────────────────────────────────────────────────────

def test_line_moa_ex_tax_plus_gst_equals_incl_tax():
    move_line = FakeMoveLine(
        "desc", 2.0, 132.44, 264.88, 304.61, [FakeTax()], [], FakeProduct(),
    )
    moa = _line_moa(move_line)
    assert moa["moa_128"] == "264.8800"
    assert moa["moa_203"] == "304.6100"
    # 369 computed as the difference — invariant holds by construction.
    assert float(moa["moa_128"]) + float(moa["moa_369"]) == pytest.approx(
        float(moa["moa_203"]), abs=1e-6,
    )


def test_line_tax_rate_formats_two_decimal_places():
    move_line = FakeMoveLine(
        "desc", 1.0, 10.0, 10.0, 11.5, [FakeTax(amount=15.0)], [], FakeProduct(),
    )
    assert _line_tax_rate(move_line) == "15.00"


def test_line_tax_rate_raises_when_no_tax():
    move_line = FakeMoveLine("desc", 1.0, 10.0, 10.0, 10.0, [], [], FakeProduct())
    with pytest.raises(NimbrelInvoiceError):
        _line_tax_rate(move_line)


def test_line_tax_rate_raises_when_multiple_taxes():
    move_line = FakeMoveLine(
        "desc", 1.0, 10.0, 10.0, 11.5,
        [FakeTax(amount=15.0), FakeTax(amount=5.0, name="Levy")], [], FakeProduct(),
    )
    with pytest.raises(NimbrelInvoiceError):
        _line_tax_rate(move_line)


def test_line_tax_rate_raises_for_non_percent_tax():
    move_line = FakeMoveLine(
        "desc", 1.0, 10.0, 10.0, 11.5,
        [FakeTax(amount=2.0, amount_type="fixed")], [], FakeProduct(),
    )
    with pytest.raises(NimbrelInvoiceError):
        _line_tax_rate(move_line)


# ── build_invoic_payload_from_move: qty-invoiced == qty-shipped ────────────

def test_payload_qty_invoiced_matches_shipped_qty():
    move, sol, order, picking, move_line = _basic_setup(qty_shipped=2.0, qty_invoiced=2.0)
    payload = build_invoic_payload_from_move(move, FakeTradingPartner())
    assert payload["lines"][0]["qty_invoiced"] == "2"


def test_payload_qty_invoiced_clamped_to_shipped_even_if_line_qty_higher():
    """Invoice line quantity must never exceed what was actually shipped —
    even if the invoice line itself carries a higher (e.g. SO-derived) qty."""
    move, sol, order, picking, move_line = _basic_setup(qty_shipped=2.0, qty_invoiced=2.0)
    move_line.quantity = 10.0  # simulate a line that (wrongly) carries ordered qty
    payload = build_invoic_payload_from_move(move, FakeTradingPartner())
    assert payload["lines"][0]["qty_invoiced"] == "2"


def test_payload_omits_lines_with_zero_shipped_qty():
    """Scenario 5A: a line not yet shipped must not appear on the INVOIC at all."""
    move, sol, order, picking, move_line = _basic_setup(qty_shipped=2.0, qty_invoiced=2.0)

    # Second SO line/move-line: nothing shipped for it yet.
    sol2 = FakeSOL(id=2, edi_line_number=2)
    sol2.move_ids = FakeRecordset([])  # no done moves at all
    sol2.order_id = sol.order_id
    product2 = FakeProduct(default_code="5101999")
    move_line2 = FakeMoveLine(
        name="Unshipped Product",
        quantity=5.0,
        price_unit=50.0,
        price_subtotal=100.0,
        price_total=115.0,
        tax_ids=[FakeTax()],
        sale_line_ids=[sol2],
        product_id=product2,
    )
    move.invoice_line_ids = FakeRecordset([move_line, move_line2])

    payload = build_invoic_payload_from_move(move, FakeTradingPartner())
    line_numbers = [l["line_no"] for l in payload["lines"]]
    assert line_numbers == ["1"]


def test_payload_raises_when_no_line_has_shipped_qty():
    move, sol, order, picking, move_line = _basic_setup(qty_shipped=0.0, qty_invoiced=2.0)
    sol.move_ids = FakeRecordset([])  # nothing shipped
    with pytest.raises(NimbrelInvoiceError):
        build_invoic_payload_from_move(move, FakeTradingPartner())


def test_payload_skips_non_product_lines():
    move, sol, order, picking, move_line = _basic_setup()
    section_line = FakeMoveLine(
        "Section header", 0.0, 0.0, 0.0, 0.0, [], [], FakeProduct(),
        display_type="line_section",
    )
    move.invoice_line_ids = FakeRecordset([section_line, move_line])
    payload = build_invoic_payload_from_move(move, FakeTradingPartner())
    assert len(payload["lines"]) == 1


# ── envelope / references ───────────────────────────────────────────────────

def test_payload_ref_on_uses_client_order_ref():
    move, sol, order, picking, move_line = _basic_setup()
    payload = build_invoic_payload_from_move(move, FakeTradingPartner())
    assert payload["ref_on"] == "POR169603"


def test_payload_ref_aak_and_cn_from_shipping_picking():
    move, sol, order, picking, move_line = _basic_setup()
    payload = build_invoic_payload_from_move(move, FakeTradingPartner())
    assert payload["ref_aak"] == "DESADV-WHOUT00042"
    assert payload["ref_cn"] == "CONNOTE-99"


def test_payload_raises_when_no_despatch_reference_resolvable():
    move, sol, order, picking, move_line = _basic_setup()
    sol.move_ids = FakeRecordset([])  # no shipped moves -> no picking to reference
    with pytest.raises(NimbrelInvoiceError):
        build_invoic_payload_from_move(move, FakeTradingPartner())


def test_payload_supplier_code_uses_nimbrel_vendor_code():
    move, sol, order, picking, move_line = _basic_setup()
    payload = build_invoic_payload_from_move(move, FakeTradingPartner(nimbrel_vendor_code="V1058"))
    assert payload["supplier"]["code"] == "V1058"


def test_payload_supplier_code_falls_back_to_partner_code():
    move, sol, order, picking, move_line = _basic_setup()
    payload = build_invoic_payload_from_move(
        move, FakeTradingPartner(nimbrel_vendor_code=None, code="NIMBREL"))
    assert payload["supplier"]["code"] == "NIMBREL"


def test_payload_buyer_nzbn_from_config_param_overrides_partner_vat():
    move, sol, order, picking, move_line = _basic_setup()
    move.env = FakeEnv(
        config_values={"mml_edi.nimbrel_nzbn": "9999999999999"},
        company=move.company_id,
    )
    payload = build_invoic_payload_from_move(move, FakeTradingPartner())
    assert payload["buyer"]["nzbn"] == "9999999999999"


def test_payload_buyer_nzbn_falls_back_to_partner_vat():
    move, sol, order, picking, move_line = _basic_setup()
    payload = build_invoic_payload_from_move(move, FakeTradingPartner())
    assert payload["buyer"]["nzbn"] == "9429040432250"


def test_payload_summary_totals_sum_line_moa():
    move, sol, order, picking, move_line = _basic_setup()
    payload = build_invoic_payload_from_move(move, FakeTradingPartner())
    assert payload["summary"]["moa_128"] == "264.88"
    assert payload["summary"]["moa_369"] == "39.73"
    assert payload["summary"]["moa_39"] == "304.61"


def test_payload_currency_defaults_from_move():
    move, sol, order, picking, move_line = _basic_setup()
    payload = build_invoic_payload_from_move(move, FakeTradingPartner())
    assert payload["currency"] == "NZD"
