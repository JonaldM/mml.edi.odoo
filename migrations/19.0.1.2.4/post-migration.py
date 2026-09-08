"""Post-migration for mml_edi 19.0.1.2.4 - lift the Animates control-ref floor.

The Animates UNB 0020 interchange control reference is drawn from the
mml_edi.animates.interchange.ref sequence, which shipped with no prefix,
padding 1, increment 1 and number_next 1 - so it emits the bare integers
1, 2, 3, ... and would eventually emit exactly 12341, 78401 and 99101, the
placeholder sentinels animates_edifact.build_unb(require_real=True) rejects.
Every ORDRSP/CONTRL/DESADV/INVOIC minted on one of those refs would hard-fail
at send time.

data/ir_sequence.xml now declares number_next 1000000, but that record is
noupdate="1", so an existing database keeps its low counter. Raise it here.
Idempotent: only ever moves the counter up, never back.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

SEQUENCE_CODE = "mml_edi.animates.interchange.ref"
CONTROL_REF_FLOOR = 1000000


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    sequence = env["ir.sequence"].search([("code", "=", SEQUENCE_CODE)], limit=1)
    if not sequence:
        _logger.info(
            "mml_edi 19.0.1.2.4 post-migration: sequence %s not present, "
            "nothing to raise", SEQUENCE_CODE,
        )
        return
    current = sequence.number_next_actual
    if current >= CONTROL_REF_FLOOR:
        _logger.info(
            "mml_edi 19.0.1.2.4 post-migration: %s already at %d, above the "
            "placeholder-sentinel range", SEQUENCE_CODE, current,
        )
        return
    sequence.number_next_actual = CONTROL_REF_FLOOR
    _logger.info(
        "mml_edi 19.0.1.2.4 post-migration: raised %s from %d to %d so it can "
        "never emit a placeholder control ref",
        SEQUENCE_CODE, current, CONTROL_REF_FLOOR,
    )
