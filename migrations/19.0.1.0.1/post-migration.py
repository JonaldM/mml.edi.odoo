"""Post-migration for mml_edi 19.0.1.0.1.

Ensures EDI capabilities and EDIService are registered in mml.capability /
mml.registry after an upgrade.

Why this is needed:
    post_init_hook only runs on fresh module install (-i), NOT on upgrade (-u).
    When upgrading an existing Odoo instance, the ir.config_parameter entry for
    'mml_registry.service.edi' may be absent or stale, and the mml.capability
    rows for 'edi.order.process', 'edi.asn.send', 'edi.invoice.send' may be
    missing — leaving mml.registry returning NullService for every call to
    service('edi') and every capability check returning False.

    mml.registry.register() writes the class path to ir.config_parameter so
    worker processes can re-hydrate it after fork via importlib.

    The folder is versioned 19.0.1.0.1 (one patch above the prior installed
    19.0.1.0.0) so Odoo's migration manager actually runs it on -u; a folder
    equal to the installed version is skipped.

    _register_edi_platform() is idempotent: capability.register() is a no-op
    if the rows already exist, and registry.register() safely overwrites the
    ir.config_parameter with the same value.

Manual verification:
    SELECT value FROM ir_config_parameter
    WHERE key = 'mml_registry.service.edi';
    -- Expected: 'odoo.addons.mml_edi.services.edi_service::EDIService'

    SELECT name FROM mml_capability
    WHERE module = 'mml_edi';
    -- Expected: edi.order.process, edi.asn.send, edi.invoice.send
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version: str) -> None:
    """Re-register EDI platform capabilities and service after upgrade.

    Args:
        cr: Odoo cursor (psycopg2 cursor wrapper).
        version: Module version string before the upgrade. None/empty means
            fresh install -- post_init_hook already handles that case, but
            calling _register_edi_platform() again is harmless (idempotent).
    """
    from odoo import api, SUPERUSER_ID
    from odoo.addons.mml_edi.hooks import _register_edi_platform

    env = api.Environment(cr, SUPERUSER_ID, {})
    _register_edi_platform(env)
    _logger.info(
        'mml_edi 19.0.1.0.1: re-registered EDI platform capabilities and service'
    )
