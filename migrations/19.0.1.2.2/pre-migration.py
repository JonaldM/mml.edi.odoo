"""Pre-migration for mml_edi 19.0.1.2.2 — Animates->Nimbrel ir.sequence
xml_id/code rename repair (gate-repair round, cadence-sweep fixture-reseed).

data/ir_sequence.xml is noupdate="1". A prior commit (f376a7b, "fictionalize
Briscoes/Animates fixture brands in mml_edi") renamed the xml_ids
seq_animates_interchange_ref -> seq_nimbrel_interchange_ref and
seq_animates_sscc_serial -> seq_nimbrel_sscc_serial in place, and also
changed the `code` field of the interchange-ref record from
mml_edi.animates.interchange.ref to mml_edi.nimbrel.interchange.ref (the
sscc-serial record's `code` was already mml_edi.sscc.serial and was left
unchanged).

Because the block is noupdate, on -u Odoo does NOT retarget the existing
ir.model.data rows for the old xml_ids to the new ones — it creates NEW
ir.sequence rows for the new xml_ids, with number_next starting at 1. On any
DB that was already running the pre-rename version this is a live bug:

  - The interchange-ref sequence would restart at 1, risking collision with
    an interchange control reference still open with the Nimbrel partner
    (see the comment above that record in data/ir_sequence.xml).
  - The sscc-serial sequence would end up with TWO ir.sequence rows sharing
    the SAME code value mml_edi.sscc.serial (the untouched old row plus a
    newly-created row with the same code). next_by_code() picks
    nondeterministically between same-code rows, which can reissue an
    already-used SSCC serial (GS1 requires 12-month uniqueness per
    Nimbrel_DESADV.pdf p.9 — see models/sscc_register.py).

Fix: before the data file reloads, re-point the existing ir.model.data rows'
`name` column at the new xml_ids (so the data-file load recognises the
existing record instead of creating a duplicate) and update the surviving
ir.sequence row's `code` column in place. Fully idempotent — safe to run on
a DB that already has the new ids (nothing to do), on a DB that never had
the old ids (fresh install — nothing to do), and safe to re-run.
"""
import logging

_logger = logging.getLogger(__name__)

# (module, old_xmlid, new_xmlid) — ir.model.data rows to re-point.
_XMLID_RENAMES = [
    ("mml_edi", "seq_animates_interchange_ref", "seq_nimbrel_interchange_ref"),
    ("mml_edi", "seq_animates_sscc_serial", "seq_nimbrel_sscc_serial"),
]

# (old_code, new_code) — ir.sequence.code values to update in place.
# Only the interchange-ref sequence's code actually changed; the
# sscc-serial sequence's code was mml_edi.sscc.serial both before and
# after the rename, so it needs no code update (only the xml_id rename
# above matters for it).
_CODE_RENAMES = [
    ("mml_edi.animates.interchange.ref", "mml_edi.nimbrel.interchange.ref"),
]


def migrate(cr, version):
    for module, old_name, new_name in _XMLID_RENAMES:
        cr.execute(
            """
            SELECT id FROM ir_model_data
             WHERE module = %s AND name = %s
            """,
            (module, old_name),
        )
        old_row = cr.fetchone()
        if not old_row:
            _logger.info(
                "mml_edi 19.0.1.2.2 pre-migration: no ir_model_data row "
                "%s.%s found — nothing to rename (fresh install or already "
                "migrated).",
                module, old_name,
            )
            continue

        cr.execute(
            """
            SELECT id FROM ir_model_data
             WHERE module = %s AND name = %s
            """,
            (module, new_name),
        )
        if cr.fetchone():
            _logger.warning(
                "mml_edi 19.0.1.2.2 pre-migration: BOTH %s.%s and %s.%s "
                "exist — leaving as-is (unexpected state, needs manual "
                "review; refusing to guess which row is authoritative).",
                module, old_name, module, new_name,
            )
            continue

        cr.execute(
            """
            UPDATE ir_model_data
               SET name = %s
             WHERE module = %s AND name = %s
            """,
            (new_name, module, old_name),
        )
        _logger.info(
            "mml_edi 19.0.1.2.2 pre-migration: renamed ir_model_data %s.%s "
            "-> %s.%s (preserves the existing ir.sequence row/number_next "
            "instead of the data-file load creating a duplicate).",
            module, old_name, module, new_name,
        )

    for old_code, new_code in _CODE_RENAMES:
        cr.execute(
            "SELECT id FROM ir_sequence WHERE code = %s",
            (old_code,),
        )
        rows = cr.fetchall()
        if not rows:
            _logger.info(
                "mml_edi 19.0.1.2.2 pre-migration: no ir_sequence row with "
                "code %r found — nothing to update.",
                old_code,
            )
            continue
        cr.execute(
            "UPDATE ir_sequence SET code = %s WHERE code = %s",
            (new_code, old_code),
        )
        _logger.info(
            "mml_edi 19.0.1.2.2 pre-migration: updated %d ir_sequence "
            "row(s) code %r -> %r.",
            len(rows), old_code, new_code,
        )
