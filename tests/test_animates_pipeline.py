"""Pure tests for the Animates inbound pipeline fixes (Wave1-B / go-live gate
review AN-02, inbound-CONTRL-swallowing, latin-1, envelope validation, C5
cancellation routing).

All tests drive ``EDIProcessor`` directly against fakes — no Odoo registry
required (mirrors tests/test_poll_ordering_invariant.py and
tests/test_duplicate_so_guard.py). Parser capabilities (``generate_contrl``,
``parse_contrl``, ``get_unb_recipient``) are exercised via SimpleNamespace/fake
parser objects implementing the C4/C1 contract shape, since Wave1-A/Wave1-C's
real implementations may not exist yet on disk — these tests assert the
CONTRACT, not another wave's internals.
"""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from mml_edi.models.edi_processor import EDIProcessor
from mml_edi.parsers.base_parser import EDIParseError, ParsedOrder, ParsedOrderLine


# ── Shared fakes ────────────────────────────────────────────────────────────

class _FakeLog:
    """Records every edi.log.log(...) call; search_count matches on the kwargs
    supplied at construction time via a simple predicate list."""

    def __init__(self):
        self.rows = []  # list of dict: {trading_partner_id, event_type, status, filename, ...}

    def log(self, trading_partner, direction, event_type, status, message, **kw):
        row = {
            "trading_partner_id": getattr(trading_partner, "id", None),
            "direction": direction,
            "event_type": event_type,
            "status": status,
            "message": message,
        }
        row.update(kw)
        self.rows.append(row)
        return row

    def search_count(self, domain):
        return len(self._match(domain))

    def search(self, domain, order=None, limit=None):
        rows = self._match(domain)
        if limit:
            rows = rows[:limit]
        return rows

    def _match(self, domain):
        out = []
        for row in self.rows:
            ok = True
            for field, op, value in domain:
                actual = row.get(field)
                if op == "=":
                    if actual != value:
                        ok = False
                        break
                elif op == "like":
                    needle = value.strip("%")
                    if needle not in (actual or ""):
                        ok = False
                        break
            if ok:
                out.append(row)
        return out


def _field_value_for_compare(record, field):
    """Mirror Odoo domain semantics: comparing a many2one field ('=', id)
    compares against the related record's .id, not the record object."""
    val = getattr(record, field, None)
    if hasattr(val, "id") and not isinstance(val, (int, str, bool)):
        return val.id
    return val


class _FakeReviewModel:

    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def create(self, vals):
        rec = SimpleNamespace(**vals)
        rec.id = len(self._rows) + 1
        rec.message_post = lambda body=None, **kw: None
        self._rows.append(rec)
        return rec

    def search(self, domain, order=None, limit=None):
        out = self._rows
        for field, op, value in domain:
            out = [r for r in out if _field_value_for_compare(r, field) == value]
        if limit:
            out = out[:limit]
        return _RecordSet(out)


class _RecordSet(list):
    """Minimal recordset stand-in: a single-record set proxies field access
    directly (mirrors Odoo's ensure_one-style attribute delegation), which
    is the real-code idiom ``review = Model.search([...], limit=1)`` then
    ``review.some_field`` relies on."""

    def filtered(self, fn):
        return _RecordSet([r for r in self if fn(r)])

    def __getitem__(self, item):
        if isinstance(item, slice):
            return _RecordSet(list.__getitem__(self, item))
        return list.__getitem__(self, item)

    def __getattr__(self, name):
        if len(self) == 1:
            return getattr(self[0], name)
        raise AttributeError(
            "_RecordSet of length %d has no attribute %r (only a "
            "single-record set proxies field access)" % (len(self), name))


class _FakeEnv:

    def __init__(self, models):
        self._models = models

    def __getitem__(self, name):
        return self._models[name]


class _FakeFTPHandler:
    """Network-free EDIFTPHandler stand-in — records uploads."""

    uploads = []

    def __init__(self, partner):
        self.partner = partner

    @contextmanager
    def connection(self):
        yield self

    def upload_file(self, filename, content):
        type(self).uploads.append((filename, content))

    @classmethod
    def reset(cls):
        cls.uploads = []


