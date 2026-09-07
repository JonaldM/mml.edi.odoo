"""Pure tests for the backorder OOS-policy availability read.

The legacy backorder branch of `_process_order_line` used to read
`product.with_context(warehouse=...).qty_available`. Odoo 19 stock only reads
the `warehouse_id` context key, so `warehouse` was a no-op and the figure was
company-wide: retired Auckland's phantom stock and every other company the cron
user belongs to leaked in. It also wrote `edi_qty_shortfall`, which drives the
ORDRSP line action, even though a backorder partner is shipped in full.

Covered here:
  - indent orders skip the availability gate entirely (no issue, shortfall 0),
  - a genuine backorder partner gets a warning issue but no shortfall write,
  - the dead `'warehouse'` context key is gone from the source.
"""
import pathlib
from types import SimpleNamespace

from mml_edi.models.edi_processor import EDIProcessor

_SRC = (pathlib.Path(__file__).parent.parent / "models" /
        "edi_processor.py").read_text(encoding="utf-8")


# -- Fakes -------------------------------------------------------------------

class _FakeSol(SimpleNamespace):
    pass


class _FakeSolModel:

    def __init__(self):
        self.created = []

    def create(self, vals):
        sol = _FakeSol(id=len(self.created) + 1, **vals)
        self.created.append(sol)
        return sol


class _FakeIssueModel:

    def __init__(self):
        self.created = []

    def create(self, vals):
        self.created.append(vals)
        return SimpleNamespace(id=len(self.created))


class _FakeEnv:

    def __init__(self, models):
        self._models = models

    def __getitem__(self, name):
        return self._models[name]


def _make_processor(available):
    """EDIProcessor wired to fake models; `available` is the DC-scoped qty."""
    proc = EDIProcessor()
    issues = _FakeIssueModel()
    sols = _FakeSolModel()
    proc.env = _FakeEnv({
        "edi.order.issue": issues,
        "sale.order.line": sols,
    })
    product = SimpleNamespace(id=11, name="Test Product", default_code="TP-1")
    proc._find_product = lambda parsed_line, partner: (product, "barcode")
    proc._get_pricelist_price = lambda p, qty, partner: None
    proc._dc_available_qty = lambda p, so: available
    proc._issues = issues
    proc._sols = sols
    return proc


def _make_args(is_indent=False, ordered=10.0):
    parsed_line = SimpleNamespace(
        line_number=1,
        product_code="9780000000002",
        description="Test Product",
        quantity=ordered,
        unit_price=9.99,
        uom="EA",
    )
    so = SimpleNamespace(id=5, x_is_indent=is_indent)
    partner = SimpleNamespace(
        id=3,
        _fields={"oos_policy": True},
        oos_policy="backorder",
        product_match_field="barcode",
        price_tolerance_pct=5.0,
    )
    review = SimpleNamespace(id=7)
    return parsed_line, so, partner, review


# -- Tests -------------------------------------------------------------------

class TestIndentSkipsAvailabilityGate:

    def test_indent_line_raises_no_shortfall_issue(self):
        """Indent stock is absent by design until the shipment lands, so the
        gate must be skipped rather than reporting every line short."""
        proc = _make_processor(available=0.0)
        blocking = proc._process_order_line(*_make_args(is_indent=True))

        assert blocking == []
        assert [i for i in proc._issues.created
                if i["issue_type"] == "qty_shortfall"] == []

    def test_indent_line_leaves_shortfall_zero(self):
        proc = _make_processor(available=0.0)
        proc._process_order_line(*_make_args(is_indent=True))

        sol = proc._sols.created[0]
        assert sol.product_uom_qty == 10.0
        assert sol.edi_qty_shortfall == 0.0


class TestBackorderPartnerWarnsWithoutShortfall:

    def test_short_availability_raises_a_warning_issue(self):
        proc = _make_processor(available=4.0)
        blocking = proc._process_order_line(*_make_args())

        assert blocking == [], "backorder shortfall is warning-only"
        shortfall_issues = [i for i in proc._issues.created
                            if i["issue_type"] == "qty_shortfall"]
        assert len(shortfall_issues) == 1
        assert shortfall_issues[0]["severity"] == "warning"
        assert "6" in shortfall_issues[0]["description"]

    def test_warning_branch_does_not_write_edi_qty_shortfall(self):
        """edi_qty_shortfall is contractually ordered-minus-confirmed and drives
        the ORDRSP line action. A backorder partner is shipped in full, so the
        warning must not turn the ACK into a CHANGED/rejected line."""
        proc = _make_processor(available=4.0)
        proc._process_order_line(*_make_args())

        sol = proc._sols.created[0]
        assert sol.product_uom_qty == 10.0
        assert sol.edi_qty_shortfall == 0.0

    def test_sufficient_availability_raises_nothing(self):
        proc = _make_processor(available=25.0)
        proc._process_order_line(*_make_args())

        assert [i for i in proc._issues.created
                if i["issue_type"] == "qty_shortfall"] == []
        assert proc._sols.created[0].edi_qty_shortfall == 0.0


class TestDeadWarehouseContextKeyIsGone:

    def test_source_no_longer_uses_the_warehouse_context_key(self):
        """Odoo 19 stock reads `warehouse_id` / `search_warehouse` only, so a
        `warehouse` context key silently yields company-wide availability."""
        assert "'warehouse':" not in _SRC
        assert "wh_ctx" not in _SRC

    def test_backorder_branch_uses_the_dc_scoped_helper(self):
        assert "_dc_available_qty" in _SRC
        assert _SRC.count("self._dc_available_qty(product, so)") == 2
