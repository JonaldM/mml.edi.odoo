"""Shared UN/EDIFACT D.01B helpers for the Nimbrel trading partner.

Pure functions (no Odoo) used by the Nimbrel parser + the ORDRSP/DESADV/INVOIC/
CONTRL builders, so envelope handling and tokenizing can never drift between them.

Nimbrel / SPS Commerce specifics confirmed against the MIG worked examples:
- UNA is always present: ``UNA:+.? '`` (component ``:``, element ``+``, decimal ``.``,
  release ``?``, segment terminator ``'``). The release char must be honoured.
- Interchange parties: the supplier side uses qualifier ``14`` (GLN) or ``ZZZ``
  (mutually defined — the portal test-mailbox / edi_sender_id convention, per the
  CONTRL MIG p.11-12); Nimbrel always uses ``ZZZ``. Both sides ``ZZZ`` is legal.
  NEVER ``14`` on both (that is the Kestrelby idiom and Nimbrel rejects it).
- Business messages (ORDERS/ORDRSP/DESADV/INVOIC) carry the trailing application
  reference + ack-request ``...++++1`` on UNB. CONTRL does NOT (DE0031 omitted) — its
  UNB ends at the interchange control reference.
- No UNG/UNE. UNOC:3 syntax. Numeric interchange control reference.
"""

from dataclasses import dataclass, field

# Default service characters per the Nimbrel UNA. Parsers MUST read the real
# UNA from the message rather than assume these, but they are the documented set.
DEFAULT_UNA = "UNA:+.? '"


@dataclass(frozen=True)
class Delims:
    component: str = ":"
    element: str = "+"
    decimal: str = "."
    release: str = "?"
    segment: str = "'"

    @classmethod
    def from_una(cls, una: str) -> "Delims":
        """Parse a ``UNA......`` string (6 service chars after the literal 'UNA')."""
        if not una or not una.startswith("UNA") or len(una) < 9:
            return cls()
        c = una[3:9]  # component, element, decimal, release, (space=repetition), segment
        return cls(component=c[0], element=c[1], decimal=c[2], release=c[3], segment=c[5])


@dataclass
class Segment:
    tag: str
    # elements: list of elements; each element is a list of components (strings)
    elements: list = field(default_factory=list)

    def comp(self, el_idx, comp_idx=0, default=""):
        """Safe accessor: element el_idx, component comp_idx."""
        if el_idx < len(self.elements) and comp_idx < len(self.elements[el_idx]):
            return self.elements[el_idx][comp_idx]
        return default


def _split_keep(s: str, sep: str, release: str):
    """Split ``s`` on ``sep``, treating ``release+char`` as two literal chars (so an
    escaped separator does NOT split). Does NOT un-escape — release chars are preserved
    so the value can be un-escaped exactly once at the component leaf."""
    out, buf, i, n = [], [], 0, len(s)
    while i < n:
        ch = s[i]
        if ch == release and i + 1 < n:
            buf.append(ch)
            buf.append(s[i + 1])
            i += 2
            continue
        if ch == sep:
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return out


def _unescape(s: str, release: str) -> str:
    """Remove release chars: ``release+char`` -> ``char`` (applied once, at the leaf)."""
    out, i, n = [], 0, len(s)
    while i < n:
        if s[i] == release and i + 1 < n:
            out.append(s[i + 1])
            i += 2
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def tokenize(text: str):
    """Tokenize an EDIFACT interchange into (Delims, [Segment]).

    Honours the message's own UNA service characters and the ``?`` release char.
    Segment terminators and separators inside a released sequence are literal.
    """
    text = text.replace("\r", "").replace("\n", "")
    delims = Delims()
    body = text
    if text.startswith("UNA"):
        delims = Delims.from_una(text[:9])
        body = text[9:]
    raw_segments = _split_keep(body, delims.segment, delims.release)
    segments = []
    for raw in raw_segments:
        if not raw.strip():
            continue
        els = _split_keep(raw, delims.element, delims.release)
        tag = els[0]
        elements = [
            [_unescape(c, delims.release) for c in _split_keep(e, delims.component, delims.release)]
            for e in els[1:]
        ]
        segments.append(Segment(tag=tag, elements=elements))
    return delims, segments


