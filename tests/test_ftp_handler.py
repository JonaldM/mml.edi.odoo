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
    # EDIFTPHandler.__init__ calls trading_partner.sudo(); a real recordset
    # returns an equivalent recordset, so make the mock return itself.
    partner.sudo.return_value = partner
    return partner


class TestSafeFilename:
    """Tests for the _safe_filename() path traversal guard."""

    def test_safe_filename_rejects_dot_dot_slash_variant(self):
        """....// must be rejected — not caught by the old blacklist."""
        from mml_edi.models.edi_ftp import _safe_filename
        from mml_edi.parsers.base_parser import EDIFTPError
        with pytest.raises(EDIFTPError):
            _safe_filename('....//etc/passwd')

    def test_safe_filename_rejects_leading_dot(self):
        """.hidden must be rejected — leading dot blocked by whitelist."""
        from mml_edi.models.edi_ftp import _safe_filename
        from mml_edi.parsers.base_parser import EDIFTPError
        with pytest.raises(EDIFTPError):
            _safe_filename('.hidden_file')

    def test_safe_filename_rejects_forward_slash(self):
        """Filenames containing / must be rejected."""
        from mml_edi.models.edi_ftp import _safe_filename
        from mml_edi.parsers.base_parser import EDIFTPError
        with pytest.raises(EDIFTPError):
            _safe_filename('subdir/malicious.edi')

    def test_safe_filename_rejects_backslash(self):
        r"""Filenames containing \ must be rejected."""
        from mml_edi.models.edi_ftp import _safe_filename
        from mml_edi.parsers.base_parser import EDIFTPError
        with pytest.raises(EDIFTPError):
            _safe_filename('subdir\\malicious.edi')

    def test_safe_filename_rejects_dot_dot(self):
        """../traversal must be rejected."""
        from mml_edi.models.edi_ftp import _safe_filename
        from mml_edi.parsers.base_parser import EDIFTPError
        with pytest.raises(EDIFTPError):
            _safe_filename('../etc/passwd')

    def test_safe_filename_rejects_empty_string(self):
        """Empty filename must be rejected."""
        from mml_edi.models.edi_ftp import _safe_filename
        from mml_edi.parsers.base_parser import EDIFTPError
        with pytest.raises(EDIFTPError):
            _safe_filename('')

    def test_safe_filename_accepts_normal_edi_filename(self):
        """Standard EDI filenames must pass through unchanged."""
        from mml_edi.models.edi_ftp import _safe_filename
        result = _safe_filename('BRISCOES_PO_20260308_001.edi')
        assert result == 'BRISCOES_PO_20260308_001.edi'

    def test_safe_filename_accepts_alphanumeric_with_dashes(self):
        """Hyphenated filenames must be accepted."""
        from mml_edi.models.edi_ftp import _safe_filename
        result = _safe_filename('order-2026-03-08.edi')
        assert result == 'order-2026-03-08.edi'

    def test_safe_filename_accepts_uppercase_filename(self):
        """All-uppercase EDI filenames must be accepted."""
        from mml_edi.models.edi_ftp import _safe_filename
        result = _safe_filename('ORDERS001.EDI')
        assert result == 'ORDERS001.EDI'


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


