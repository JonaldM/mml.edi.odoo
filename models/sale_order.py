# mml.edi/models/sale_order.py
from odoo import api, fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    edi_trading_partner_id = fields.Many2one(
        "edi.trading.partner",
        ondelete="set null",
        index=True,
        string="EDI Trading Partner",
        help="Set when this SO was created from an EDI order",
    )
    edi_review_id = fields.Many2one(
        "edi.order.review",
        ondelete="set null",
        index=True,
        string="EDI Review",
    )
    is_edi_order = fields.Boolean(
        compute="_compute_is_edi_order",
        store=True,
        string="EDI Order",
    )

    @api.depends("edi_trading_partner_id")
    def _compute_is_edi_order(self):
        for order in self:
            order.is_edi_order = bool(order.edi_trading_partner_id)

    def action_view_edi_review(self):
        self.ensure_one()
        if not self.edi_review_id:
            return {"type": "ir.actions.act_window_close"}
        return {
            "type": "ir.actions.act_window",
            "res_model": "edi.order.review",
            "res_id": self.edi_review_id.id,
            "view_mode": "form",
            "target": "current",
        }


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    edi_line_number = fields.Integer(string="EDI Line #")
    edi_price = fields.Float(
        string="EDI Price",
        digits="Product Price",
        help="Price as received in the EDI file",
    )
    edi_system_price = fields.Float(
        string="System Price at EDI",
        digits="Product Price",
        help="Pricelist price at time of EDI processing",
    )
    edi_price_discrepancy = fields.Boolean(
        compute="_compute_edi_price_discrepancy",
        store=True,
        string="Price Discrepancy",
    )
    edi_qty_shortfall = fields.Float(
        string="Stock Shortfall",
        digits="Product Unit of Measure",
        help="Qty requested minus qty available at time of EDI processing (0 if sufficient)",
    )
    edi_matched_by = fields.Selection(
        [
            ("barcode", "Barcode (EAN-13)"),
            ("default_code", "Internal Reference"),
            ("supplier_sku", "Supplier Code"),
        ],
        string="Matched By",
        help="Product lookup strategy that succeeded for this EDI line",
    )

    @api.depends("edi_price", "edi_system_price")
    def _compute_edi_price_discrepancy(self):
        for line in self:
            line.edi_price_discrepancy = (
                line.edi_price > 0
                and line.edi_system_price > 0
                and round(line.edi_price, 4) != round(line.edi_system_price, 4)
            )
