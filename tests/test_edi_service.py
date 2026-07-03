"""Tests for EDIService."""
from unittest.mock import MagicMock


def _make_env_with_asn_disabled():
    """Return a minimal env mock where mml_edi.asn_enabled = '0' (gate closed)."""
    config_param = MagicMock()
    config_param.sudo.return_value = config_param
    config_param.get_param.return_value = '0'

    env = MagicMock()
    env.__getitem__ = MagicMock(return_value=config_param)
    return env


def test_edi_service_importable():
    from mml_edi.services.edi_service import EDIService
    assert EDIService is not None


def test_edi_service_constructor_stores_env():
    from mml_edi.services.edi_service import EDIService
    env = _make_env_with_asn_disabled()
    svc = EDIService(env)
    assert svc.env is not None


def test_edi_service_has_on_3pl_despatch_confirmed():
    from mml_edi.services.edi_service import EDIService
    assert callable(getattr(EDIService, 'on_3pl_despatch_confirmed', None))


def test_on_3pl_despatch_confirmed_returns_none_when_disabled():
    """Gate check: when mml_edi.asn_enabled='0' method returns None without processing."""
    from mml_edi.services.edi_service import EDIService

    class FakeEvent:
        res_id = 1
        res_model = 'stock.picking'

    env = _make_env_with_asn_disabled()
    svc = EDIService(env)
    result = svc.on_3pl_despatch_confirmed(FakeEvent())
    assert result is None


# --- Per-partner dispatch routing (AN-03/OPS-6) -----------------------------

def test_animates_parser_classes_contains_animates_parser():
    from mml_edi.services.edi_service import _ANIMATES_PARSER_CLASSES
    assert 'mml_edi.parsers.animates.AnimatesParser' in _ANIMATES_PARSER_CLASSES


def test_animates_parser_classes_excludes_briscoes():
    from mml_edi.services.edi_service import _ANIMATES_PARSER_CLASSES
    assert 'mml_edi.parsers.briscoes_idoc.BriscoesIDOCParser' not in _ANIMATES_PARSER_CLASSES


def test_dispatch_routes_to_briscoes_by_default():
    """A partner whose parser_class is NOT in _ANIMATES_PARSER_CLASSES must
    route through _dispatch_briscoes — proves the refactor kept Briscoes'
    call path byte-identical (same method, same signature) rather than
    silently changing behaviour for the live production partner."""
    from mml_edi.services.edi_service import EDIService, _ANIMATES_PARSER_CLASSES

    config_param = MagicMock()
    config_param.sudo.return_value = config_param
    config_param.get_param.return_value = '1'  # enabled

    picking = MagicMock()
    picking.exists.return_value = True
    sale_order = MagicMock()
    partner = MagicMock()
    partner.parser_class = 'mml_edi.parsers.briscoes_idoc.BriscoesIDOCParser'
    assert partner.parser_class not in _ANIMATES_PARSER_CLASSES
    sale_order.edi_trading_partner_id = partner
    picking.sale_id = sale_order

    env = MagicMock()

    def getitem(key):
        if key == 'ir.config_parameter':
            return config_param
        if key == 'stock.picking':
            m = MagicMock()
            m.browse.return_value = picking
            return m
        return MagicMock()

    env.__getitem__ = MagicMock(side_effect=getitem)

    svc = EDIService(env)
    svc._dispatch_briscoes = MagicMock()
    svc._dispatch_animates = MagicMock()

    class FakeEvent:
        res_id = 1
        res_model = 'stock.picking'

    svc.on_3pl_despatch_confirmed(FakeEvent())
    svc._dispatch_briscoes.assert_called_once_with(picking, sale_order, partner)
    svc._dispatch_animates.assert_not_called()


def test_dispatch_routes_to_animates_for_animates_parser():
    from mml_edi.services.edi_service import EDIService

    config_param = MagicMock()
    config_param.sudo.return_value = config_param
    config_param.get_param.return_value = '1'  # enabled

    picking = MagicMock()
    picking.exists.return_value = True
    sale_order = MagicMock()
    partner = MagicMock()
    partner.parser_class = 'mml_edi.parsers.animates.AnimatesParser'
    sale_order.edi_trading_partner_id = partner
    picking.sale_id = sale_order

    env = MagicMock()

    def getitem(key):
        if key == 'ir.config_parameter':
            return config_param
        if key == 'stock.picking':
            m = MagicMock()
            m.browse.return_value = picking
            return m
        return MagicMock()

    env.__getitem__ = MagicMock(side_effect=getitem)

    svc = EDIService(env)
    svc._dispatch_briscoes = MagicMock()
    svc._dispatch_animates = MagicMock()

    class FakeEvent:
        res_id = 1
        res_model = 'stock.picking'

    svc.on_3pl_despatch_confirmed(FakeEvent())
    svc._dispatch_animates.assert_called_once_with(picking, sale_order, partner)
    svc._dispatch_briscoes.assert_not_called()


# --- _animates_shipment_status (scenario 5A/5B partial/complete) -----------

def _make_service_for_shipment_status(prior_logs, order_lines):
    from mml_edi.services.edi_service import EDIService

    log_model = MagicMock()
    log_model.search.return_value = prior_logs

    env = MagicMock()
    env.__getitem__ = MagicMock(side_effect=lambda k: log_model if k == 'edi.log' else MagicMock())

    svc = EDIService(env)

    sale_order = MagicMock()
    sale_order.id = 99
    sale_order.order_line = order_lines
    return svc, sale_order


def _make_sol(edi_line_number, product_uom_qty):
    sol = MagicMock()
    sol.edi_line_number = edi_line_number
    sol.product_uom_qty = product_uom_qty
    return sol


def test_shipment_status_partial_when_line_not_fully_shipped():
    order_lines = [_make_sol(1, 10.0), _make_sol(2, 5.0)]
    svc, so = _make_service_for_shipment_status(prior_logs=[], order_lines=order_lines)
    partner = MagicMock()
    status = svc._animates_shipment_status(
        partner, so, 'PO0319555', {1: 10.0, 2: 3.0}  # line 2 short-shipped
    )
    assert status == 'partial'


def test_shipment_status_complete_single_shipment_no_prior_desadv():
    order_lines = [_make_sol(1, 10.0), _make_sol(2, 5.0)]
    svc, so = _make_service_for_shipment_status(prior_logs=[], order_lines=order_lines)
    partner = MagicMock()
    status = svc._animates_shipment_status(
        partner, so, 'PO0319555', {1: 10.0, 2: 5.0}
    )
    assert status == 'complete_single_shipment'


def test_shipment_status_complete_after_partial_when_prior_desadv_exists():
    order_lines = [_make_sol(1, 10.0), _make_sol(2, 5.0)]
    prior = [MagicMock()]  # a previous DESADV was logged for this SO
    svc, so = _make_service_for_shipment_status(prior_logs=prior, order_lines=order_lines)
    partner = MagicMock()
    status = svc._animates_shipment_status(
        partner, so, 'PO0319555', {1: 10.0, 2: 5.0}
    )
    assert status == 'complete_after_partial'


def test_shipment_status_no_order_lines_defaults_complete():
    svc, so = _make_service_for_shipment_status(prior_logs=[], order_lines=[])
    partner = MagicMock()
    status = svc._animates_shipment_status(partner, so, 'PO0319555', {})
    assert status == 'complete_single_shipment'
