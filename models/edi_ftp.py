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
import os
import re as _re
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from ..parsers.base_parser import EDIFTPError

_logger = logging.getLogger(__name__)

_RETRY_ATTEMPTS = 4   # 1 initial + 3 retries
_RETRY_DELAYS = [2, 4, 8]  # seconds between retries
_CONNECT_TIMEOUT = 30  # seconds
_TRANSFER_TIMEOUT = 60  # seconds


_SAFE_FILENAME_RE = _re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.\-]*$')


def _safe_filename(filename: str) -> str:
    """Validate and return a safe filename using a whitelist pattern.

    Requires the filename to start with an alphanumeric character and contain
    only letters, digits, underscores, hyphens, and dots. This prevents path
    traversal via leading dots (.hidden), embedded slashes, backslashes, or
    double-dot sequences regardless of encoding tricks.

    Raises EDIFTPError if the filename is invalid or empty.
    """
    if not filename or not isinstance(filename, str):
        raise EDIFTPError(f'Invalid filename: {filename!r}')
    if not _SAFE_FILENAME_RE.match(filename):
        raise EDIFTPError(
            f'Filename rejected — must start with alphanumeric and contain '
            f'only letters, digits, underscores, hyphens, dots: {filename!r}'
        )
    return filename


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
        # sudo() so the elevated credential fields (ftp_user, ftp_password,
        # sftp_host_key — all groups='base.group_system') are readable even when
        # the handler runs as a non-admin user. Without this, _queue_ack(),
        # triggered by a clicking EDI user, hits AccessError on the credential
        # read and the ORDRSP is silently never sent. The view-level group
        # restriction on those fields is unchanged.
        self.partner = trading_partner.sudo()
        self._ftp = None  # ftplib.FTP or paramiko.SFTPClient
        self._transport = None  # paramiko.Transport (SFTP only)

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
            if hasattr(self, '_transport') and self._transport:
                self._transport.close()
                self._transport = None

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
        """List unprocessed files in the active inbox. Returns filenames only.

        Files already archived by move_to_processed carry a '.processed.<ts>'
        marker in their name; they are excluded so they are not re-downloaded
        and re-renamed on every poll (move_to_processed renames in place, so an
        un-filtered listing would re-surface them indefinitely).
        """
        inbox = self.partner.get_active_inbox_path()

        def _keep(name):
            return bool(name) and not name.startswith('.') and '.processed.' not in name

        try:
            if self.partner.ftp_protocol == "sftp":
                return [f.filename for f in self._ftp.listdir_attr(inbox)
                        if _keep(f.filename)]
            else:
                return [
                    os.path.basename(f) for f in self._ftp.nlst(inbox)
                    if _keep(os.path.basename(f))
                ]
        except Exception as exc:
            raise EDIFTPError("list_files failed on %s: %s" % (inbox, exc)) from exc

    def list_outbox_files(self) -> list:
        """List files currently in the active OUTBOX directory (names only).

        Used by the ACK send path to VERIFY whether a claimed upload actually
        landed after an FTP ghost-success (data stored but the final 226 lost)
        — re-uploading a delivered ACK would double-respond to the partner.
        No filtering: the caller matches an exact filename.
        """
        outbox = self.partner.get_active_outbox_path()
        try:
            if self.partner.ftp_protocol == "sftp":
                return [f.filename for f in self._ftp.listdir_attr(outbox)]
            return [os.path.basename(f) for f in self._ftp.nlst(outbox)]
        except Exception as exc:
            raise EDIFTPError(
                "list_outbox_files failed on %s: %s" % (outbox, exc)) from exc

    def download_file(self, filename: str) -> bytes:
        """Download a single file by name from the active inbox. Returns raw bytes."""
        filename = _safe_filename(filename)
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
        filename = _safe_filename(filename)
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
        filename = _safe_filename(filename)
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
        ftp.login(self.partner.ftp_user, self.partner.get_ftp_password())
        ftp.set_pasv(True)
        self._ftp = ftp

    def _connect_sftp(self):
        try:
            import base64
            import paramiko
        except ImportError:
            raise EDIFTPError(
                "paramiko is required for SFTP. Install with: pip install paramiko"
            )

        stored_key_b64 = self.partner.sftp_host_key
        if not stored_key_b64:
            raise EDIFTPError(
                "SFTP host key not configured for partner '%s'. "
                "Set sftp_host_key on the trading partner before enabling SFTP." % self.partner.code
            )

        try:
            key_bytes = base64.b64decode(stored_key_b64)
            server_key = paramiko.RSAKey(data=key_bytes)
        except Exception as exc:
            raise EDIFTPError(
                "Invalid sftp_host_key format for partner '%s': %s" % (self.partner.code, exc)
            )

        client = paramiko.SSHClient()
        client.get_host_keys().add(
            self.partner.ftp_host, 'ssh-rsa', server_key
        )
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

        try:
            client.connect(
                hostname=self.partner.ftp_host,
                port=self.partner.ftp_port,
                username=self.partner.ftp_user,
                password=self.partner.get_ftp_password(),
                timeout=_CONNECT_TIMEOUT,
                look_for_keys=False,
                allow_agent=False,
            )
        except paramiko.SSHException as exc:
            raise EDIFTPError(
                "SFTP connection failed for '%s': %s" % (self.partner.code, exc)
            )

        self._transport = client   # assign BEFORE open_sftp so disconnect() can clean up on failure
        self._ftp = client.open_sftp()