def escape(value, delims: Delims) -> str:
    """Escape service characters in a data value using the release char.

    Release char is escaped FIRST to avoid double-escaping.
    """
    if value is None:
        return ""
    s = str(value)
    s = s.replace(delims.release, delims.release + delims.release)
    for ch in (delims.element, delims.component, delims.segment):
        s = s.replace(ch, delims.release + ch)
    return s


def serialize_segment(seg: Segment, delims: Delims) -> str:
    parts = [seg.tag]
    for el in seg.elements:
        parts.append(delims.component.join(escape(c, delims) for c in el))
    return delims.element.join(parts)


def serialize(segments, delims: Delims = None, una: str = DEFAULT_UNA) -> str:
    """Render segments back to an interchange string (with leading UNA)."""
    delims = delims or Delims()
    body = "".join(serialize_segment(s, delims) + delims.segment for s in segments)
    return (una + body) if una else body


# --- Envelope -------------------------------------------------------------

#: Sentinel identity values that must NEVER reach a production payload. These are
#: the pure-test/documentation defaults (SUPPLIER_GLN placeholder, the frozen
#: worked-example control ref/date from the MIG). A ``require_real=True`` caller
#: gets an EdifactError if any of these leak through, so a production payload
#: path can never silently ship placeholder identity (AN-01 / OPS-identity).
_PLACEHOLDER_SENDER_IDS = frozenset({"SUPPLIER_GLN", "5412345000013"})
_PLACEHOLDER_CTRL_REFS = frozenset({"12341", "99101", "78401"})
_PLACEHOLDER_DATETIMES = frozenset({"200916", "200928", "200915"})


def build_unb(sender_gln, ctrl_ref, date_yymmdd, time_hhmm, *, contrl=False,
              recipient="NIMBREL", inbound=False,
              sender_qualifier=None, recipient_qualifier=None,
              require_real=False):
    """UNB for an MML->Nimbrel interchange (or inverted for inbound/CONTRL parse).

    Backward-compatible defaults: sender qualifies ``14`` (GLN) and recipient
    defaults to ``NIMBREL``:``ZZZ`` unless overridden — this is what every pure
    test and the DESADV/INVOIC/CONTRL builders already pass. Callers that HAVE a
    trading partner record should instead go through ``build_unb_for_partner``
    (or pass ``sender_qualifier``/explicit ``recipient``/``recipient_qualifier``
    themselves) so the real edi_sender_id / TST1NIMBREL-vs-NIMBREL identity is
    used rather than these positional defaults.

    sender_qualifier: DE0007 for the sender S002 (default ``"14"`` — GLN — for
        backward compatibility with the GLN-only call sites).
    recipient_qualifier: DE0007 for the recipient S003 (default ``"ZZZ"``).
    inbound=True inverts the parties (recipient side sends, sender side receives)
        — used for inbound parsing / test-fixture construction, never for
        production outbound payloads.
    require_real=True: raise EdifactError if sender id, control ref, or date/time
        are one of the known placeholder/worked-example sentinel values. Set this
        on every PRODUCTION payload path (never on pure tests) so a caller cannot
        silently ship the ``SUPPLIER_GLN`` / ``12341`` / frozen-2020-timestamp
        defaults documented in AN-01.
    """
    if require_real:
        if str(sender_gln) in _PLACEHOLDER_SENDER_IDS:
            raise EdifactError(
                "build_unb(require_real=True): sender id %r is a placeholder "
                "sentinel — pass the real edi_sender_id/supplier_gln "
                "(see edi.trading.partner.get_unb_sender())" % sender_gln
            )
        if str(ctrl_ref) in _PLACEHOLDER_CTRL_REFS:
            raise EdifactError(
                "build_unb(require_real=True): ctrl_ref %r is a placeholder "
                "sentinel — pass a real value from the "
                "'mml_edi.nimbrel.interchange.ref' sequence" % ctrl_ref
            )
        if str(date_yymmdd) in _PLACEHOLDER_DATETIMES:
            raise EdifactError(
                "build_unb(require_real=True): date_yymmdd %r is a frozen "
                "worked-example sentinel — pass the real interchange "
                "preparation timestamp" % date_yymmdd
            )

    sender_qual = sender_qualifier or "14"
    recipient_qual = recipient_qualifier or "ZZZ"
    supplier = ["%s" % sender_gln, sender_qual]
    nimbrel = [recipient, recipient_qual]
    if inbound:
        send, recv = nimbrel, supplier
    else:
        send, recv = supplier, nimbrel
    seg = Segment("UNB", [
        ["UNOC", "3"],
        send,
        recv,
        [date_yymmdd, time_hhmm],
        [str(ctrl_ref)],
    ])
    if not contrl:
        # appref (empty) + priority (empty) + ack-request (empty) + test (empty) ... per MIG
        # business messages end ...+<ctrl>++++1  -> add 3 empty elements then '1'
        seg.elements += [[""], [""], [""], ["1"]]
    return seg


