# mml.edi/tests/test_localdir_handler.py
"""
LocalDirHandler unit tests — pure filesystem I/O against tmp_path, no FTP
server and no Odoo required.
Run with: python -m pytest tests/test_localdir_handler.py -v
"""
import os
from types import SimpleNamespace

import pytest

from mml_edi.models.edi_ftp import LocalDirHandler
from mml_edi.parsers.base_parser import EDIFTPError


def make_fake_partner(tmp_path, inbox_name="inbox", outbox_name="outbox",
                       create_dirs=True):
    """Create a SimpleNamespace fake partner backed by tmp_path directories."""
    inbox = tmp_path / inbox_name
    outbox = tmp_path / outbox_name
    if create_dirs:
        inbox.mkdir(parents=True, exist_ok=True)
        outbox.mkdir(parents=True, exist_ok=True)

    partner = SimpleNamespace(
        code="TEST",
        ftp_protocol="localdir",
        _inbox=str(inbox),
        _outbox=str(outbox),
    )
    partner.get_active_inbox_path = lambda: partner._inbox
    partner.get_active_outbox_path = lambda: partner._outbox
    # EDIFTPHandler-parity: __init__ calls trading_partner.sudo()
    partner.sudo = lambda: partner
    return partner


class TestConnection:
    """connection() context manager — fail-closed directory validation."""

    def test_connection_succeeds_when_dirs_exist(self, tmp_path):
        partner = make_fake_partner(tmp_path)
        handler = LocalDirHandler(partner)
        with handler.connection():
            pass  # must not raise

    def test_connect_raises_when_inbox_missing(self, tmp_path):
        partner = make_fake_partner(tmp_path, create_dirs=False)
        (tmp_path / "outbox").mkdir()
        handler = LocalDirHandler(partner)
        with pytest.raises(EDIFTPError, match="inbox"):
            handler.connect()

    def test_connect_raises_when_outbox_missing(self, tmp_path):
        partner = make_fake_partner(tmp_path, create_dirs=False)
        (tmp_path / "inbox").mkdir()
        handler = LocalDirHandler(partner)
        with pytest.raises(EDIFTPError, match="outbox"):
            handler.connect()

    def test_connect_raises_when_inbox_path_is_a_file(self, tmp_path):
        """A non-directory at the inbox path must fail closed, not silently pass."""
        partner = make_fake_partner(tmp_path, create_dirs=False)
        bad_inbox = tmp_path / "inbox"
        bad_inbox.write_bytes(b"not a directory")
        (tmp_path / "outbox").mkdir()
        handler = LocalDirHandler(partner)
        with pytest.raises(EDIFTPError):
            handler.connect()

    def test_connection_context_manager_disconnect_is_noop_safe(self, tmp_path):
        """disconnect() must be safe to call and not raise even though there is
        no real connection to tear down."""
        partner = make_fake_partner(tmp_path)
        handler = LocalDirHandler(partner)
        handler.disconnect()  # before connect — must not raise
        with handler.connection():
            pass
        handler.disconnect()  # after connect — must not raise

    def test_connection_still_disconnects_on_exception(self, tmp_path):
        partner = make_fake_partner(tmp_path)
        handler = LocalDirHandler(partner)
        with pytest.raises(ValueError):
            with handler.connection():
                raise ValueError("boom")


