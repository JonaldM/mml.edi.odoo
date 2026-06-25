"""Shared UN/EDIFACT D.01B helpers for the Animates trading partner.

Pure functions (no Odoo) used by the Animates parser + the ORDRSP/DESADV/INVOIC/
CONTRL builders, so envelope handling and tokenizing can never drift between them.

Animates / SPS Commerce specifics confirmed against the MIG worked examples:
- UNA is always present: ``UNA:+.? '`` (component ``:``, element ``+``, decimal ``.``,
  release ``?``, segment terminator ``'``). The release char must be honoured.
- Interchange parties: the supplier side uses qualifier ``14`` (GLN), Animates uses
  ``ZZZ`` (mutually defined). NEVER ``14`` on both (that is the Briscoes idiom and
  Animates rejects it).
- Business messages (ORDERS/ORDRSP/DESADV/INVOIC) carry the trailing application
  reference + ack-request ``...++++1`` on UNB. CONTRL does NOT (DE0031 omitted) — its
  UNB ends at the interchange control reference.
- No UNG/UNE. UNOC:3 syntax. Numeric interchange control reference.
"""

from dataclasses import dataclass, field

# Default service characters per the Animates UNA. Parsers MUST read the real
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

def build_unb(sender_gln, ctrl_ref, date_yymmdd, time_hhmm, *, contrl=False,
              recipient="ANIMATES", inbound=False):
    """UNB for an MML->Animates interchange (or inverted for inbound/CONTRL parse).

    sender = supplier GLN qualified ``14``; recipient = ``ANIMATES`` qualified ``ZZZ``.
    Business messages carry the trailing ``++++1`` (appref + ack request); CONTRL omits it.
    inbound=True inverts the parties (ANIMATES:ZZZ sender, supplier:14 recipient).
    """
    supplier = ["%s" % sender_gln, "14"]
    animates = [recipient, "ZZZ"]
    if inbound:
        send, recv = animates, supplier
    else:
        send, recv = supplier, animates
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
    - UNB sender/recipient qualifiers (supplier 14 + ANIMATES ZZZ in either direction)
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
    if quals != {"14", "ZZZ"}:
        raise EdifactError("UNB party qualifiers must be {14, ZZZ}, got %s" % quals)
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
