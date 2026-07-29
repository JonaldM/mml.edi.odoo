import base64
import logging
from datetime import datetime, timezone

_logger = logging.getLogger(__name__)

# Parser classes routed to the Nimbrel DESADV path. Kept as a small frozenset
# (like edi_trading_partner._ALLOWED_PARSER_CLASSES) rather than a field on
# edi.trading.partner, so adding a future EDIFACT-DESADV partner is a
# one-line change here with no migration.
_NIMBREL_PARSER_CLASSES = frozenset({
    "mml_edi.parsers.nimbrel.NimbrelParser",
})


class EDIService:
    """Public API for mml_edi. Retrieved via mml.registry.service('edi')."""

    def __init__(self, env):
        self.env = env

    def on_3pl_despatch_confirmed(self, event) -> None:
        """
        Called when a 3PL confirms despatch of a stock.picking.
        Generates and uploads the trading partner's despatch-advice document
        to their EDI outbox.

        Gated by ir.config_parameter 'mml_edi.asn_enabled' = '1'.
        Default is '0' — activate once the legacy .NET service is retired.

        event.res_model = 'stock.picking'
        event.res_id    = stock.picking id

        Per-partner dispatch (AN-03/OPS-6): the despatch hook used to be
        hardcoded to the Kestrelby iDOC ASN format. It now routes on the
        trading partner's parser_class, exactly like edi.trading_partner.
        get_parser_instance() does for inbound parsing — Kestrelby keeps its
        existing behaviour byte-for-byte (see _dispatch_kestrelby /
        _build_despatch_dict_kestrelby / _generate_and_upload_asn_kestrelby,
        unchanged from the pre-refactor implementation), Nimbrel gets a new
        DESADV path (_dispatch_nimbrel).
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

        partner = sale_order.edi_trading_partner_id
        if not partner:
            _logger.info(
                'EDI ASN: no EDI trading partner for SO %s — skipping', sale_order.name
            )
            return

        if partner.parser_class in _NIMBREL_PARSER_CLASSES:
            self._dispatch_nimbrel(picking, sale_order, partner)
        else:
            self._dispatch_kestrelby(picking, sale_order, partner)

    # ── Kestrelby path (pre-existing behaviour, unchanged) ───────────────────

    def _dispatch_kestrelby(self, picking, sale_order, partner) -> None:
        despatch = self._build_despatch_dict_kestrelby(picking, sale_order, partner)
        if not despatch:
            return

        try:
            self._generate_and_upload_asn_kestrelby(despatch, partner, picking)
        except Exception:
            _logger.exception(
                'EDI ASN: failed to generate/upload ASN for picking %s', picking.name
            )
            self.env['edi.log'].log(
                partner, 'outbound', 'error', 'error',
                'ASN generation failed for picking %s' % picking.name,
                detail='See server log for traceback.',
            )

    def _build_despatch_dict_kestrelby(self, picking, sale_order, partner) -> dict:
        """Extract despatch data from the stock.picking for the Kestrelby ASN generator."""
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
                from odoo.exceptions import UserError
                raise UserError(
                    "Cannot generate ASN: product '%s' "
                    "(ref: %s) has no valid EAN-13 barcode. "
                    "Assign a barcode before confirming despatch." % (
                        move.product_id.display_name,
                        move.product_id.default_code or 'N/A',
                    )
                )
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

    def _generate_and_upload_asn_kestrelby(self, despatch: dict, partner, picking) -> None:
        """Generate DESADV bytes and upload to partner EDIS VAN /ToEDIS/ outbox."""
        from ..parsers.kestrelby_asn import KestrelbyASNGenerator
        from ..models.edi_ftp import get_transport_handler

        gen = KestrelbyASNGenerator()
        asn_content = gen.generate(despatch).encode('ascii')

        filename = 'DESADV_{po}_{date}.edi'.format(
            po=despatch['po_number'],
            date=despatch['despatch_date'],
        )

        handler = get_transport_handler(partner)
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
        picking.message_post(body='ASN sent to Kestrelby: %s' % filename)
        self.env['edi.log'].log(
            partner, 'outbound', 'ack_sent', 'success',
            'DESADV uploaded to EDIS VAN: %s (%d bytes)' % (filename, len(asn_content)),
            filename=filename,
        )
        _logger.info('EDI ASN: uploaded %s (%d bytes)', filename, len(asn_content))

    # ── Nimbrel path (AN-03/OPS-6: new) ────────────────────────────────────

    def _dispatch_nimbrel(self, picking, sale_order, partner) -> None:
        try:
            payload = self._build_despatch_dict_nimbrel(picking, sale_order, partner)
        except Exception:
            _logger.exception(
                'EDI ASN: failed to build Nimbrel DESADV payload for picking %s',
                picking.name,
            )
            self.env['edi.log'].log(
                partner, 'outbound', 'error', 'error',
                'DESADV payload build failed for picking %s' % picking.name,
                detail='See server log for traceback.',
                sale_order=sale_order,
            )
            return
        if not payload:
            return

        try:
            self._generate_and_upload_desadv_nimbrel(payload, partner, picking, sale_order)
        except Exception:
            _logger.exception(
                'EDI ASN: failed to generate/upload Nimbrel DESADV for picking %s',
                picking.name,
            )
            self.env['edi.log'].log(
                partner, 'outbound', 'error', 'error',
                'DESADV generation/upload failed for picking %s' % picking.name,
                detail='See server log for traceback.',
                sale_order=sale_order,
            )

    def _build_despatch_dict_nimbrel(self, picking, sale_order, partner) -> dict:
        """Build the ``parsers.nimbrel_desadv.build_desadv`` payload from a
        stock.picking, minting one SSCC per logistic unit (one carton per
        product-carrying move; see _pack_units_for_nimbrel) and determining
        partial vs complete shipment status (scenario 5A/5B).

        Returns {} (no-op, logged) when the picking has nothing shippable —
        mirrors the Kestrelby path's own empty-deliveries no-op.
        """
        from odoo.exceptions import UserError

        done_moves = picking.move_ids.filtered(
            lambda m: m.state == 'done' and m.quantity > 0
        )
        if not done_moves:
            _logger.warning(
                'EDI ASN: no done moves for picking %s — DESADV not sent', picking.name
            )
            return {}

        review = sale_order.edi_review_id
        isc_by_line, vendor_by_line = self._nimbrel_item_identity_map(
            partner, review, sale_order,
        )

        store_code = sale_order.edi_review_id.store_code if sale_order.edi_review_id else None
        if not store_code:
            # Fallback: partner.ref on the delivery address, mirroring the
            # inbound NAD+ST convention (store_code is looked up the same
            # way the processor resolves it on inbound — see
            # edi_processor._resolve_store_partner and its ref lookup).
            store_code = (
                sale_order.partner_shipping_id.ref
                or sale_order.partner_id.ref
                or ''
            )
        if not store_code:
            raise UserError(
                "Cannot generate Nimbrel DESADV for %s: no store code resolvable "
                "(neither the originating review nor the shipping partner has one)."
                % sale_order.name
            )

        po_number = sale_order.client_order_ref or sale_order.name
        units, cnt_qty_by_line = self._pack_units_for_nimbrel(
            picking, done_moves, isc_by_line, vendor_by_line,
        )
        if not units:
            _logger.warning(
                'EDI ASN: no packable lines for picking %s — DESADV not sent',
                picking.name,
            )
            return {}

        shipped_status = self._nimbrel_shipment_status(
            partner, sale_order, po_number, cnt_qty_by_line,
        )

        connote = getattr(picking, 'carrier_tracking_ref', None) or picking.name

        now = datetime.now(timezone.utc)
        payload = {
            'advice_no': 'DESADV-%s' % picking.name.replace('/', ''),
            'doc_date': now.strftime('%Y%m%d'),
            'despatch_date': (picking.date_done or now).strftime('%Y%m%d')
            if hasattr(picking, 'date_done') else now.strftime('%Y%m%d'),
            'po': po_number,
            'connote': connote,
            'buyer': None,       # filled by get_unb_recipient() at build time
            'ship_to': store_code,
            'supplier': partner.nimbrel_vendor_code or partner.code,
            'shipment_totals': {
                'units': len(units),
                'unit_pac_type': 'CT',
            },
            'units': units,
        }
        if shipped_status == 'partial':
            payload['split'] = True
            payload['ali_code'] = '165'
        elif shipped_status == 'complete_after_partial':
            payload['split'] = False
            payload['ali_code'] = '164'
        # 'complete_single_shipment' -> no ALI segment (spec: "not necessary
        # to send this segment" when the order ships in one go).

        return payload

    def _nimbrel_item_identity_map(self, partner, review, sale_order):
        """Return ({edi_line_number: isc}, {edi_line_number: vendor_code}) by
        re-parsing the review's stored raw interchange (same source ORDRSP
        uses — see parsers/nimbrel.py::_review_to_ordrsp_payload). DESADV
        can fire long after parse-time, so ISC (Nimbrel' own item number,
        PIA+5:IN) is not assumed to be persisted anywhere on sale.order.line
        — it is re-derived from the original inbound ORDERS interchange,
        the single source of truth for that identifier.

        Falls back to an empty map (never raises) when no review/raw data is
        available — the DESADV builder degrades to using the live product's
        default_code for PIA+1:SA and omits PIA+5:IN's ISC only if truly
        unrecoverable (see _pack_units_for_nimbrel).
        """
        isc_by_line, vendor_by_line = {}, {}
        if not review or not review.edi_raw_data:
            return isc_by_line, vendor_by_line
        try:
            parser = partner.get_parser_instance()
            orders = parser.parse_file(
                review.edi_raw_data.encode('iso-8859-1', errors='replace'),
                partner,
            )
        except Exception:
            _logger.warning(
                'EDI ASN: could not re-parse review %s raw data for ISC identity '
                '— PIA+5:IN will be omitted where unresolved', review.id,
                exc_info=True,
            )
            return isc_by_line, vendor_by_line
        for order in orders:
            if order.po_number != review.customer_po_number:
                continue
            for line in order.lines:
                if line.buyer_article_no:
                    isc_by_line[line.line_number] = line.buyer_article_no
                if line.vendor_code:
                    vendor_by_line[line.line_number] = line.vendor_code
        return isc_by_line, vendor_by_line

    def _pack_units_for_nimbrel(self, picking, done_moves, isc_by_line, vendor_by_line):
        """One logistic unit (carton) per shipped move line, each minting its
        own SSCC via sscc.register.get_or_create() (idempotent — re-running
        this for the same picking never re-mints).

        Returns (units_list, {edi_line_number: shipped_qty}) — the latter
        feeds partial/complete shipment-status detection.
        """
        Register = self.env['sscc.register']
        units = []
        cnt_qty_by_line = {}
        for idx, move in enumerate(done_moves, start=1):
            sol = move.sale_line_id
            line_no = sol.edi_line_number if sol else idx
            isc = isc_by_line.get(line_no)
            vendor_code = (
                (sol.product_id.default_code if sol and sol.product_id else None)
                or vendor_by_line.get(line_no)
                or ''
            )
            if not isc:
                _logger.warning(
                    'EDI ASN: no ISC resolvable for picking %s line %s — '
                    'falling back to internal reference for PIA+5:IN',
                    picking.name, line_no,
                )
                isc = vendor_code or (move.product_id.default_code or '')

            unit_key = 'carton-%s' % (sol.id if sol else move.id)
            sscc_row = Register.get_or_create(picking, unit_key, unit_type='carton')

            gtin = move.product_id.barcode or None
            qty = move.quantity
            units.append({
                'cps_idx': idx + 1,
                'pac_type': 'CT',
                'sscc': sscc_row.sscc,
                'line_no': str(line_no),
                'gtin': gtin,
                'isc': isc,
                'vendor_code': vendor_code or isc,
                'qty': str(int(round(qty))),
            })
            cnt_qty_by_line[line_no] = cnt_qty_by_line.get(line_no, 0.0) + qty
        return units, cnt_qty_by_line

    def _nimbrel_shipment_status(self, partner, sale_order, po_number, cnt_qty_by_line):
        """Determine partial / complete_after_partial / complete_single_shipment
        for THIS despatch by comparing cumulative shipped qty (this DESADV +
        every prior DESADV logged for this PO) against each SO line's ordered
        qty. Prior DESADVs are counted via edi.log rows this service itself
        writes (event_type='ack_sent', filename LIKE 'DESADV_NIMBREL_<po>_%')
        — consistent with how the Kestrelby path already treats edi.log as the
        durable record of what has been sent (no new persisted model needed
        beyond sscc.register, which is scoped to labels/uniqueness, not
        shipment-completeness accounting).
        """
        prior_logs = self.env['edi.log'].search([
            ('trading_partner_id', '=', partner.id),
            ('event_type', '=', 'ack_sent'),
            ('status', '=', 'success'),
            ('sale_order_id', '=', sale_order.id),
            ('filename', 'like', 'DESADV_NIMBREL_%'),
        ])
        had_prior_desadv = bool(prior_logs)

        order_line_qty = {
            sol.edi_line_number: sol.product_uom_qty
            for sol in sale_order.order_line
            if sol.edi_line_number
        }
        all_lines_fully_shipped = all(
            cnt_qty_by_line.get(line_no, 0.0) + 1e-6 >= ordered_qty
            for line_no, ordered_qty in order_line_qty.items()
        ) if order_line_qty else True

        if all_lines_fully_shipped:
            return 'complete_after_partial' if had_prior_desadv else 'complete_single_shipment'
        return 'partial'

    def _generate_and_upload_desadv_nimbrel(self, payload: dict, partner, picking, sale_order) -> None:
        """Build the DESADV interchange via parsers.nimbrel_desadv.build_desadv
        (using the partner's real UNB identity per contracts C1/C3) and upload
        it to the partner's EDIS/SPS outbox."""
        from ..parsers.nimbrel_desadv import build_desadv
        from ..models.edi_ftp import get_transport_handler

        recipient_id, recipient_qual = partner.get_unb_recipient()
        sender_id, sender_qual = partner.get_unb_sender()
        ctrl_ref = self.env['ir.sequence'].sudo().next_by_code(
            'mml_edi.nimbrel.interchange.ref'
        )
        if not ctrl_ref:
            raise ValueError(
                "mml_edi.nimbrel.interchange.ref sequence is not configured"
            )

        payload = dict(payload, buyer=recipient_id)

        # build_desadv's UNB is built inline (not via build_unb_for_partner),
        # so pass the real identity through its supplier_gln kwarg (sender
        # side of the envelope — see nimbrel_desadv.build_desadv's
        # supplier_gln/ctrl_ref kwargs, mirrored on build_ordrsp/build_contrl).
        desadv_bytes = build_desadv(payload, supplier_gln=sender_id, ctrl_ref=int(ctrl_ref))

        po_for_filename = payload['po'].replace('/', '')
        filename = 'DESADV_NIMBREL_%s_%s.edi' % (
            po_for_filename, payload['doc_date'],
        )

        handler = get_transport_handler(partner)
        with handler.connection():
            handler.upload_file(filename, desadv_bytes)

        self.env['ir.attachment'].create({
            'name': filename,
            'datas': base64.b64encode(desadv_bytes).decode(),
            'res_model': 'stock.picking',
            'res_id': picking.id,
            'mimetype': 'text/plain',
        })
        picking.message_post(body='DESADV sent to Nimbrel: %s' % filename)
        self.env['edi.log'].log(
            partner, 'outbound', 'ack_sent', 'success',
            'DESADV uploaded for %s: %s (%d bytes)' % (
                payload['po'], filename, len(desadv_bytes),
            ),
            filename=filename,
            sale_order=sale_order,
        )
        _logger.info(
            'EDI ASN: uploaded Nimbrel DESADV %s (%d bytes)',
            filename, len(desadv_bytes),
        )
