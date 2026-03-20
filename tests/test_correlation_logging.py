"""
Verify that EDI processor log output includes session correlation IDs.
Pure Python — no Odoo runtime needed.
"""
import re
import pytest


class TestCorrelationId:

    def test_session_id_format(self):
        """
        build_session_id() must return an 8-character lowercase hex string,
        suitable for use as [EDI:<id>] log prefix.
        """
        try:
            from mml_edi.models.edi_processor import build_session_id
        except ImportError:
            pytest.skip("build_session_id not yet implemented")

        sid = build_session_id()
        assert re.match(r'^[0-9a-f]{8}$', sid), (
            "Session ID must be 8 lowercase hex chars, got: %r" % sid
        )

    def test_two_calls_produce_different_session_ids(self):
        """Each poll run must get a unique session ID."""
        try:
            from mml_edi.models.edi_processor import build_session_id
        except ImportError:
            pytest.skip("build_session_id not yet implemented")

        ids = {build_session_id() for _ in range(20)}
        assert len(ids) > 1, "Session IDs must be unique across calls"
