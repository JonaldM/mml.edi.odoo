"""Pre-migration for mml_edi 19.0.1.2.2 — Briscoes->Kestrelby /
Animates->Nimbrel rename repair (gate-repair round, cadence-sweep
fixture-reseed).

Covers four unmigrated renames introduced by commit f376a7b, each of which is
invisible at upgrade time and only surfaces as wrong data on the wire:

  1. ir.sequence xml_ids + code (see the long note below).
  2. edi_trading_partner.animates_vendor_code -> nimbrel_vendor_code. A stored
     Char field renamed in Python only: Odoo would add a fresh NULL column and
     orphan the old one, silently losing the configured supplier code (e.g.
     'V1058'). Both consumers then fall back to partner.code
     (services/edi_service.py::_build_despatch_dict_nimbrel and
     services/nimbrel_invoice.py), putting the WRONG supplier identity in
     NAD+SU / PIA+1:SA on outbound ORDRSP/DESADV/INVOIC.
  3. edi_trading_partner.parser_class values. _ALLOWED_PARSER_CLASSES is a
     fail-closed security allow-list checked BEFORE the import
     (get_parser_instance raises UserError "not in the approved list"), so a
     row still holding an old dotted path makes that partner's poll hard-fail
     on every run with no graceful degradation and no operator warning.
     wizards/edi_seed_stores.py matches on the same stored string, so a stale
     value would also silently seed the wrong store list.
  4. ir_config_parameter key mml_edi.animates_nzbn -> mml_edi.nimbrel_nzbn.
     _buyer_nzbn() falls back to buyer_partner.vat or "" when the key is
     missing, emitting a blank/wrong RFF+AMT under NAD+BY on live INVOIC — a
     failure that only surfaces as a partner-side invoice rejection.

Every step is guarded on information_schema / existence checks so it is
idempotent: safe on a fresh install (nothing to rename), safe on an already
migrated DB, and safe to re-run.

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

# (table, old_column, new_column) — stored fields renamed in Python only.
_COLUMN_RENAMES = [
    ("edi_trading_partner", "animates_vendor_code", "nimbrel_vendor_code"),
]

# (old_dotted_path, new_dotted_path) — edi_trading_partner.parser_class values.
# briscoes.BriscoesParser was never on the allow-list (EDIFACT CT->EA defect),
# but a row could still hold it; renaming it keeps the value pointing at a real
# module so the failure stays the intended "not approved", not a dangling path.
_PARSER_CLASS_RENAMES = [
    ("mml_edi.parsers.briscoes_idoc.BriscoesIDOCParser",
     "mml_edi.parsers.kestrelby_idoc.KestrelbyIDOCParser"),
    ("mml_edi.parsers.briscoes.BriscoesParser",
     "mml_edi.parsers.kestrelby.KestrelbyParser"),
    ("mml_edi.parsers.animates.AnimatesParser",
     "mml_edi.parsers.nimbrel.NimbrelParser"),
]

# (old_key, new_key) — ir_config_parameter keys.
_CONFIG_PARAM_RENAMES = [
    ("mml_edi.animates_nzbn", "mml_edi.nimbrel_nzbn"),
]


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return bool(cr.fetchone())


def _migrate_column_renames(cr):
    """Rename stored columns in place so configured values survive the upgrade.

    Guarded on information_schema so it is a no-op on a fresh install (no old
    column) and on an already-migrated DB (new column already present).
    """
    for table, old_col, new_col in _COLUMN_RENAMES:
        if not _column_exists(cr, table, old_col):
            _logger.info(
                "mml_edi 19.0.1.2.2 pre-migration: %s.%s absent — nothing to "
                "rename (fresh install or already migrated).", table, old_col,
            )
            continue
        if _column_exists(cr, table, new_col):
            _logger.warning(
                "mml_edi 19.0.1.2.2 pre-migration: BOTH %s.%s and %s.%s exist "
                "— leaving as-is (unexpected state, needs manual review; "
                "refusing to guess which column is authoritative).",
                table, old_col, table, new_col,
            )
            continue
        cr.execute(
            'ALTER TABLE "%s" RENAME COLUMN "%s" TO "%s"' % (table, old_col, new_col)
        )
        _logger.info(
            "mml_edi 19.0.1.2.2 pre-migration: renamed column %s.%s -> %s "
            "(preserves the configured supplier code instead of ORM-creating "
            "a NULL column and orphaning the old one).",
            table, old_col, new_col,
        )


def _migrate_parser_classes(cr):
    """Re-point stored parser_class dotted paths at the renamed modules.

    _ALLOWED_PARSER_CLASSES is fail-closed: an unrenamed value makes
    get_parser_instance() raise before it ever attempts the import, so every
    poll for that partner hard-fails after upgrade.
    """
    if not _column_exists(cr, "edi_trading_partner", "parser_class"):
        _logger.info(
            "mml_edi 19.0.1.2.2 pre-migration: edi_trading_partner.parser_class "
            "absent — fresh install, nothing to re-point."
        )
        return
    for old_path, new_path in _PARSER_CLASS_RENAMES:
        cr.execute(
            """
            UPDATE edi_trading_partner
               SET parser_class = %s
             WHERE parser_class = %s
            """,
            (new_path, old_path),
        )
        if cr.rowcount:
            _logger.info(
                "mml_edi 19.0.1.2.2 pre-migration: re-pointed %d "
                "edi_trading_partner row(s) parser_class %r -> %r.",
                cr.rowcount, old_path, new_path,
            )


def _migrate_config_params(cr):
    """Rename ir_config_parameter keys, keeping the operator's value.

    Skipped for any key where the NEW key already carries a value, so an
    operator who set the new key by hand is never overwritten.
    """
    for old_key, new_key in _CONFIG_PARAM_RENAMES:
        cr.execute("SELECT id FROM ir_config_parameter WHERE key = %s", (old_key,))
        if not cr.fetchone():
            _logger.info(
                "mml_edi 19.0.1.2.2 pre-migration: ir_config_parameter %r "
                "absent — nothing to rename.", old_key,
            )
            continue
        cr.execute("SELECT id FROM ir_config_parameter WHERE key = %s", (new_key,))
        if cr.fetchone():
            _logger.warning(
                "mml_edi 19.0.1.2.2 pre-migration: BOTH %r and %r exist in "
                "ir_config_parameter — keeping the new key's value and "
                "leaving the old row for manual review.", old_key, new_key,
            )
            continue
        cr.execute(
            "UPDATE ir_config_parameter SET key = %s WHERE key = %s",
            (new_key, old_key),
        )
        _logger.info(
            "mml_edi 19.0.1.2.2 pre-migration: renamed ir_config_parameter "
            "%r -> %r (preserves the configured NZBN instead of silently "
            "falling back to partner.vat on outbound INVOIC).",
            old_key, new_key,
        )


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

    _migrate_column_renames(cr)
    _migrate_parser_classes(cr)
    _migrate_config_params(cr)