class LocalDirHandler:
    """
    Manages a local-directory "transport" for a single trading partner —
    a drop-in peer of EDIFTPHandler with the same public interface, backed
    by plain filesystem I/O instead of FTP/SFTP.

    Purpose: (a) a bridge point for future AS2/other gateways that deliver
    files into a local directory rather than speaking FTP themselves, and
    (b) drill/test rigs that need a partner without standing up an FTP
    server.

    Usage:
        handler = LocalDirHandler(trading_partner)
        with handler.connection():
            files = handler.list_files()
            content = handler.download_file(files[0])
            handler.move_to_processed(files[0])

    In this mode get_active_inbox_path()/get_active_outbox_path() return
    absolute local directory paths (not remote paths on an FTP server).
    """

    def __init__(self, trading_partner):
        # sudo() for parity with EDIFTPHandler — see its __init__ for why
        # (elevated credential fields must be readable for non-admin callers).
        self.partner = trading_partner.sudo()

    # ── Connection lifecycle ──────────────────────────────────────────────

    def connect(self) -> None:
        """
        Validate that the inbox and outbox directories exist and are
        directories. There is no real connection to establish for local
        disk, but we fail closed here rather than surfacing a confusing
        error later from list_files()/upload_file().
        """
        inbox = self.partner.get_active_inbox_path()
        outbox = self.partner.get_active_outbox_path()
        for label, path in (("inbox", inbox), ("outbox", outbox)):
            if not path or not os.path.isdir(path):
                raise EDIFTPError(
                    "LocalDirHandler connect failed — %s path does not exist "
                    "or is not a directory: %r" % (label, path)
                )

    def disconnect(self) -> None:
        """No-op — no persistent connection is held for local disk."""
        pass

    @contextmanager
    def connection(self):
        """Context manager — validates dirs on enter, no-op teardown on exit."""
        self.connect()
        try:
            yield self
        finally:
            self.disconnect()

    # ── File operations ───────────────────────────────────────────────────

    def list_files(self) -> list:
        """List unprocessed files in the active inbox. Returns filenames only.

        Mirrors EDIFTPHandler.list_files: files already archived by
        move_to_processed carry a '.processed.<ts>' marker in their name and
        dot-files are excluded, so they are not re-surfaced on every poll.
        """
        inbox = self.partner.get_active_inbox_path()

        def _keep(name):
            return bool(name) and not name.startswith('.') and '.processed.' not in name

        try:
            return [name for name in os.listdir(inbox) if _keep(name)]
        except Exception as exc:
            raise EDIFTPError("list_files failed on %s: %s" % (inbox, exc)) from exc

    def list_outbox_files(self) -> list:
        """List files currently in the active OUTBOX directory (names only).

        No filtering — mirrors EDIFTPHandler.list_outbox_files, used by the
        ACK send path to verify a claimed upload actually landed.
        """
        outbox = self.partner.get_active_outbox_path()
        try:
            return os.listdir(outbox)
        except Exception as exc:
            raise EDIFTPError(
                "list_outbox_files failed on %s: %s" % (outbox, exc)) from exc

    def download_file(self, filename: str) -> bytes:
        """Download (read) a single file by name from the active inbox. Returns raw bytes."""
        filename = _safe_filename(filename)
        inbox = self.partner.get_active_inbox_path()
        filepath = os.path.join(inbox, filename)
        try:
            with open(filepath, 'rb') as fh:
                return fh.read()
        except Exception as exc:
            raise EDIFTPError("download_file failed for %s: %s" % (filepath, exc)) from exc

    def upload_file(self, filename: str, content: bytes) -> None:
        """Upload (write) a file by name to the active outbox directory.

        Written atomically: content lands in a temp file in the SAME
        directory first, then os.replace() swaps it into place — a reader
        can never observe a partially-written file under the final name.
        """
        filename = _safe_filename(filename)
        outbox = self.partner.get_active_outbox_path()
        filepath = os.path.join(outbox, filename)
        tmp_filepath = os.path.join(outbox, ".%s.tmp.%s" % (filename, os.getpid()))
        try:
            with open(tmp_filepath, 'wb') as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_filepath, filepath)
        except Exception as exc:
            try:
                os.remove(tmp_filepath)
            except OSError:
                pass
            raise EDIFTPError("upload_file failed for %s: %s" % (filepath, exc)) from exc

    def move_to_processed(self, filename: str) -> None:
        """
        Rename a processed file in the inbox to prevent re-processing.
        New name format: {filename}.processed.{YYYYMMDDHHMMSS}

        Mirrors EDIFTPHandler.move_to_processed, including raising
        EDIFTPError (not a bare OSError) when the source file is missing.
        """
        filename = _safe_filename(filename)
        inbox = self.partner.get_active_inbox_path()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        old_path = os.path.join(inbox, filename)
        new_path = os.path.join(inbox, "%s.processed.%s" % (filename, timestamp))
        try:
            os.rename(old_path, new_path)
        except Exception as exc:
            raise EDIFTPError(
                "move_to_processed failed for %s: %s" % (old_path, exc)
            ) from exc


def get_transport_handler(partner):
    """Transport factory — the ONE place partner.ftp_protocol picks a handler.

    'localdir' -> LocalDirHandler (plain directories on this server: drill
    rigs, or bridging gateways such as a future AS2 sidecar that deliver
    files to disk). Anything else -> EDIFTPHandler (ftp/sftp). Both classes
    implement the same six-method transport interface, so callers never
    branch on protocol themselves.
    """
    if getattr(partner, "ftp_protocol", None) == "localdir":
        return LocalDirHandler(partner)
    return EDIFTPHandler(partner)
