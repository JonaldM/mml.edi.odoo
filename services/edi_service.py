import logging
from datetime import datetime, timezone

_logger = logging.getLogger(__name__)


class EDIService:
    """Public API for mml_edi. Retrieved via mml.registry.service('edi')."""

    def __init__(self, env):
        self.env = env

    def on_3pl_despatch_confirmed(self, event) -> None:
        """
        Called when Mainfreight confirms despatch of a stock.picking.
        Generates and uploads a Briscoes DESADV to the EDIS VAN FTP /ToEDIS/ outbox.

        Gated by ir.config_parameter 'mml_edi.asn_enabled' = '1'.
        Default is '0' — activate once the legacy .NET service is retired.

        event.res_model = 'stock.picking'
        event.res_id    = stock.picking id
        """
        enabled = self.env['ir.config_parameter'].sudo().get_param(
            'mml_edi.asn_enabled', '0'
        )
        if enabled != '1':
            _logger.info('EDI ASN: disabled via mml_edi.asn_enabled — skipping')
            return

        if not event.res_id or event.res_model != 'stock.picking':
            _logger.warning('EDI ASN: event has no valid picking id — skipping')
            return

        picking = self.env['stock.picking'].browse(event.res_id)
        if not picking.exists():
            _logger.warning('EDI ASN: picking id=%s not found', event.res_id)
            return

        sale_order = picking.sale_id
        if not sale_order:
            _logger.info(
                'EDI ASN: picking %s has no linked SO — not an EDI order, skipping',
                picking.name,
            )
            return

        partner = self.env['edi.trading.partner'].search([
            ('partner_id', '=', sale_order.partner_id.id),
            ('active', '=', True),
        ], limit=1)
        if not partner:
            _logger.info(
                'EDI ASN: no EDI trading partner for SO %s — skipping', sale_order.name
            )
            return

        despatch = self._build_despatch_dict(picking, sale_order, partner)
        if not despatch:
            return

        try:
            self._generate_and_upload_asn(despatch, partner, picking)
        except Exception:
            _logger.exception(
                'EDI ASN: failed to generate/upload ASN for picking %s', picking.name
            )
            self.env['edi.log'].log(
                partner, 'outbound', 'error', picking.name,
                'ASN generation failed for picking %s' % picking.name,
                detail='See server log for traceback.',
            )

    def _build_despatch_dict(self, picking, sale_order, partner) -> dict:
        """Extract despatch data from the stock.picking for the ASN generator."""
        mml_edis_id = self.env['ir.config_parameter'].sudo().get_param(
            'mml_edi.sender_id', 'MMLEDI'
        )
        ctrl_ref = (
            self.env['ir.sequence'].sudo().next_by_code('edi.asn.ctrl.ref') or '1'
        )

        deliveries = {}
        seq = 10
        for move in picking.move_ids.filtered(lambda m: m.state == 'done'):
            store_gln = move.location_dest_id.edi_store_gln or ''
            if not store_gln:
                _logger.warning(
                    'EDI ASN: location %s has no edi_store_gln — line skipped',
                    move.location_dest_id.name,
                )
                continue
            barcode = move.product_id.barcode or ''
            if len(barcode) != 13:
                _logger.warning(
                    'EDI ASN: product %s has no valid EAN-13 — line skipped',
                    move.product_id.display_name,
                )
                continue
            deliveries.setdefault(store_gln, []).append({
                'ean13': barcode,
                'qty': move.quantity,
                'seq': seq,
            })
            seq += 10

        if not deliveries:
            _logger.warning(
                'EDI ASN: no valid lines for picking %s — ASN not sent', picking.name
            )
            return {}

        po_number = sale_order.client_order_ref or sale_order.name

        return {
            'po_number': po_number,
            'despatch_ref': 'DASN-%s' % po_number,
            'despatch_date': datetime.now(timezone.utc).strftime('%Y%m%d'),
            'mml_edis_id': mml_edis_id,
            'ctrl_ref': ctrl_ref,
            'deliveries': [
                {'store_gln': gln, 'lines': lines}
                for gln, lines in deliveries.items()
            ],
        }

    def _generate_and_upload_asn(self, despatch: dict, partner, picking) -> None:
        """Generate DESADV bytes and upload to partner EDIS VAN /ToEDIS/ outbox."""
        import base64
        from ..parsers.briscoes_asn import BriscoesASNGenerator
        from ..models.edi_ftp import EDIFTPHandler

        gen = BriscoesASNGenerator()
        asn_content = gen.generate(despatch).encode('ascii')

        filename = 'DESADV_{po}_{date}.edi'.format(
            po=despatch['po_number'],
            date=despatch['despatch_date'],
        )

        handler = EDIFTPHandler(partner)
        with handler.connection():
            handler.upload_file(filename, asn_content)

        # Audit trail — attachment on the picking
        self.env['ir.attachment'].create({
            'name': filename,
            'datas': base64.b64encode(asn_content).decode(),
            'res_model': 'stock.picking',
            'res_id': picking.id,
            'mimetype': 'text/plain',
        })
        picking.message_post(body='ASN sent to Briscoes: %s' % filename)
        self.env['edi.log'].log(
            partner, 'outbound', 'ack_sent', filename,
            'DESADV uploaded to EDIS VAN: %s (%d bytes)' % (filename, len(asn_content)),
        )
        _logger.info('EDI ASN: uploaded %s (%d bytes)', filename, len(asn_content))
