"""Tests for EDIService."""


class _FakeEnv:
    pass


def test_edi_service_importable():
    from mml_edi.services.edi_service import EDIService
    assert EDIService is not None


def test_edi_service_constructor_stores_env():
    from mml_edi.services.edi_service import EDIService
    svc = EDIService(_FakeEnv())
    assert svc.env is not None


def test_edi_service_has_on_3pl_despatch_confirmed():
    from mml_edi.services.edi_service import EDIService
    assert callable(getattr(EDIService, 'on_3pl_despatch_confirmed', None))


def test_on_3pl_despatch_confirmed_returns_none():
    """Stub method must not raise and must return None."""
    from mml_edi.services.edi_service import EDIService

    class FakeEvent:
        res_id = 1

    svc = EDIService(_FakeEnv())
    result = svc.on_3pl_despatch_confirmed(FakeEvent())
    assert result is None