def build_unb_for_partner(partner, ctrl_ref, date_yymmdd, time_hhmm, *,
                           contrl=False, inbound=False):
    """UNB built from an edi.trading.partner's real identity (C1 helpers).

    This is the PRODUCTION entry point: sender identity comes from
    ``partner.get_unb_sender()`` (raises UserError if unconfigured — no silent
    placeholder fallback) and recipient from ``partner.get_unb_recipient()``
    (switches NIMBREL/TST1NIMBREL on partner.environment). Always builds with
    ``require_real=True``.
    """
    sender_id, sender_qual = partner.get_unb_sender()
    recipient_id, recipient_qual = partner.get_unb_recipient()
    return build_unb(
        sender_id, ctrl_ref, date_yymmdd, time_hhmm,
        contrl=contrl, inbound=inbound,
        recipient=recipient_id,
        sender_qualifier=sender_qual,
        recipient_qualifier=recipient_qual,
        require_real=True,
    )


def build_unh(msg_ref, msg_type, version="D", release="01B", agency="UN", assoc=None):
    """UNH. msg_ref is the message reference (zero-padded, e.g. '0001')."""
    s0065 = [msg_type, version, release, agency]
    if assoc:
        s0065.append(assoc)
    return Segment("UNH", [[str(msg_ref)], s0065])


def build_unt(segment_count, msg_ref):
    return Segment("UNT", [[str(segment_count)], [str(msg_ref)]])


def build_unz(msg_count, ctrl_ref):
    return Segment("UNZ", [[str(msg_count)], [str(ctrl_ref)]])


def pad_ref(n) -> str:
    """Message reference number, zero-padded to 4 (our consistent policy; MIG samples
    vary between '1' and '0001' but only the UNH0062==UNT0062 invariant is validated)."""
    return "%04d" % int(n)


# --- Validation -----------------------------------------------------------

class EdifactError(ValueError):
    pass


