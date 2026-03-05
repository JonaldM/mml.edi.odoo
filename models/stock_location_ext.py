from odoo import fields, models


class StockLocationEdiExt(models.Model):
    _inherit = 'stock.location'

    edi_store_gln = fields.Char(
        string='EDI Store GLN',
        help='Briscoes store GLN for this delivery location. Used in DESADV (ASN) generation.',
    )
