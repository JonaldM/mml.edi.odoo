# mml.edi/models/sale_report.py
"""Exposes indent-revenue fields on sale.report (Sales Analysis pivot).

sale.report is a SQL view; extension is via the _select_additional_fields()
and _group_by_sale() hooks rather than plain ORM inheritance. Alias 's' is
sale_order in the base query. Lets revenue reporting bucket indent orders
into their committed delivery month instead of the order month.
"""
from odoo import fields, models


class SaleReport(models.Model):
    _inherit = "sale.report"

    x_is_indent = fields.Boolean(string="Indent Order", readonly=True)
    x_expected_revenue_date = fields.Date(string="Expected Revenue Date", readonly=True)

    def _select_additional_fields(self):
        res = super()._select_additional_fields()
        res["x_is_indent"] = "s.x_is_indent"
        res["x_expected_revenue_date"] = "s.x_expected_revenue_date"
        return res

    def _group_by_sale(self):
        return super()._group_by_sale() + """,
            s.x_is_indent,
            s.x_expected_revenue_date"""
