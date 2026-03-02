# mml.edi/tests/test_ftp_handler.py
"""
FTP handler unit tests — all FTP calls are mocked.
These do not require a live FTP server or Odoo DB.
Run with: python -m pytest tests/test_ftp_handler.py -v
"""
from unittest.mock import MagicMock, patch, call
import pytest
import sys
import os

# Make the mml.edi package importable from its parent directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


def make_mock_partner(protocol="ftp", host="ftp.test.com", port=21,
                      user="user", password="pass",
                      inbox="/inbox", outbox="/outbox",
                      environment="production"):
    """Create a mock trading partner for testing."""
    partner = MagicMock()
    partner.ftp_protocol = protocol
    partner.ftp_host = host
    partner.ftp_port = port
    partner.ftp_user = user
    partner.ftp_password = password
    partner.environment = environment
    partner.code = "TEST"
    # Use methods instead of properties (matching the fixed model)
    partner.get_active_inbox_path.return_value = inbox
    partner.get_active_outbox_path.return_value = outbox
    return partner


class TestEDIFTPHandlerFTP:
    def test_list_files_returns_list(self):
        """list_files() returns filenames from the FTP server."""
        from mml_edi.models.edi_ftp import EDIFTPHandler
        partner = make_mock_partner()
        handler = EDIFTPHandler(partner)
        mock_ftp = MagicMock()
        mock_ftp.nlst.return_value = ["order1.edi", "order2.edi"]
        handler._ftp = mock_ftp
        files = handler.list_files()
        assert files == ["order1.edi", "order2.edi"]

    def test_retry_on_connection_failure(self):
        """connect() retries 3 times total before raising EDIFTPError."""
        from mml_edi.models.edi_ftp import EDIFTPHandler
        from mml_edi.parsers.base_parser import EDIFTPError
        partner = make_mock_partner()
        handler = EDIFTPHandler(partner)
        with patch("mml_edi.models.edi_ftp.ftplib.FTP") as mock_ftp_cls, \
             patch("mml_edi.models.edi_ftp.time.sleep"):
            mock_ftp_cls.side_effect = Exception("Connection refused")
            with pytest.raises(EDIFTPError):
                handler.connect()
            # 1 initial attempt + 3 retry delays = 4 total attempts
            assert mock_ftp_cls.call_count == 4

    def test_move_to_processed_renames_file(self):
        """move_to_processed() renames the file with a .processed timestamp suffix."""
        from mml_edi.models.edi_ftp import EDIFTPHandler
        partner = make_mock_partner()
        handler = EDIFTPHandler(partner)
        mock_ftp = MagicMock()
        handler._ftp = mock_ftp
        handler.move_to_processed("order1.edi")
        mock_ftp.rename.assert_called_once()
        old_path, new_path = mock_ftp.rename.call_args[0]
        assert "order1.edi" in old_path
        assert "processed" in new_path.lower() or ".processed" in new_path

    def test_context_manager_connects_and_disconnects(self):
        """connection() context manager calls connect() and disconnect()."""
        from mml_edi.models.edi_ftp import EDIFTPHandler
        partner = make_mock_partner()
        handler = EDIFTPHandler(partner)
        handler.connect = MagicMock()
        handler.disconnect = MagicMock()
        with handler.connection():
            pass
        handler.connect.assert_called_once()
        handler.disconnect.assert_called_once()

    def test_context_manager_disconnects_on_exception(self):
        """connection() calls disconnect() even if an exception is raised inside."""
        from mml_edi.models.edi_ftp import EDIFTPHandler
        partner = make_mock_partner()
        handler = EDIFTPHandler(partner)
        handler.connect = MagicMock()
        handler.disconnect = MagicMock()
        with pytest.raises(ValueError):
            with handler.connection():
                raise ValueError("test error")
        handler.disconnect.assert_called_once()
