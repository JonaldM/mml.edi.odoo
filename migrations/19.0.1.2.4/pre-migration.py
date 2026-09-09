"""Pre-migration for mml_edi 19.0.1.2.4 - collapse the duplicate vendor code.

edi.trading.partner carried two near-identical fields: animates_vendor_code,
which every outbound Animates generator read, and vendor_code, the only one the
Trading Partner form ever exposed. 19.0.1.2.4 keeps vendor_code alone, so any
value an operator or script put in animates_vendor_code must be carried over
before the field is dropped from the model.

Copies only where vendor_code is empty, so a value typed on the form always
wins. No DDL: the orphan column is left in place for forensics.
"""
import logging

_logger = logging.getLogger(__name__)

COLUMN_EXISTS_SQL = """
    SELECT 1
      FROM information_schema.columns
     WHERE table_name = 'edi_trading_partner'
       AND column_name = 'animates_vendor_code'
"""

CARRY_OVER_SQL = """
    UPDATE edi_trading_partner
       SET vendor_code = animates_vendor_code
     WHERE animates_vendor_code IS NOT NULL
       AND animates_vendor_code != ''
       AND (vendor_code IS NULL OR vendor_code = '')
"""


def migrate(cr, version):
    cr.execute(COLUMN_EXISTS_SQL)
    if not cr.fetchone():
        _logger.info(
            "mml_edi 19.0.1.2.4 pre-migration: no animates_vendor_code column, "
            "nothing to carry over"
        )
        return
    cr.execute(CARRY_OVER_SQL)
    _logger.info(
        "mml_edi 19.0.1.2.4 pre-migration: carried animates_vendor_code into "
        "vendor_code on %d trading partner(s)", cr.rowcount,
    )
