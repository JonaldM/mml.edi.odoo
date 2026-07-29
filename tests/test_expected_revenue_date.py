"""Pure tests for x_expected_revenue_date (sale.order + sale.report).

Revenue reporting currently buckets by date_order, which mis-places indent
revenue (forward commitments invoiced on delivery, months after date_order).
x_expected_revenue_date is the canonical field the forecasting suite and
revenue reports should bucket by instead: commitment_date for indents,
date_order for everything else.
"""
from datetime import datetime
import pathlib
import re

from mml_edi.models.sale_order import _expected_revenue_date

_SALE_ORDER_SRC = (pathlib.Path(__file__).parent.parent / "models" /
                    "sale_order.py").read_text(encoding="utf-8")
_SALE_REPORT_SRC = (pathlib.Path(__file__).parent.parent / "models" /
                     "sale_report.py").read_text(encoding="utf-8")


# ── _expected_revenue_date (pure helper) ───────────────────────────────────

def test_indent_with_commitment_date_uses_commitment_date():
    commitment = datetime(2026, 10, 1, 9, 30)
    order_date = datetime(2026, 2, 25, 8, 0)
    assert _expected_revenue_date(True, commitment, order_date) == commitment.date()


def test_non_indent_uses_date_order():
    commitment = datetime(2026, 10, 1, 9, 30)
    order_date = datetime(2026, 2, 25, 8, 0)
    assert _expected_revenue_date(False, commitment, order_date) == order_date.date()


def test_indent_without_commitment_date_falls_back_to_date_order():
    order_date = datetime(2026, 2, 25, 8, 0)
    assert _expected_revenue_date(True, False, order_date) == order_date.date()


def test_no_dates_at_all_is_false():
    assert _expected_revenue_date(False, False, False) is False
    assert _expected_revenue_date(True, False, False) is False


# ── sale.order field declaration ───────────────────────────────────────────

def test_field_declared_stored_and_indexed():
    assert re.search(
        r"x_expected_revenue_date\s*=\s*fields\.Date\(",
        _SALE_ORDER_SRC,
    ), "x_expected_revenue_date field not declared as fields.Date"
    field_block = re.search(
        r"x_expected_revenue_date\s*=\s*fields\.Date\((.*?)\)\n\n",
        _SALE_ORDER_SRC,
        re.DOTALL,
    )
    assert field_block, "could not isolate x_expected_revenue_date field block"
    assert "store=True" in field_block.group(1)
    assert "index=True" in field_block.group(1)
    assert 'compute="_compute_x_expected_revenue_date"' in field_block.group(1)


def test_compute_depends_on_indent_commitment_and_order_date():
    assert re.search(
        r'@api\.depends\("x_is_indent",\s*"commitment_date",\s*"date_order"\)\s*\n'
        r'\s*def _compute_x_expected_revenue_date',
        _SALE_ORDER_SRC,
    ), "compute method missing correct @api.depends"


def test_compute_delegates_to_pure_helper():
    assert "_expected_revenue_date(" in _SALE_ORDER_SRC.split(
        "def _compute_x_expected_revenue_date"
    )[1].split("\n\n")[0]


# ── sale.report wiring ──────────────────────────────────────────────────────

def test_sale_report_inherits_sale_report():
    assert 'class SaleReport(models.Model):' in _SALE_REPORT_SRC
    assert '_inherit = "sale.report"' in _SALE_REPORT_SRC


def test_sale_report_select_additional_fields_wires_both_columns():
    method_src = _SALE_REPORT_SRC.split("def _select_additional_fields")[1].split(
        "def _group_by_sale"
    )[0]
    assert '"x_is_indent"' in method_src and "s.x_is_indent" in method_src
    assert '"x_expected_revenue_date"' in method_src and "s.x_expected_revenue_date" in method_src
    assert "super()._select_additional_fields()" in method_src


def test_sale_report_group_by_sale_wires_both_columns():
    method_src = _SALE_REPORT_SRC.split("def _group_by_sale")[1]
    assert "s.x_is_indent" in method_src
    assert "s.x_expected_revenue_date" in method_src
    assert "super()._group_by_sale()" in method_src