def validate_interchange(segments):
    """Validate control-count + reference invariants. Raises EdifactError on violation.

    Checks (per the octo correctness review):
    - UNB sender/recipient qualifiers: each must be a known code (14 or ZZZ) and
      at least one party must be ZZZ (the Nimbrel side always is). The supplier
      side may be 14 (GLN) or ZZZ (edi_sender_id convention) per the CONTRL MIG,
      so both-ZZZ is valid; 14 on both (the Kestrelby idiom) is rejected.
    - UNB 0020 == UNZ 0020 (interchange control reference)
    - each UNH 0062 == its UNT 0062
    - each UNT 0074 == number of segments from UNH..UNT inclusive
    - UNZ 0036 == number of UNH segments
    - CNT+2 (where present) == number of LIN segments in that message
    """
    by_tag = lambda t: [s for s in segments if s.tag == t]
    unb = by_tag("UNB")
    unz = by_tag("UNZ")
    if not unb or not unz:
        raise EdifactError("missing UNB/UNZ")
    quals = {unb[0].comp(1, 1), unb[0].comp(2, 1)}
    if not quals <= {"14", "ZZZ"} or "ZZZ" not in quals:
        raise EdifactError(
            "UNB party qualifiers must each be 14 or ZZZ with at least one "
            "ZZZ party, got %s" % quals)
    if unb[0].comp(4, 0) != unz[0].comp(1, 0):
        raise EdifactError("UNB0020 != UNZ0020")

    # walk messages
    msg_count = 0
    i = 0
    while i < len(segments):
        if segments[i].tag == "UNH":
            start = i
            ref = segments[i].comp(0, 0)
            j = i + 1
            while j < len(segments) and segments[j].tag != "UNT":
                j += 1
            if j >= len(segments):
                raise EdifactError("UNH without UNT")
            unt = segments[j]
            if unt.comp(1, 0) != ref:
                raise EdifactError("UNH0062 %s != UNT0062 %s" % (ref, unt.comp(1, 0)))
            count = (j - start) + 1  # UNH..UNT inclusive
            if str(count) != unt.comp(0, 0):
                raise EdifactError("UNT0074 %s != actual segment count %d" % (unt.comp(0, 0), count))
            lin = sum(1 for s in segments[start:j + 1] if s.tag == "LIN")
            cnt2 = [s for s in segments[start:j + 1] if s.tag == "CNT" and s.comp(0, 0) == "2"]
            if cnt2 and cnt2[0].comp(0, 1) and int(cnt2[0].comp(0, 1)) != lin:
                raise EdifactError("CNT+2 %s != LIN count %d" % (cnt2[0].comp(0, 1), lin))
            msg_count += 1
            i = j + 1
        else:
            i += 1
    if str(msg_count) != unz[0].comp(0, 0):
        raise EdifactError("UNZ0036 %s != message count %d" % (unz[0].comp(0, 0), msg_count))
    return True


# --- Normalized comparison (for golden-file tests) ------------------------

def normalized_segments(text, *, ignore_ref_padding=True):
    """Return a comparable representation: list of (tag, elements) with msg-reference
    padding normalized, so two messages equal in structure compare equal regardless of
    UNH/UNT 0062 padding ('1' vs '0001') and the UNA prefix. Money/qty values are kept
    verbatim (precision IS build-critical)."""
    _, segs = tokenize(text)
    out = []
    for s in segs:
        elements = [list(el) for el in s.elements]
        if ignore_ref_padding and s.tag in ("UNH", "UNT"):
            idx = 0 if s.tag == "UNH" else 1
            if idx < len(elements) and elements[idx] and elements[idx][0].isdigit():
                elements[idx][0] = str(int(elements[idx][0]))
        out.append((s.tag, elements))
    return out


def assert_equivalent(actual, expected, *, ignore_ref_padding=True):
    """Assert two interchanges are segment-equivalent (normalized). Returns True or
    raises AssertionError with the first differing segment."""
    a = normalized_segments(actual, ignore_ref_padding=ignore_ref_padding)
    e = normalized_segments(expected, ignore_ref_padding=ignore_ref_padding)
    if len(a) != len(e):
        raise AssertionError("segment count differs: actual %d vs expected %d" % (len(a), len(e)))
    for idx, (sa, se) in enumerate(zip(a, e)):
        if sa != se:
            raise AssertionError("segment %d differs:\n  actual:   %r\n  expected: %r" % (idx, sa, se))
    return True
