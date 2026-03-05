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