class TestListFiles:
    """list_files() — unprocessed-file filtering in the inbox."""

    def test_list_files_returns_plain_files(self, tmp_path):
        partner = make_fake_partner(tmp_path)
        (tmp_path / "inbox" / "order1.edi").write_bytes(b"a")
        (tmp_path / "inbox" / "order2.edi").write_bytes(b"b")
        handler = LocalDirHandler(partner)
        files = sorted(handler.list_files())
        assert files == ["order1.edi", "order2.edi"]

    def test_list_files_excludes_processed_marker(self, tmp_path):
        partner = make_fake_partner(tmp_path)
        (tmp_path / "inbox" / "order1.edi").write_bytes(b"a")
        (tmp_path / "inbox" / "order2.edi.processed.20260101000000").write_bytes(b"b")
        handler = LocalDirHandler(partner)
        files = handler.list_files()
        assert files == ["order1.edi"]

    def test_list_files_excludes_dot_files(self, tmp_path):
        partner = make_fake_partner(tmp_path)
        (tmp_path / "inbox" / "order1.edi").write_bytes(b"a")
        (tmp_path / "inbox" / ".hidden").write_bytes(b"b")
        handler = LocalDirHandler(partner)
        files = handler.list_files()
        assert files == ["order1.edi"]

    def test_list_files_empty_inbox_returns_empty_list(self, tmp_path):
        partner = make_fake_partner(tmp_path)
        handler = LocalDirHandler(partner)
        assert handler.list_files() == []

    def test_list_files_raises_edi_ftp_error_on_missing_dir(self, tmp_path):
        partner = make_fake_partner(tmp_path, create_dirs=False)
        handler = LocalDirHandler(partner)
        with pytest.raises(EDIFTPError):
            handler.list_files()


class TestListOutboxFiles:
    def test_list_outbox_files_no_filtering(self, tmp_path):
        partner = make_fake_partner(tmp_path)
        (tmp_path / "outbox" / "ack1.edi").write_bytes(b"a")
        (tmp_path / "outbox" / ".hidden").write_bytes(b"b")
        (tmp_path / "outbox" / "ack2.edi.processed.20260101000000").write_bytes(b"c")
        handler = LocalDirHandler(partner)
        files = sorted(handler.list_outbox_files())
        # No filtering — everything present, unlike list_files()
        assert files == [".hidden", "ack1.edi", "ack2.edi.processed.20260101000000"]


