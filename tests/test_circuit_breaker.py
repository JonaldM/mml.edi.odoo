"""
Circuit breaker logic for EDI FTP polling.
Tests the open/close/half-open state transitions on edi.trading.partner.

Pure Python — no Odoo runtime needed.
Run with:  pytest mml_edi/tests/test_circuit_breaker.py -v
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock


def _make_partner(failure_count=0, open_since=None, threshold=5, cooldown=60):
    """Build a minimal mock trading partner with circuit breaker state."""
    partner = MagicMock()
    partner.code = "TEST"
    partner.circuit_failure_count = failure_count
    partner.circuit_open_since = open_since
    partner.circuit_failure_threshold = threshold
    partner.circuit_cooldown_minutes = cooldown
    return partner


class TestCircuitBreakerState:

    def test_circuit_is_closed_with_no_failures(self):
        from mml_edi.models.edi_trading_partner import circuit_is_open
        partner = _make_partner(failure_count=0)
        assert circuit_is_open(partner) is False

    def test_circuit_opens_after_threshold_failures(self):
        from mml_edi.models.edi_trading_partner import circuit_is_open
        now = datetime.now(timezone.utc)
        partner = _make_partner(failure_count=5, open_since=now)
        assert circuit_is_open(partner) is True

    def test_circuit_is_half_open_after_cooldown_expires(self):
        from mml_edi.models.edi_trading_partner import circuit_is_open
        # Circuit opened 90 minutes ago (past the 60-min cooldown)
        old_open = datetime.now(timezone.utc) - timedelta(minutes=90)
        partner = _make_partner(failure_count=5, open_since=old_open, cooldown=60)
        # Half-open: should return False (allow one attempt)
        assert circuit_is_open(partner) is False

    def test_circuit_stays_open_within_cooldown(self):
        from mml_edi.models.edi_trading_partner import circuit_is_open
        # Circuit opened 30 minutes ago, cooldown is 60 min
        recent_open = datetime.now(timezone.utc) - timedelta(minutes=30)
        partner = _make_partner(failure_count=5, open_since=recent_open, cooldown=60)
        assert circuit_is_open(partner) is True
