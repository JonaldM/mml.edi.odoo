"""Post-migration for mml_edi 19.0.1.0.2 — OOS short-ship + ACK rollout.

Backfill ``edi_ordered_qty`` = ``product_uom_qty`` on existing EDI sale order lines.
Historical lines were processed under the accept-in-full policy, so the ordered qty
equals the current line qty — safe to copy. Idempotent.

DELIBERATELY behaviour-neutral: every trading partner keeps the ``'backorder'``
default. The Kestrelby Group partner is flipped to ``oos_policy = 'short_ship'`` as a
separate, explicit go-live step ONLY AFTER Kestrelby confirm they accept a
short-confirm ORDRSP (open decision 5.4). Flipping the flag is the single switch
that activates the new behaviour — do not auto-flip it here.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # Backfill edi_ordered_qty for existing EDI lines (raw SQL — fast).
    cr.execute(
        """
        UPDATE sale_order_line sol
           SET edi_ordered_qty = product_uom_qty
          FROM sale_order so
         WHERE sol.order_id = so.id
           AND so.edi_trading_partner_id IS NOT NULL
           AND COALESCE(sol.edi_ordered_qty, 0) = 0
        """
    )
    _logger.info("mml_edi 19.0.1.0.2: backfilled edi_ordered_qty on %s EDI lines",
                 cr.rowcount)
