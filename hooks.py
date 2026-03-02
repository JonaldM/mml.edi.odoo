def post_init_hook(env):
    from odoo.addons.mml_edi.services.edi_service import EDIService
    env['mml.capability'].register([
        'edi.order.process',
        'edi.asn.send',
        'edi.invoice.send',
    ], module='mml_edi')
    env['mml.registry'].register('edi', EDIService)


def uninstall_hook(env):
    env['mml.capability'].deregister_module('mml_edi')
    env['mml.registry'].deregister('edi')
    env['mml.event.subscription'].deregister_module('mml_edi')
