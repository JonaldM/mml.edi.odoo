import logging

_logger = logging.getLogger(__name__)


def _register_edi_platform(env) -> None:
    """Register all mml_edi capabilities and service in the platform layer.

    Extracted so both post_init_hook and the upgrade post-migration can call
    the same logic.  Both operations are idempotent:
      - mml.capability.register() is a no-op if the capability rows already exist.
      - mml.registry.register() overwrites the ir.config_parameter class path,
        which is safe to repeat (same value is written on re-run).

    Args:
        env: Odoo Environment instance.
    """
    from odoo.addons.mml_edi.services.edi_service import EDIService

    env['mml.capability'].register([
        'edi.order.process',
        'edi.asn.send',
        'edi.invoice.send',
    ], module='mml_edi')
    env['mml.registry'].register('edi', EDIService)
    _logger.info('mml_edi: registered EDI platform capabilities and service')


def post_init_hook(env) -> None:
    """Register EDI platform capabilities and service on fresh install."""
    _register_edi_platform(env)


def uninstall_hook(env) -> None:
    """Deregister EDI platform capabilities and service on uninstall."""
    env['mml.capability'].deregister_module('mml_edi')
    env['mml.registry'].deregister('edi')
    env['mml.event.subscription'].deregister_module('mml_edi')