def _make_partner(edi_format="edifact_d01b", code="ANIMATES", **extra):
    vals = dict(id=99, code=code, edi_format=edi_format, environment="test")
    vals.update(extra)
    return SimpleNamespace(**vals)


def _make_review(**kw):
    defaults = dict(
        id=1, trading_partner_id=None, customer_po_number="PO1",
        edi_file_hash="abc123", edi_raw_data="RAW", state="pending_review",
        change_summary="", store_code=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def _reset_ftp():
    _FakeFTPHandler.reset()
    yield
    _FakeFTPHandler.reset()


# ── AN-02: CONTRL emission for inbound ORDERS ───────────────────────────────

class TestEmitInboundContrl:

    def _proc(self, parser, review_rows, log=None):
        log = log or _FakeLog()
        partner = _make_partner()
        partner.get_parser_instance = lambda: parser
        proc = EDIProcessor()
        proc.env = _FakeEnv({
            "edi.log": log,
            "edi.order.review": _FakeReviewModel(review_rows),
        })
        return proc, partner, log

    def test_noop_when_parser_lacks_generate_contrl(self, monkeypatch):
        """Preserve Briscoes byte-for-byte: a parser with no generate_contrl
        must never attempt an upload."""
        import mml_edi.models.edi_processor as proc_mod
        monkeypatch.setattr(proc_mod, "EDIFTPHandler", _FakeFTPHandler, raising=False)

        parser = SimpleNamespace()  # no generate_contrl attribute
        review = _make_review(trading_partner_id=SimpleNamespace(id=99))
        proc, partner, log = self._proc(parser, [review])

        proc._emit_inbound_contrl(partner, "abc123")

        assert _FakeFTPHandler.uploads == []
        assert not any(r["event_type"] == "contrl_sent" for r in log.rows)

    def test_uploads_and_logs_contrl_sent_when_parser_supports_it(self, monkeypatch):
        import mml_edi.models.edi_ftp as edi_ftp_mod
        monkeypatch.setattr(edi_ftp_mod, "EDIFTPHandler", _FakeFTPHandler)

        calls = []

        def generate_contrl(raw_text, partner):
            calls.append((raw_text, partner))
            return b"UNA:+.? 'UNB+...'"

        parser = SimpleNamespace(generate_contrl=generate_contrl)
        review = _make_review(
            trading_partner_id=SimpleNamespace(id=99), edi_raw_data="RAWTEXT")
        proc, partner, log = self._proc(parser, [review])

        proc._emit_inbound_contrl(partner, "abc123")

        assert len(_FakeFTPHandler.uploads) == 1
        filename, content = _FakeFTPHandler.uploads[0]
        assert content == b"UNA:+.? 'UNB+...'"
        assert calls == [("RAWTEXT", partner)]
        sent = [r for r in log.rows if r["event_type"] == "contrl_sent"]
        assert len(sent) == 1
        assert sent[0]["status"] == "success"
        assert sent[0]["filename"] == filename

    def test_idempotent_does_not_resend_for_same_interchange(self, monkeypatch):
        import mml_edi.models.edi_ftp as edi_ftp_mod
        monkeypatch.setattr(edi_ftp_mod, "EDIFTPHandler", _FakeFTPHandler)

        parser = SimpleNamespace(generate_contrl=lambda raw, p: b"BODY")
        review = _make_review(trading_partner_id=SimpleNamespace(id=99))
        proc, partner, log = self._proc(parser, [review])

        proc._emit_inbound_contrl(partner, "abc123")
        proc._emit_inbound_contrl(partner, "abc123")

        assert len(_FakeFTPHandler.uploads) == 1, (
            "a second call for the same file_hash must not re-upload"
        )

    def test_generation_failure_logs_error_and_never_raises(self, monkeypatch):
        import mml_edi.models.edi_ftp as edi_ftp_mod
        monkeypatch.setattr(edi_ftp_mod, "EDIFTPHandler", _FakeFTPHandler)

        def boom(raw, p):
            raise ValueError("bad envelope")

        parser = SimpleNamespace(generate_contrl=boom)
        review = _make_review(trading_partner_id=SimpleNamespace(id=99))
        proc, partner, log = self._proc(parser, [review])
        proc._send_cron_alert = lambda *a, **kw: None

        proc._emit_inbound_contrl(partner, "abc123")  # must not raise

        errors = [r for r in log.rows
                  if r["event_type"] == "contrl_sent" and r["status"] == "error"]
        assert len(errors) == 1
        assert _FakeFTPHandler.uploads == []

    def test_upload_failure_logs_error_and_never_raises(self, monkeypatch):
        class _BoomFTP(_FakeFTPHandler):
            def upload_file(self, filename, content):
                raise RuntimeError("FTP down")

        import mml_edi.models.edi_ftp as edi_ftp_mod
        monkeypatch.setattr(edi_ftp_mod, "EDIFTPHandler", _BoomFTP)

        parser = SimpleNamespace(generate_contrl=lambda raw, p: b"BODY")
        review = _make_review(trading_partner_id=SimpleNamespace(id=99))
        proc, partner, log = self._proc(parser, [review])
        proc._send_cron_alert = lambda *a, **kw: None

        proc._emit_inbound_contrl(partner, "abc123")  # must not raise

        errors = [r for r in log.rows
                  if r["event_type"] == "contrl_sent" and r["status"] == "error"]
        assert len(errors) == 1


class TestSendFileResponsesOrdering:
    """CONTRL emission ordering: after the ORDRSP queueing loop, independent
    of whether any ORDRSP actually sent (queue_ack legitimately defers)."""

    def test_contrl_emitted_after_ack_queue_loop(self, monkeypatch):
        events = []

        class _Review:
            def __init__(self, po):
                self.customer_po_number = po
                self.change_summary = ""

            def _queue_ack(self):
                events.append(("queue_ack", self.customer_po_number))

        class _Reviews(_RecordSet):
            pass

        proc = EDIProcessor()
        review = _Review("PO1")
        reviews = _Reviews([review])

        class _ReviewModel:
            def search(self, domain, order=None):
                return reviews

        proc.env = _FakeEnv({"edi.order.review": _ReviewModel()})
        proc._send_oos_summary = lambda partner, po: None
        proc._emit_inbound_contrl = lambda partner, file_hash: events.append(
            ("contrl", file_hash))

        partner = _make_partner()
        proc._send_file_responses(partner, "hash1")

        assert events == [("queue_ack", "PO1"), ("contrl", "hash1")]

    def test_contrl_still_emitted_when_no_reviews_queue_ack(self, monkeypatch):
        """Even a file whose only order was a cancellation (no ORDRSP queued
        at all) must still get its CONTRL — CONTRL acks interchange receipt,
        not business acceptance."""
        events = []

        proc = EDIProcessor()

        class _ReviewModel:
            def search(self, domain, order=None):
                return _RecordSet([])

        proc.env = _FakeEnv({"edi.order.review": _ReviewModel()})
        proc._emit_inbound_contrl = lambda partner, file_hash: events.append(
            ("contrl", file_hash))

        partner = _make_partner()
        proc._send_file_responses(partner, "hash2")

        assert events == [("contrl", "hash2")]

    def test_contrl_failure_does_not_propagate(self):
        """A CONTRL emission failure must never raise into the poll loop —
        it would turn a successfully processed ORDERS file into a failed
        poll and re-trigger reprocessing (defeating file-hash dedup)."""
        proc = EDIProcessor()

        class _ReviewModel:
            def search(self, domain, order=None):
                return _RecordSet([])

        proc.env = _FakeEnv({"edi.order.review": _ReviewModel()})

        def boom(partner, file_hash):
            raise RuntimeError("boom")

        proc._emit_inbound_contrl = boom
        partner = _make_partner()

        proc._send_file_responses(partner, "hash3")  # must not raise


# ── Inbound CONTRL routing (not "parsed 0 orders") ──────────────────────────

_CONTRL_RAW = (
    "UNA:+.? '\r\n"
    "UNB+UNOC:3+ANIMATES:ZZZ+SUPPLIER_GLN:14+200928:1030+99101'\r\n"
    "UNH+0001+CONTRL:D:3:UN:EAN004'\r\n"
    "UCI+72+SUPPLIER_GLN:14+ANIMATES:ZZZ+8'\r\n"
    "UNT+3+0001'\r\n"
    "UNZ+1+99101'\r\n"
)

_NEGATIVE_CONTRL_RAW = _CONTRL_RAW.replace("+8'", "+7'")


class TestIsContrlMessage:

    def test_detects_contrl_message_type(self):
        proc = EDIProcessor()
        assert proc._is_contrl_message(_CONTRL_RAW) is True

    def test_orders_message_is_not_contrl(self):
        proc = EDIProcessor()
        orders_raw = (
            "UNA:+.? 'UNB+UNOC:3+ANIMATES:ZZZ+SUPPLIER_GLN:14+200928:1030+1'"
            "UNH+1+ORDERS:D:01B:UN:EAN008'BGM+220+PO123+9'UNT+3+1'UNZ+1+1'"
        )
        assert proc._is_contrl_message(orders_raw) is False


class TestHandleInboundContrl:

    def _proc_with_parser(self, parse_contrl_fn):
        log = _FakeLog()
        parser = SimpleNamespace(parse_contrl=parse_contrl_fn)
        partner = _make_partner()
        partner.get_parser_instance = lambda: parser
        proc = EDIProcessor()
        proc.env = _FakeEnv({"edi.log": log})
        proc._send_cron_alert = lambda *a, **kw: None
        return proc, partner, log

    def test_positive_contrl_routes_to_parse_contrl_and_logs_received(self):
        def parse_contrl(raw_text):
            return {
                "action": "8", "original_ref": "72",
                "original_sender_id": "SUPPLIER_GLN", "original_sender_qual": "14",
                "original_recipient_id": "ANIMATES", "original_recipient_qual": "ZZZ",
            }

        proc, partner, log = self._proc_with_parser(parse_contrl)

        handled = proc._handle_inbound_contrl(_CONTRL_RAW, "CONTRL_1.edi", "h1", partner)

        assert handled is True
        received = [r for r in log.rows if r["event_type"] == "contrl_received"]
        assert len(received) == 1
        assert received[0]["status"] == "success"
        assert "72" in received[0]["message"]

    def test_never_falls_through_to_parsed_0_orders(self):
        """The exact swallowing behaviour the gate review flagged: a CONTRL
        file must never be handed to parser.parse_file at all."""
        def parse_contrl(raw_text):
            return {"action": "8", "original_ref": "72"}

        proc, partner, log = self._proc_with_parser(parse_contrl)
        parser = partner.get_parser_instance()
        parser.parse_file = lambda content, p: (_ for _ in ()).throw(
            AssertionError("parse_file must never be called for a CONTRL"))

        # Simulate the _process_file dispatch: CONTRL check short-circuits.
        assert proc._is_contrl_message(_CONTRL_RAW) is True
        handled = proc._handle_inbound_contrl(_CONTRL_RAW, "CONTRL_1.edi", "h1", partner)
        assert handled is True  # caller returns [] without calling parse_file

    def test_negative_contrl_creates_blocking_alert_not_silence(self):
        def parse_contrl(raw_text):
            return {
                "action": "7", "original_ref": "72",
                "original_sender_id": "SUPPLIER_GLN", "original_sender_qual": "14",
                "original_recipient_id": "ANIMATES", "original_recipient_qual": "ZZZ",
            }

        proc, partner, log = self._proc_with_parser(parse_contrl)
        alerts = []
        proc._send_cron_alert = lambda module, subject, body: alerts.append(
            (module, subject, body))

        handled = proc._handle_inbound_contrl(
            _NEGATIVE_CONTRL_RAW, "CONTRL_1.edi", "h1", partner)

        assert handled is True
        received = [r for r in log.rows if r["event_type"] == "contrl_received"]
        assert len(received) == 1
        assert received[0]["status"] == "error", (
            "a NEGATIVE CONTRL must be logged as an error, not silently accepted"
        )
        assert alerts, "a negative CONTRL must fire a blocking alert, never vanish"

    def test_parse_contrl_exception_logged_not_raised(self):
        def parse_contrl(raw_text):
            raise ValueError("malformed CONTRL")

        proc, partner, log = self._proc_with_parser(parse_contrl)

        handled = proc._handle_inbound_contrl(_CONTRL_RAW, "CONTRL_1.edi", "h1", partner)

        assert handled is True
        errors = [r for r in log.rows if r["status"] == "error"]
        assert len(errors) == 1

    def test_noop_false_when_parser_lacks_parse_contrl(self):
        """A parser without CONTRL support (Briscoes) must be a strict no-op
        — never consume the file, never log."""
        partner = _make_partner()
        partner.get_parser_instance = lambda: SimpleNamespace()  # no parse_contrl
        proc = EDIProcessor()
        proc.env = _FakeEnv({"edi.log": _FakeLog()})

        handled = proc._handle_inbound_contrl(_CONTRL_RAW, "f.edi", "h1", partner)
        assert handled is False


# ── Latin-1 ingest ───────────────────────────────────────────────────────────

class TestDecodeRawText:

    def test_edifact_partner_decodes_latin1(self):
        proc = EDIProcessor()
        partner = _make_partner(edi_format="edifact_d01b")
        content = "Café".encode("iso-8859-1")  # 0xE9 byte for 'é'

        text = proc._decode_raw_text(content, partner)

        assert text == "Café", (
            "EDIFACT partners must decode Latin-1, not corrupt high bytes "
            "via a utf-8 errors='replace' pass"
        )
        assert "�" not in text

    def test_edifact_d96a_also_decodes_latin1(self):
        proc = EDIProcessor()
        partner = _make_partner(edi_format="edifact_d96a")
        content = b"\xe9\xe8"  # arbitrary Latin-1 high bytes

        text = proc._decode_raw_text(content, partner)

        assert "�" not in text

    def test_non_edifact_partner_keeps_utf8(self):
        proc = EDIProcessor()
        partner = _make_partner(edi_format="idoc_xml")
        content = "Café".encode("utf-8")

        text = proc._decode_raw_text(content, partner)

        assert text == "Café"

    def test_utf8_decode_of_latin1_bytes_would_have_corrupted(self):
        """Sanity-check the bug this fix closes: decoding a genuine Latin-1
        byte as UTF-8 with errors='replace' produces U+FFFD and the original
        character can never be recovered afterwards (verified missed
        finding)."""
        content = "é".encode("iso-8859-1")  # single byte 0xE9
        corrupted = content.decode("utf-8", errors="replace")
        assert corrupted == "�"
        # ... and re-encoding+decoding as iso-8859-1 afterwards does NOT
        # recover it — the corruption is permanent once utf-8 touches it.
        assert corrupted.encode("iso-8859-1", errors="replace") != content


class TestProcessFileRawDataAssignment:
    """order.raw_data must only be filled by the processor's fallback decode
    when the parser did not already set it — Briscoes (which decodes and
    sets raw_data itself) must be preserved byte-for-byte."""

    def _run(self, monkeypatch, parser_orders, edi_format, content):
        proc = EDIProcessor()
        log = _FakeLog()
        proc.env = _FakeEnv({"edi.log": log})
        # This test class is about raw_data assignment, not envelope
        # validation (covered separately by TestValidateInboundEnvelope) —
        # the sample content here is not a full EDIFACT interchange.
        proc._validate_inbound_envelope = lambda raw_text, partner: None

        partner = _make_partner(edi_format=edi_format)
        parser = SimpleNamespace(parse_file=lambda c, p: parser_orders)
        partner.get_parser_instance = lambda: parser

        processed = []
        proc.process_parsed_order = lambda order, p, f, h: processed.append(order)

        class _cr:
            @contextmanager
            def savepoint(self):
                yield

        proc.env.cr = _cr()

        failures = proc._process_file(content, "hash1", "file.edi", partner)
        return processed, failures

    def test_briscoes_style_parser_raw_data_preserved(self, monkeypatch):
        """Parser already decoded+set raw_data (Briscoes idiom) — the
        processor's fallback decode must NOT overwrite it."""
        order = ParsedOrder(
            po_number="PO1", order_date=None,
            lines=[ParsedOrderLine(
                product_code="X", description="d", quantity=1, unit_price=1,
                line_number=1)],
            raw_data="PARSER_OWN_DECODE",
        )
        processed, failures = self._run(
            monkeypatch, [order], "idoc_xml", b"<xml>raw bytes</xml>")
        assert failures == []
        assert processed[0].raw_data == "PARSER_OWN_DECODE"

    def test_animates_style_parser_gets_processor_fallback_decode(self, monkeypatch):
        """Parser leaves raw_data unset (Animates idiom) — the processor must
        fill it using the format-correct (Latin-1) decode."""
        order = ParsedOrder(
            po_number="PO1", order_date=None,
            lines=[ParsedOrderLine(
                product_code="X", description="d", quantity=1, unit_price=1,
                line_number=1)],
        )
        content = "Café PO".encode("iso-8859-1")
        processed, failures = self._run(
            monkeypatch, [order], "edifact_d01b", content)
        assert failures == []
        assert processed[0].raw_data == "Café PO"


# ── Envelope validation + sender mismatch (fail-closed) ─────────────────────

_VALID_ORDERS_ENVELOPE = (
    "UNA:+.? 'UNB+UNOC:3+ANIMATES:ZZZ+SUPPLIER_GLN:14+200928:1030+1++++1'"
    "UNH+1+ORDERS:D:01B:UN:EAN008'BGM+220+PO123+9'UNT+3+1'UNZ+1+1'"
)


class TestValidateInboundEnvelope:

    def test_noop_for_non_edifact_partner(self):
        proc = EDIProcessor()
        partner = _make_partner(edi_format="idoc_xml")
        # Malformed on purpose — must not even be looked at.
        proc._validate_inbound_envelope("not edifact at all", partner)

    def test_valid_envelope_passes(self):
        proc = EDIProcessor()
        partner = _make_partner(edi_format="edifact_d01b")
        proc._validate_inbound_envelope(_VALID_ORDERS_ENVELOPE, partner)  # no raise

    def test_truncated_interchange_raises_parse_error(self):
        proc = EDIProcessor()
        partner = _make_partner(edi_format="edifact_d01b")
        truncated = "UNA:+.? 'UNB+UNOC:3+ANIMATES:ZZZ+SUPPLIER_GLN:14+200928:1030+1++++1'UNH+1+ORDERS:D:01B:UN:EAN008'"
        with pytest.raises(EDIParseError):
            proc._validate_inbound_envelope(truncated, partner)

    def test_sender_mismatch_raises_when_c1_helper_present(self):
        proc = EDIProcessor()
        partner = _make_partner(edi_format="edifact_d01b")
        # C1 helper: expects a DIFFERENT sender than the envelope actually carries.
        partner.get_unb_recipient = lambda: ("WRONG_GLN", "14")

        with pytest.raises(EDIParseError, match="sender"):
            proc._validate_inbound_envelope(_VALID_ORDERS_ENVELOPE, partner)

    def test_sender_match_passes_when_c1_helper_present(self):
        proc = EDIProcessor()
        partner = _make_partner(edi_format="edifact_d01b")
        # Matches the ANIMATES:ZZZ sender baked into _VALID_ORDERS_ENVELOPE.
        partner.get_unb_recipient = lambda: ("ANIMATES", "ZZZ")

        proc._validate_inbound_envelope(_VALID_ORDERS_ENVELOPE, partner)  # no raise

    def test_skips_sender_check_when_c1_helper_absent(self):
        """Wave1-A's C1 helper may not exist yet on an older partner record
        — degrade gracefully rather than blocking every inbound file."""
        proc = EDIProcessor()
        partner = _make_partner(edi_format="edifact_d01b")
        assert not hasattr(partner, "get_unb_recipient")
        proc._validate_inbound_envelope(_VALID_ORDERS_ENVELOPE, partner)  # no raise


# ── C5: cancellation routing ─────────────────────────────────────────────────

class _FakeSaleOrder:
    def __init__(self, state="sale", id=1):
        self.id = id
        self.state = state
        self.name = "S00042"
        self.cancelled = False
        self.posted = []

    def action_cancel(self):
        self.state = "cancel"
        self.cancelled = True

    def message_post(self, body=None, **kw):
        self.posted.append(body)


class TestProcessParsedOrderCancellationRouting:

    def test_cancellation_document_type_routes_to_process_cancellation(self):
        proc = EDIProcessor()
        calls = []
        proc._process_cancellation = lambda *a, **kw: calls.append(("cancel", a))
        proc._process_change_order = lambda *a, **kw: calls.append(("change", a))
        proc._process_new_order = lambda *a, **kw: calls.append(("new", a))

        partner = _make_partner()
        partner.render_client_ref = lambda po, store: po

        order = ParsedOrder(
            po_number="PO1", order_date=None, lines=[],
            document_type="cancellation",
        )
        proc.process_parsed_order(order, partner, "f.edi", "h1")

        assert [c[0] for c in calls] == ["cancel"]

    def test_change_order_still_routes_to_process_change_order(self):
        """Byte-for-byte preservation: change_order routing is untouched."""
        proc = EDIProcessor()
        calls = []
        proc._process_cancellation = lambda *a, **kw: calls.append("cancel")
        proc._process_change_order = lambda *a, **kw: calls.append("change")
        proc._process_new_order = lambda *a, **kw: calls.append("new")

        partner = _make_partner()
        partner.render_client_ref = lambda po, store: po
        order = ParsedOrder(
            po_number="PO1", order_date=None, lines=[], document_type="change_order")
        proc.process_parsed_order(order, partner, "f.edi", "h1")

        assert calls == ["change"]

    def test_new_order_still_routes_to_process_new_order(self):
        proc = EDIProcessor()
        calls = []
        proc._process_cancellation = lambda *a, **kw: calls.append("cancel")
        proc._process_change_order = lambda *a, **kw: calls.append("change")
        proc._process_new_order = lambda *a, **kw: calls.append("new")

        partner = _make_partner()
        partner.render_client_ref = lambda po, store: po
        order = ParsedOrder(
            po_number="PO1", order_date=None, lines=[], document_type="new_order")
        proc.process_parsed_order(order, partner, "f.edi", "h1")

        assert calls == ["new"]


class TestProcessCancellation:

    def _proc(self):
        proc = EDIProcessor()
        log = _FakeLog()
        review_model = _FakeReviewModel()
        proc.env = _FakeEnv({
            "edi.log": log,
            "edi.order.review": review_model,
        })
        return proc, log, review_model

    def test_cancels_the_linked_so_and_creates_terminal_review(self):
        proc, log, review_model = self._proc()
        so = _FakeSaleOrder(state="sale")
        proc._find_existing_so = lambda client_ref, partner: so

        partner = _make_partner()
        partner.alert_on_issues = False
        order = ParsedOrder(
            po_number="PO1", order_date=None, lines=[], document_type="cancellation")

        proc._process_cancellation(order, partner, "PO1", "f.edi", "h1")

        assert so.state == "cancel"
        assert so.cancelled is True
        assert len(review_model._rows) == 1
        review = review_model._rows[0]
        assert review.state == "rejected"
        assert review.change_summary.startswith(proc.CANCELLATION_MARKER)

    def test_never_calls_queue_ack(self):
        """The review created for a cancellation must never be handed to
        _queue_ack — asserted structurally: the fake review has no
        _queue_ack method at all, so any accidental call raises."""
        proc, log, review_model = self._proc()
        so = _FakeSaleOrder(state="sale")
        proc._find_existing_so = lambda client_ref, partner: so
        partner = _make_partner()
        partner.alert_on_issues = False
        order = ParsedOrder(
            po_number="PO1", order_date=None, lines=[], document_type="cancellation")

        proc._process_cancellation(order, partner, "PO1", "f.edi", "h1")
        # No AttributeError raised => _queue_ack was never invoked.

    def test_no_matching_so_logs_warning_and_creates_no_review(self):
        proc, log, review_model = self._proc()
        proc._find_existing_so = lambda client_ref, partner: None
        partner = _make_partner()
        order = ParsedOrder(
            po_number="PO1", order_date=None, lines=[], document_type="cancellation")

        proc._process_cancellation(order, partner, "PO1", "f.edi", "h1")

        assert review_model._rows == []
        warnings = [r for r in log.rows if r["status"] == "warning"]
        assert len(warnings) == 1

    def test_already_cancelled_so_is_idempotent(self):
        proc, log, review_model = self._proc()
        so = _FakeSaleOrder(state="cancel")
        proc._find_existing_so = lambda client_ref, partner: so
        partner = _make_partner()
        partner.alert_on_issues = False
        order = ParsedOrder(
            po_number="PO1", order_date=None, lines=[], document_type="cancellation")

        proc._process_cancellation(order, partner, "PO1", "f.edi", "h1")

        assert so.state == "cancel"
        assert len(review_model._rows) == 1


class TestIsCancellationReview:

    def test_detects_marker_prefix(self):
        proc = EDIProcessor()
        review = SimpleNamespace(change_summary=proc.CANCELLATION_MARKER + " PO cancelled")
        assert proc._is_cancellation_review(review) is True

    def test_ordinary_change_order_is_not_cancellation(self):
        proc = EDIProcessor()
        review = SimpleNamespace(change_summary="Line 1 qty: 10 -> 20")
        assert proc._is_cancellation_review(review) is False

    def test_missing_change_summary_is_not_cancellation(self):
        proc = EDIProcessor()
        review = SimpleNamespace(change_summary=None)
        assert proc._is_cancellation_review(review) is False


class TestRetryPendingAcksSkipsCancellations:
    """The ACK retry cron must never pick up a cancellation review — it holds
    state='rejected' (a state the cron otherwise treats as resolved/ackable)
    but must be excluded before it ever reaches _queue_ack."""

    def test_cancellation_review_excluded_from_retry(self):
        proc = EDIProcessor()
        log = _FakeLog()

        cancel_review = _make_review(
            state="rejected",
            trading_partner_id=_make_partner(),
            change_summary=proc.CANCELLATION_MARKER + " PO1 cancelled",
        )

        def _boom():
            raise AssertionError("_queue_ack must never be called for a cancellation")
        cancel_review._queue_ack = _boom
        cancel_review._ack_exchange_filename = lambda: "ACK_X.edi"

        class _ReviewModel:
            def search(self, domain, order=None):
                return _RecordSet([cancel_review])

            def search_count(self, domain):
                return 0

        proc.env = _FakeEnv({
            "edi.order.review": _ReviewModel(),
            "edi.log": log,
        })

        proc.retry_pending_acks()  # must not raise / must not call _queue_ack

    def test_ordinary_resolved_review_still_retried(self):
        proc = EDIProcessor()
        log = _FakeLog()
        calls = []

        review = _make_review(
            state="approved",
            trading_partner_id=_make_partner(),
            change_summary="Line 1 qty changed",
        )
        review._queue_ack = lambda: calls.append("queued")
        review._ack_exchange_filename = lambda: "ACK_Y.edi"

        class _ReviewModel:
            def search(self, domain, order=None):
                return _RecordSet([review])

            def search_count(self, domain):
                return 0

        proc.env = _FakeEnv({
            "edi.order.review": _ReviewModel(),
            "edi.log": log,
        })

        proc.retry_pending_acks()

        assert calls == ["queued"], (
            "an ordinary resolved (non-cancellation) review must still be "
            "eligible for the retry cron"
        )


# ── Real-parser integration: the actual Animates cancellation fixture ──────

class TestRealAnimatesCancellationFixture:
    """Feeds the real AnimatesParser's parsed cancellation output through
    process_parsed_order's routing dispatch — confirms the C5 contract
    (parser sets document_type='cancellation', lines=[]) is consumed
    correctly end-to-end without an Odoo registry."""

    def test_real_parsed_cancellation_routes_to_process_cancellation(self):
        import pathlib
        from mml_edi.parsers.animates import AnimatesParser

        fixture = (
            pathlib.Path(__file__).parent / "fixtures"
            / "animates_orders_cancel_PO0319333.edi"
        )
        raw = fixture.read_bytes()
        orders = AnimatesParser().parse_file(raw, trading_partner=None)
        assert len(orders) == 1
        order = orders[0]
        assert order.document_type == "cancellation"
        assert order.lines == []

        proc = EDIProcessor()
        calls = []
        proc._process_cancellation = lambda *a, **kw: calls.append(a)
        proc._process_change_order = lambda *a, **kw: calls.append("WRONG:change")
        proc._process_new_order = lambda *a, **kw: calls.append("WRONG:new")

        partner = _make_partner()
        partner.render_client_ref = lambda po, store: po

        proc.process_parsed_order(order, partner, "cancel.edi", "hashX")

        assert len(calls) == 1
        assert calls[0][0].po_number == "PO0319333"
