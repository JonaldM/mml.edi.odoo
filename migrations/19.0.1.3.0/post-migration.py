"""Post-migration for mml_edi 19.0.1.3.0 — account-specific literals -> config.

19.0.1.3.0 removed every account-specific literal from mml_edi's runtime paths.
Each is now an ir.config_parameter read with NO code fallback:

  | key                                    | was hardcoded in                       |
  |----------------------------------------|----------------------------------------|
  | mml_edi.sender_id                       | services/edi_service.py (get_param fallback) |
  | mml_edi.kestrelby_buyer_gln             | parsers/kestrelby_asn.py (_KESTRELBY_GLN)    |
  | mml_edi.default_unb_recipient_id        | parsers/nimbrel_edifact.py (DEFAULT_RECIPIENT_ID)      |
  | mml_edi.default_unb_recipient_test_id   | parsers/nimbrel_edifact.py (DEFAULT_RECIPIENT_TEST_ID) |
  | mml_edi.gs1_company_prefix              | models/sscc_register.py (DEFAULT_GS1_PREFIX) |
  | mml_edi.notify_from                     | models/edi_processor.py (_edi_mailer fallback) |

Removing a fallback silently changes behaviour for the deployment that was
relying on it, so this migration seeds each key with that deployment's own
previous value — but only when the key is currently UNSET, so an operator who
already configured a key keeps their value.

The values themselves are account-specific and therefore do NOT live in this
file; they come from the deployment-local ``_legacy_deployment_values`` module,
which is excluded from the productised source tree. When that module is absent
(any deployment other than the one those defaults belonged to) this migration is
a no-op seeder and simply reports which keys are unconfigured.

There is deliberately no matching post_init_hook. The usual Odoo 19 rule —
post_init never fires on ``-u``, migrations never fire on a fresh install, so
registration needs both — applies to things that must exist in EVERY database
(mml_edi's capability/service registration in hooks.py is exactly that, and does
have both). Here the asymmetry is the point: a FRESH install must start with no
account-specific values at all.

Manual verification:
    SELECT key, value FROM ir_config_parameter
     WHERE key IN ('mml_edi.sender_id', 'mml_edi.kestrelby_buyer_gln',
                   'mml_edi.default_unb_recipient_id',
                   'mml_edi.default_unb_recipient_test_id',
                   'mml_edi.gs1_company_prefix', 'mml_edi.notify_from');
"""
import importlib.util
import logging
import os

_logger = logging.getLogger(__name__)

#: Sibling module holding the deployment-local legacy values. Loaded BY PATH,
#: never by import statement: Odoo runs migration scripts through
#: importlib.util.spec_from_file_location() with no package context
#: (odoo/modules/migration.py::load_script), so `from . import ...` raises
#: ImportError at runtime and would make this seeder a silent no-op.
_OVERLAY_FILENAME = "_legacy_deployment_values.py"

#: Every key this version turned from a code default into required
#: configuration. Used for the "still unconfigured" report even when there are
#: no legacy values to seed.
EXTRACTED_PARAM_KEYS = (
    "mml_edi.sender_id",
    "mml_edi.kestrelby_buyer_gln",
    "mml_edi.default_unb_recipient_id",
    "mml_edi.default_unb_recipient_test_id",
    "mml_edi.gs1_company_prefix",
    "mml_edi.notify_from",
)


def _legacy_defaults() -> dict:
    """Deployment-local legacy values, or ``{}`` when the overlay is absent.

    A missing overlay is the NORMAL case: the file is excluded from the
    productised tree, so only the deployment whose code defaults these were
    ever has one.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        _OVERLAY_FILENAME)
    if not os.path.isfile(path):
        return {}
    try:
        spec = importlib.util.spec_from_file_location(
            "mml_edi_legacy_deployment_values", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return dict(getattr(module, "LEGACY_DEFAULTS", None) or {})
    except Exception:
        # Never fail an upgrade over the overlay: worst case the keys stay
        # unset and the warning below tells the operator exactly which ones.
        _logger.exception(
            "mml_edi 19.0.1.3.0: could not read the deployment overlay at %s "
            "— continuing with no legacy values to seed", path,
        )
        return {}


def _is_unset(cr, key: str) -> bool:
    """True when ``key`` has no row, or a row whose value is blank."""
    cr.execute(
        "SELECT value FROM ir_config_parameter WHERE key = %s", (key,)
    )
    row = cr.fetchone()
    return row is None or not (row[0] or "").strip()


def migrate(cr, version):
    # Fresh installs never reach a migration; guard anyway so the file is safe
    # to execute standalone.
    if not version:
        return

    defaults = _legacy_defaults()
    seeded, kept, unconfigured = [], [], []

    for key in EXTRACTED_PARAM_KEYS:
        if not _is_unset(cr, key):
            kept.append(key)
            continue
        value = (defaults.get(key) or "").strip()
        if not value:
            unconfigured.append(key)
            continue
        # No ORM in a migration: ir.config_parameter is a plain table and
        # set_param would need a registry that is not yet loaded here.
        cr.execute(
            "INSERT INTO ir_config_parameter (key, value, create_uid, "
            "write_uid, create_date, write_date) "
            "VALUES (%s, %s, 1, 1, now() AT TIME ZONE 'UTC', "
            "now() AT TIME ZONE 'UTC') "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
            "write_uid = 1, write_date = now() AT TIME ZONE 'UTC'",
            (key, value),
        )
        seeded.append(key)

    _logger.info(
        "mml_edi 19.0.1.3.0: account-specific literals extracted to config — "
        "%d key(s) seeded from the deployment overlay (%s), %d already "
        "configured (%s)",
        len(seeded), ", ".join(seeded) or "none",
        len(kept), ", ".join(kept) or "none",
    )
    if unconfigured:
        # Not fatal: most of these only gate outbound flows that are off by
        # default (mml_edi.asn_enabled = '0'), and failing the whole upgrade
        # over an unconfigured optional identity would be worse than the
        # fail-closed error the runtime raises at first use.
        _logger.warning(
            "mml_edi 19.0.1.3.0: %d account-specific system parameter(s) are "
            "not configured: %s. The code defaults that used to cover them "
            "have been removed; the affected features now fail closed with an "
            "explicit error until an operator sets them.",
            len(unconfigured), ", ".join(unconfigured),
        )
