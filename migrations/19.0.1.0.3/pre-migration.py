"""Pre-migration for mml_edi 19.0.1.0.3 — duplicate-SO backstop pre-check (IDEM-1b).

19.0.1.0.3 adds a partial UNIQUE index on
sale_order (edi_trading_partner_id, company_id, client_order_ref)
WHERE edi_trading_partner_id IS NOT NULL AND client_order_ref IS NOT NULL
AND state != 'cancel' — the DB backstop against the concurrent-poll
duplicate-SO race. sale.order.init() creates that index DURING module load,
so if live duplicates already exist the upgrade would die mid-load with an
opaque psycopg2 error.

Fail CLOSED and EARLY instead: scan for duplicates here, and abort the
upgrade with a clear listing so an operator can cancel/merge the offending
orders and re-run. No DDL is performed in this file.
"""
import logging

_logger = logging.getLogger(__name__)

# Must mirror the index predicate in sale.order.init() / post-migration.py.
DUPLICATE_SCAN_SQL = """
    SELECT so.edi_trading_partner_id,
           so.company_id,
           so.client_order_ref,
           array_agg(so.name ORDER BY so.id),
           count(*)
      FROM sale_order so
     WHERE so.edi_trading_partner_id IS NOT NULL
       AND so.client_order_ref IS NOT NULL
       AND so.state != 'cancel'
  GROUP BY so.edi_trading_partner_id, so.company_id, so.client_order_ref
    HAVING count(*) > 1
"""


def format_duplicate_listing(rows) -> str:
    """Human-readable listing of duplicate live EDI SOs, one group per line."""
    return "\n".join(
        "  - partner_id=%s company_id=%s client_order_ref=%r: %d orders (%s)"
        % (partner_id, company_id, ref, count, ", ".join(names))
        for partner_id, company_id, ref, names, count in rows
    )


def migrate(cr, version):
    cr.execute(DUPLICATE_SCAN_SQL)
    rows = cr.fetchall()
    if not rows:
        _logger.info(
            "mml_edi 19.0.1.0.3 pre-migration: no duplicate live EDI SOs — "
            "unique-index creation can proceed"
        )
        return
    raise RuntimeError(
        "mml_edi upgrade ABORTED: %d group(s) of duplicate live EDI sale "
        "orders exist and would break the new unique index "
        "sale_order_edi_client_ref_uniq:\n%s\n"
        "Cancel or merge the duplicates (keep exactly one non-cancelled SO "
        "per client_order_ref), then re-run the upgrade."
        % (len(rows), format_duplicate_listing(rows))
    )
