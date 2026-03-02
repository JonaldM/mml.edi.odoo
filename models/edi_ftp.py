# mml.edi/models/edi_ftp.py
"""
FTP/SFTP connection handler for EDI trading partners.

Supports plain FTP (ftplib) and SFTP (paramiko).
All file operations use paths from trading_partner.get_active_inbox_path()
and trading_partner.get_active_outbox_path().
"""

import ftplib
import io
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from ..parsers.base_parser import EDIFTPError

_logger = logging.getLogger(__name__)

_RETRY_ATTEMPTS = 4   # 1 initial + 3 retries
_RETRY_DELAYS = [2, 4, 8]  # seconds between retries
_CONNECT_TIMEOUT = 30  # seconds
_TRANSFER_TIMEOUT = 60  # seconds


class EDIFTPHandler:
    """
    Manages FTP/SFTP connections for a single trading partner.

    Usage:
        handler = EDIFTPHandler(trading_partner)
        with handler.connection():
            files = handler.list_files()
            content = handler.download_file(files[0])
            handler.move_to_processed(files[0])
    """

    def __init__(self, trading_partner):
        self.partner = trading_partner
        self._ftp = None  # ftplib.FTP or paramiko.SFTPClient

    # ── Connection lifecycle ──────────────────────────────────────────────

    def connect(self) -> None:
        """
        Establish connection. Retries up to 3 times with exponential backoff.
        Raises EDIFTPError on final failure.
        """
        last_exc = None
        for attempt in range(_RETRY_ATTEMPTS):
            if attempt > 0:
                time.sleep(_RETRY_DELAYS[attempt - 1])
            try:
                if self.partner.ftp_protocol == "sftp":
                    self._connect_sftp()
                else:
                    self._connect_ftp()
                _logger.info(
                    "[EDI FTP] Connected to %s (%s)",
                    self.partner.ftp_host, self.partner.code,
                )
                return
            except Exception as exc:
                last_exc = exc
                _logger.warning(
                    "[EDI FTP] Connection attempt %d/%d failed for %s: %s",
                    attempt + 1, _RETRY_ATTEMPTS, self.partner.code, exc,
                )

        raise EDIFTPError(
            "Failed to connect to %s after %d attempts: %s"
            % (self.partner.ftp_host, _RETRY_ATTEMPTS, last_exc)
        )

    def disconnect(self) -> None:
        """Clean disconnect. Safe to call even if not connected."""
        if self._ftp is None:
            return
        try:
            if self.partner.ftp_protocol == "sftp":
                self._ftp.close()
            else:
                self._ftp.quit()
        except Exception:
            pass  # Best-effort disconnect
        finally:
            self._ftp = None

    @contextmanager
    def connection(self):
        """Context manager — auto connect on enter, disconnect on exit (even on exception)."""
        self.connect()
        try:
            yield self
        finally:
            self.disconnect()

    # ── File operations ───────────────────────────────────────────────────

    def list_files(self) -> list:
        """List files in the active inbox directory. Returns filenames only."""
        inbox = self.partner.get_active_inbox_path()
        try:
            if self.partner.ftp_protocol == "sftp":
                return [f.filename for f in self._ftp.listdir_attr(inbox)
                        if not f.filename.startswith(".")]
            else:
                return self._ftp.nlst(inbox)
        except Exception as exc:
            raise EDIFTPError("list_files failed on %s: %s" % (inbox, exc)) from exc

    def download_file(self, filename: str) -> bytes:
        """Download a single file by name from the active inbox. Returns raw bytes."""
        inbox = self.partner.get_active_inbox_path()
        filepath = "%s/%s" % (inbox, filename)
        buf = io.BytesIO()
        try:
            if self.partner.ftp_protocol == "sftp":
                self._ftp.getfo(filepath, buf)
            else:
                self._ftp.retrbinary("RETR %s" % filepath, buf.write)
            return buf.getvalue()
        except Exception as exc:
            raise EDIFTPError("download_file failed for %s: %s" % (filepath, exc)) from exc

    def upload_file(self, filename: str, content: bytes) -> None:
        """Upload a file by name to the active outbox directory."""
        outbox = self.partner.get_active_outbox_path()
        filepath = "%s/%s" % (outbox, filename)
        buf = io.BytesIO(content)
        try:
            if self.partner.ftp_protocol == "sftp":
                self._ftp.putfo(buf, filepath)
            else:
                self._ftp.storbinary("STOR %s" % filepath, buf)
        except Exception as exc:
            raise EDIFTPError("upload_file failed for %s: %s" % (filepath, exc)) from exc

    def move_to_processed(self, filename: str) -> None:
        """
        Rename a processed file in the inbox to prevent re-processing.
        New name format: {filename}.processed.{YYYYMMDDHHMMSS}
        """
        inbox = self.partner.get_active_inbox_path()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        old_path = "%s/%s" % (inbox, filename)
        new_path = "%s/%s.processed.%s" % (inbox, filename, timestamp)
        try:
            self._ftp.rename(old_path, new_path)
        except Exception as exc:
            raise EDIFTPError(
                "move_to_processed failed for %s: %s" % (old_path, exc)
            ) from exc

    # ── Internal connection methods ───────────────────────────────────────

    def _connect_ftp(self):
        ftp = ftplib.FTP(timeout=_CONNECT_TIMEOUT)
        ftp.connect(self.partner.ftp_host, self.partner.ftp_port)
        ftp.login(self.partner.ftp_user, self.partner.ftp_password)
        ftp.set_pasv(True)
        self._ftp = ftp

    def _connect_sftp(self):
        try:
            import paramiko
        except ImportError:
            raise EDIFTPError(
                "paramiko is required for SFTP. Install with: pip install paramiko"
            )
        transport = paramiko.Transport(
            (self.partner.ftp_host, self.partner.ftp_port)
        )
        transport.connect(
            username=self.partner.ftp_user,
            password=self.partner.ftp_password,
        )
        self._ftp = paramiko.SFTPClient.from_transport(transport)