class TestDownloadUpload:
    """Byte fidelity + atomic upload."""

    def test_download_file_roundtrip_ascii(self, tmp_path):
        partner = make_fake_partner(tmp_path)
        (tmp_path / "inbox" / "order1.edi").write_bytes(b"hello world")
        handler = LocalDirHandler(partner)
        assert handler.download_file("order1.edi") == b"hello world"

    def test_download_file_roundtrip_latin1_bytes(self, tmp_path):
        """Non-UTF8 bytes (e.g. EDIFACT Latin-1 payloads) must survive intact."""
        partner = make_fake_partner(tmp_path)
        payload = "Caf\xe9 über \xf1".encode("latin-1")
        (tmp_path / "inbox" / "order1.edi").write_bytes(payload)
        handler = LocalDirHandler(partner)
        assert handler.download_file("order1.edi") == payload

    def test_download_file_missing_raises_edi_ftp_error(self, tmp_path):
        partner = make_fake_partner(tmp_path)
        handler = LocalDirHandler(partner)
        with pytest.raises(EDIFTPError):
            handler.download_file("nope.edi")

    def test_upload_file_writes_content(self, tmp_path):
        partner = make_fake_partner(tmp_path)
        handler = LocalDirHandler(partner)
        handler.upload_file("ack1.edi", b"ack content")
        written = (tmp_path / "outbox" / "ack1.edi").read_bytes()
        assert written == b"ack content"

    def test_upload_file_roundtrip_latin1_bytes(self, tmp_path):
        partner = make_fake_partner(tmp_path)
        handler = LocalDirHandler(partner)
        payload = "R\xe9ponse \xe0 la commande".encode("latin-1")
        handler.upload_file("ack1.edi", payload)
        assert (tmp_path / "outbox" / "ack1.edi").read_bytes() == payload

    def test_upload_file_atomic_no_partial_name_visible(self, tmp_path):
        """After upload, exactly one file with the final name exists in the
        outbox and no temp artifact is left behind."""
        partner = make_fake_partner(tmp_path)
        handler = LocalDirHandler(partner)
        handler.upload_file("ack1.edi", b"payload")
        entries = os.listdir(str(tmp_path / "outbox"))
        assert entries == ["ack1.edi"]

    def test_upload_file_overwrite_is_still_atomic_single_file(self, tmp_path):
        partner = make_fake_partner(tmp_path)
        handler = LocalDirHandler(partner)
        handler.upload_file("ack1.edi", b"first")
        handler.upload_file("ack1.edi", b"second")
        entries = os.listdir(str(tmp_path / "outbox"))
        assert entries == ["ack1.edi"]
        assert (tmp_path / "outbox" / "ack1.edi").read_bytes() == b"second"

    def test_upload_file_cleans_up_temp_on_failure(self, tmp_path, monkeypatch):
        """If the atomic replace step fails, no temp file should linger."""
        partner = make_fake_partner(tmp_path)
        handler = LocalDirHandler(partner)

        def _boom(*args, **kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(EDIFTPError):
            handler.upload_file("ack1.edi", b"payload")
        entries = os.listdir(str(tmp_path / "outbox"))
        assert entries == []


class TestMoveToProcessed:
    def test_move_to_processed_renames_with_marker(self, tmp_path):
        partner = make_fake_partner(tmp_path)
        (tmp_path / "inbox" / "order1.edi").write_bytes(b"a")
        handler = LocalDirHandler(partner)
        handler.move_to_processed("order1.edi")
        entries = os.listdir(str(tmp_path / "inbox"))
        assert len(entries) == 1
        assert entries[0].startswith("order1.edi.processed.")
        assert not (tmp_path / "inbox" / "order1.edi").exists()

    def test_move_to_processed_missing_source_raises_edi_ftp_error(self, tmp_path):
        """Mirrors EDIFTPHandler: a missing source must surface as EDIFTPError,
        not a bare OSError/FileNotFoundError."""
        partner = make_fake_partner(tmp_path)
        handler = LocalDirHandler(partner)
        with pytest.raises(EDIFTPError):
            handler.move_to_processed("nope.edi")

    def test_move_to_processed_result_excluded_from_list_files(self, tmp_path):
        partner = make_fake_partner(tmp_path)
        (tmp_path / "inbox" / "order1.edi").write_bytes(b"a")
        handler = LocalDirHandler(partner)
        handler.move_to_processed("order1.edi")
        assert handler.list_files() == []


class TestPathTraversalRejection:
    """_safe_filename must be applied to every filename argument."""

    @pytest.mark.parametrize("bad_name", [
        "../evil",
        "../../etc/passwd",
        "a/b",
        "a\\b",
        "/etc/passwd",
        ".hidden",
        "",
    ])
    def test_download_file_rejects_traversal(self, tmp_path, bad_name):
        partner = make_fake_partner(tmp_path)
        handler = LocalDirHandler(partner)
        with pytest.raises(EDIFTPError):
            handler.download_file(bad_name)

    @pytest.mark.parametrize("bad_name", [
        "../evil",
        "a/b",
        "a\\b",
        "/etc/passwd",
    ])
    def test_upload_file_rejects_traversal(self, tmp_path, bad_name):
        partner = make_fake_partner(tmp_path)
        handler = LocalDirHandler(partner)
        with pytest.raises(EDIFTPError):
            handler.upload_file(bad_name, b"payload")
        # Nothing must have been written to the outbox
        assert os.listdir(str(tmp_path / "outbox")) == []

    @pytest.mark.parametrize("bad_name", [
        "../evil",
        "a/b",
        "a\\b",
        "/etc/passwd",
    ])
    def test_move_to_processed_rejects_traversal(self, tmp_path, bad_name):
        partner = make_fake_partner(tmp_path)
        handler = LocalDirHandler(partner)
        with pytest.raises(EDIFTPError):
            handler.move_to_processed(bad_name)

    def test_absolute_path_windows_style_rejected(self, tmp_path):
        partner = make_fake_partner(tmp_path)
        handler = LocalDirHandler(partner)
        with pytest.raises(EDIFTPError):
            handler.download_file("C:\\Windows\\System32\\config")