class TestEDIFTPHandlerSFTP:
    """Tests for SFTP-specific behaviour in EDIFTPHandler.

    paramiko may not be installed in all test environments, so tests that need
    to reach the host-key check (which comes after the paramiko import) mock
    the import using sys.modules.
    """

    def _make_mock_paramiko(self):
        """Return a MagicMock that stands in for the paramiko module."""
        mock_paramiko = MagicMock()
        mock_paramiko.SSHException = Exception
        return mock_paramiko

    def test_sftp_rejects_connection_when_no_host_key(self):
        """_connect_sftp() must raise EDIFTPError when sftp_host_key is blank."""
        import sys
        from mml_edi.models.edi_ftp import EDIFTPHandler
        from mml_edi.parsers.base_parser import EDIFTPError

        partner = make_mock_partner(protocol="sftp")
        partner.sftp_host_key = ''  # no host key configured

        mock_paramiko = self._make_mock_paramiko()
        with patch.dict(sys.modules, {'paramiko': mock_paramiko}):
            handler = EDIFTPHandler(partner)
            with pytest.raises(EDIFTPError, match="SFTP host key not configured"):
                handler._connect_sftp()

    def test_sftp_rejects_connection_when_host_key_is_none(self):
        """_connect_sftp() must raise EDIFTPError when sftp_host_key is None."""
        import sys
        from mml_edi.models.edi_ftp import EDIFTPHandler
        from mml_edi.parsers.base_parser import EDIFTPError

        partner = make_mock_partner(protocol="sftp")
        partner.sftp_host_key = None

        mock_paramiko = self._make_mock_paramiko()
        with patch.dict(sys.modules, {'paramiko': mock_paramiko}):
            handler = EDIFTPHandler(partner)
            with pytest.raises(EDIFTPError, match="SFTP host key not configured"):
                handler._connect_sftp()

    def test_sftp_raises_when_paramiko_missing(self):
        """_connect_sftp() must raise EDIFTPError when paramiko is not installed."""
        import sys
        from mml_edi.models.edi_ftp import EDIFTPHandler
        from mml_edi.parsers.base_parser import EDIFTPError

        partner = make_mock_partner(protocol="sftp")
        partner.sftp_host_key = 'somekey'

        # Remove paramiko from sys.modules to simulate it not being installed
        with patch.dict(sys.modules, {'paramiko': None}):
            handler = EDIFTPHandler(partner)
            with pytest.raises(EDIFTPError, match="paramiko is required"):
                handler._connect_sftp()

    def test_sftp_rejects_invalid_host_key_format(self):
        """_connect_sftp() must raise EDIFTPError when sftp_host_key is not valid base64 RSA key."""
        import sys
        from mml_edi.models.edi_ftp import EDIFTPHandler
        from mml_edi.parsers.base_parser import EDIFTPError

        partner = make_mock_partner(protocol="sftp")
        # Valid base64 but not a valid RSA key — RSAKey(data=...) will raise
        partner.sftp_host_key = 'bm90YXZhbGlka2V5'  # base64 of 'notavalidkey'

        mock_paramiko = self._make_mock_paramiko()
        mock_paramiko.RSAKey.side_effect = Exception("Not a valid RSA key")
        with patch.dict(sys.modules, {'paramiko': mock_paramiko}):
            handler = EDIFTPHandler(partner)
            with pytest.raises(EDIFTPError, match="Invalid sftp_host_key format"):
                handler._connect_sftp()

    def test_sftp_host_key_registered_under_bracketed_name_on_custom_port(self):
        """Non-default port must pin the key as "[host]:port".

        Paramiko's SSHClient.connect looks the host key up under the bare
        hostname ONLY on port 22; on any other port it uses "[host]:port".
        Registering under the bare name on a custom port silently misses and
        RejectPolicy aborts the connection. Regression: SPS Commerce serves
        SFTP on 10022, which failed with "not found in known_hosts".
        """
        import sys
        from mml_edi.models.edi_ftp import EDIFTPHandler

        partner = make_mock_partner(protocol="sftp", host="sftp.example.com",
                                    port=10022)
        partner.sftp_host_key = 'bm90YXZhbGlka2V5'

        mock_paramiko = self._make_mock_paramiko()
        with patch.dict(sys.modules, {'paramiko': mock_paramiko}):
            handler = EDIFTPHandler(partner)
            handler._connect_sftp()

        client = mock_paramiko.SSHClient.return_value
        registered_name = client.get_host_keys.return_value.add.call_args[0][0]
        assert registered_name == '[sftp.example.com]:10022'

    def test_sftp_host_key_registered_under_bare_host_on_default_port(self):
        """Port 22 must keep the bare hostname — matches paramiko's lookup."""
        import sys
        from mml_edi.models.edi_ftp import EDIFTPHandler

        partner = make_mock_partner(protocol="sftp", host="sftp.example.com",
                                    port=22)
        partner.sftp_host_key = 'bm90YXZhbGlka2V5'

        mock_paramiko = self._make_mock_paramiko()
        with patch.dict(sys.modules, {'paramiko': mock_paramiko}):
            handler = EDIFTPHandler(partner)
            handler._connect_sftp()

        client = mock_paramiko.SSHClient.return_value
        registered_name = client.get_host_keys.return_value.add.call_args[0][0]
        assert registered_name == 'sftp.example.com'
