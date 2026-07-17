# mml.edi/models/sale_order.py
from odoo import api, fields, models

from .edi_processor import SO_EDI_CLIENT_REF_INDEX


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def init(self):
        """DB backstop for the EDI duplicate-SO race (IDEM-1b).

        At most ONE live (non-cancelled) SO per (EDI trading partner, company,
        client_order_ref). client_order_ref is rendered from the partner's
        client_ref_template and is unique per (partner, PO, store) by
        construction, so this index makes the DB reject what the Python-only
        TOCTOU dedup can miss under concurrency. Partial:
          - non-EDI orders (edi_trading_partner_id IS NULL) are untouched;
          - cancelled SOs are excluded so a re-order after cancellation (a
            deliberate processor path) stays allowed.
        The duplicate pre-check for upgrades lives in migrations/19.0.1.0.3
        (pre-migration aborts with a listing before this index is created).
        """
        super().init()
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS %s
                ON sale_order (edi_trading_partner_id, company_id, client_order_ref)
             WHERE edi_trading_partner_id IS NOT NULL
               AND client_order_ref IS NOT NULL
               AND state != 'cancel'
            """ % SO_EDI_CLIENT_REF_INDEX
        )

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
    x_is_indent = fields.Boolean(
        string="Indent Order",
        tracking=True,
        copy=False,
        index=True,
        help="Forward commitment placed ahead of stock arrival, for delivery "
             "in a future window (EDI delivery date beyond the indent "
             "threshold at import; also settable manually). Indents are "
             "accepted in full — the out-of-stock gate is skipped by design — "
             "and their deliveries are held from the 3PL until the release "
             "window before the committed date.",
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
    edi_ordered_qty = fields.Float(
        string="EDI Ordered Qty",
        digits="Product Unit of Measure",
        help="Original qty the customer ordered. When the line is short-shipped to "
             "available stock, product_uom_qty holds the shipped qty and this holds "
             "the original; the difference is edi_qty_shortfall, acknowledged in the ORDRSP.",
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
