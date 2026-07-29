"""Pure tests for indent-order detection and the OOS-gate exemption.

Indent orders are forward commitments placed months before the stock arrives
(live case: two Kestrelby POs from 25 Feb 2026 committing $128k for 1 October
delivery). They must be accepted in full — the short-ship gate would zero
every line — and flagged so operators never mistake them for stale orders.
"""
from datetime import datetime
import pathlib
import re

from mml_edi.models.edi_processor import (
    INDENT_THRESHOLD_DAYS,
    _is_indent_commitment,
)

_SRC = (pathlib.Path(__file__).parent.parent / "models" /
        "edi_processor.py").read_text(encoding="utf-8")

_NOW = datetime(2026, 7, 3, 12, 0)


def test_far_future_delivery_is_indent():
    assert _is_indent_commitment(datetime(2026, 10, 1), _NOW)


def test_near_delivery_is_not_indent():
    assert not _is_indent_commitment(datetime(2026, 7, 10), _NOW)


def test_threshold_boundary():
    inside = datetime(2026, 7, 30)      # 27 days out — inside 28-day threshold
    outside = datetime(2026, 8, 15)     # 43 days out
    assert not _is_indent_commitment(inside, _NOW)
    assert _is_indent_commitment(outside, _NOW)
    assert INDENT_THRESHOLD_DAYS == 28


def test_missing_date_is_not_indent():
    assert not _is_indent_commitment(False, _NOW)
    assert not _is_indent_commitment(None, _NOW)


def test_custom_threshold_respected():
    assert _is_indent_commitment(datetime(2026, 7, 10), _NOW, threshold_days=3)
    assert not _is_indent_commitment(datetime(2026, 7, 10), _NOW, threshold_days=60)


def test_gate_exemption_wired_into_line_processor():
    """The OOS policy must be forced to accept-in-full for indent orders —
    pins the exemption so a refactor cannot silently drop it."""
    assert re.search(
        r"if getattr\(so, 'x_is_indent', False\):\s*\n\s*policy = 'backorder'",
        _SRC), "indent exemption missing from _process_order_line"


def test_reclamp_skips_indents():
    assert "so.state == 'draft' and not getattr(so, 'x_is_indent', False)" in _SRC
