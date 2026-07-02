"""Post-migration for mml_edi 19.0.1.0.3 — duplicate-SO backstop index (IDEM-1b).

Creates the partial UNIQUE index that rejects concurrent duplicate EDI SO
creates at the DB layer (both Python dedup layers are uncommitted-transaction
TOCTOU searches — see the 2026-07-02 go-live gate review, blocker IDEM-1).

sale.order.init() also creates this index (fresh installs and upgrades); this
migration is the explicit, verifiable upgrade path. The duplicate pre-check
already ran in pre-migration.py (aborting with a listing), but is repeated
here defensively so this file can never half-create the index against a dirty
table if executed standalone.

Manual verification:
    SELECT indexdef FROM pg_indexes
    WHERE indexname = 'sale_order_edi_client_ref_uniq';
"""
import logging

_logger = logging.getLogger(__name__)

# Must mirror pre-migration.py and sale.order.init().
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

CREATE_INDEX_SQL = """
    CREATE UNIQUE INDEX IF NOT EXISTS sale_order_edi_client_ref_uniq
        ON sale_order (edi_trading_partner_id, company_id, client_order_ref)
     WHERE edi_trading_partner_id IS NOT NULL
       AND client_order_ref IS NOT NULL
       AND state != 'cancel'
"""


def migrate(cr, version):
    # Pre-check duplicates and abort with a clear listing rather than
    # half-create (fail-closed; normally already enforced by pre-migration.py).
    cr.execute(DUPLICATE_SCAN_SQL)
    rows = cr.fetchall()
    if rows:
        listing = "\n".join(
            "  - partner_id=%s company_id=%s client_order_ref=%r: %d orders (%s)"
            % (partner_id, company_id, ref, count, ", ".join(names))
            for partner_id, company_id, ref, names, count in rows
        )
        raise RuntimeError(
            "mml_edi 19.0.1.0.3: cannot create unique index "
            "sale_order_edi_client_ref_uniq — %d duplicate group(s) of live "
            "EDI sale orders exist:\n%s\n"
            "Cancel or merge the duplicates, then re-run the upgrade."
            % (len(rows), listing)
        )
    cr.execute(CREATE_INDEX_SQL)
    _logger.info(
        "mml_edi 19.0.1.0.3: unique index sale_order_edi_client_ref_uniq "
        "ensured (duplicate-SO DB backstop)"
    )
