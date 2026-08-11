"""Animates UN/EDIFACT D.01B CONTRL (Syntax and Service Report) builder + parser.

Pure functions, no Odoo. CONTRL is the interchange-level functional acknowledgement
exchanged between Animates and suppliers: it syntactically acknowledges RECEIPT of an
interchange (it does NOT imply business acceptance). DE0083 action code ``8`` =
"interchange received" is the only value Animates uses.

Worked example (Animates_CONTRL.pdf p.8) — an ack returned to the supplier from
Animates acknowledging receipt of interchange 72:

    UNA:+.? '
    UNB+UNOC:3+ANIMATES:ZZZ+SUPPLIER_GLN:14+200928:1030+99101'
    UNH+0001+CONTRL:D:3:UN:EAN004'
    UCI+72+SUPPLIER_GLN:14+ANIMATES:ZZZ+8'
    UNT+3+0001'
    UNZ+1+99101'

Envelope notes (MIG p.13):
- The CONTRL UNB OMITS DE0031 (the trailing ``++++1`` application-reference /
  acknowledgement-request tail that business messages carry). It ends at the
  interchange control reference. ``build_unb(..., contrl=True)`` handles this.
- Interchange parties: supplier = qualifier ``14`` (GLN), Animates = qualifier ``ZZZ``.

UCI parties (octo M-CONTRL-UCI): the UCI S002 sender + S003 recipient are the
ORIGINAL acknowledged interchange's UNB sender/recipient VERBATIM — which is the
REVERSE of the CONTRL envelope's own sender/recipient.

------------------------------------------------------------------------------
build_contrl payload schema (reverse-engineered from the fixture; every value that
varies between acks is carried here):

    {
        # --- the interchange being acknowledged (the ORIGINAL UNB) ---
        "original_ref":           str,  # original interchange control ref (UNB0020) -> UCI DE0020
        "original_sender_id":     str,  # original UNB sender id (S002.0004)  -> UCI S002.0004
        "original_sender_qual":   str,  # original UNB sender qualifier        -> UCI S002.0007
        "original_recipient_id":  str,  # original UNB recipient id            -> UCI S003.0010
        "original_recipient_qual":str,  # original UNB recipient qualifier     -> UCI S003.0007

        # --- this CONTRL interchange's preparation timestamp (UNB date/time) ---
        "date": str,                    # YYMMDD (optional, default "000000")
        "time": str,                    # HHMM   (optional, default "0000")

        # --- optional: override the action code (defaults to "8" = received) ---
        "action": str,                  # DE0083, default "8"
    }

The OUTBOUND CONTRL envelope itself is always supplier(=supplier_gln):14 -> ANIMATES:ZZZ
(MML acking Animates). ``ctrl_ref`` is THIS CONTRL interchange's own control reference;
``msg_ref`` is the UNH/UNT message reference number.
------------------------------------------------------------------------------
"""

from .animates_edifact import (
    Delims, Segment, tokenize, serialize, build_unb, build_unh, build_unt,
    build_unz, pad_ref, EdifactError, DEFAULT_UNA,
)

#: DE0083 — only "interchange received" is used by Animates.
ACTION_INTERCHANGE_RECEIVED = "8"

# UN/EDIFACT DE 0083 values that mean "accepted" on an INBOUND CONTRL.
# The CONTRL MIG only documents '8' for the ack WE send, but SPS Commerce
# acknowledges our interchanges with '7' ("this level acknowledged") and
# rejects with '4' — observed live during certification (ORDRSP ctrl 71,
# DESADV 73, INVOIC 76 => 7; the two malformed INVOICs 74/75 => 4).
ACCEPTED_ACTIONS = frozenset({"1", "7", ACTION_INTERCHANGE_RECEIVED})


def _require(payload: dict, key: str) -> str:
    if key not in payload or payload[key] in (None, ""):
        raise EdifactError("CONTRL payload missing required field %r" % key)
    return str(payload[key])


def extract_unb_identity(text) -> dict:
    """Extract the UNB envelope identity from ANY inbound interchange (not just
    a CONTRL) — used to build the ``original_*`` fields of a ``build_contrl``
    payload acknowledging receipt of that interchange (C4). Accepts str or bytes.

    Returns:
        {
            "original_ref":            str,  # UNB0020 interchange control ref
            "original_sender_id":      str,  # UNB S002.0004
            "original_sender_qual":    str,  # UNB S002.0007
            "original_recipient_id":   str,  # UNB S003.0010
            "original_recipient_qual": str,  # UNB S003.0007
            "date":                    str,  # UNB S004.0017 (YYMMDD) — this
                                               # CONTRL's own prep date defaults
                                               # to the ORIGINAL interchange's,
                                               # the caller may override.
            "time":                    str,  # UNB S004.0019 (HHMM)
        }

    Raises EdifactError if no UNB segment is present.
    """
    if isinstance(text, bytes):
        text = text.decode("latin-1")
    _, segs = tokenize(text)
    unb = next((s for s in segs if s.tag == "UNB"), None)
    if unb is None:
        raise EdifactError("No UNB segment — not a valid EDIFACT interchange")
    return {
        "original_ref": unb.comp(4, 0),
        "original_sender_id": unb.comp(1, 0),
        "original_sender_qual": unb.comp(1, 1),
        "original_recipient_id": unb.comp(2, 0),
        "original_recipient_qual": unb.comp(2, 1),
        "date": unb.comp(3, 0),
        "time": unb.comp(3, 1),
    }


def build_contrl(payload: dict, *, supplier_gln: str = "SUPPLIER_GLN",
                 ctrl_ref: int = 99101, msg_ref: int = 1,
                 sender_qualifier=None, recipient="ANIMATES",
                 recipient_qualifier=None, require_real=False) -> bytes:
    """Build an OUTBOUND CONTRL (supplier -> Animates) acking a received interchange.

    Envelope identity (AN-01, mirrors build_ordrsp): ``supplier_gln``/``ctrl_ref``
    plus the optional ``sender_qualifier``/``recipient``/``recipient_qualifier`` are
    forwarded to ``build_unb`` — pure tests keep relying on the documented
    ``SUPPLIER_GLN:14`` / ``ANIMATES:ZZZ`` worked-example defaults, while a
    PRODUCTION caller (see ``animates.py::AnimatesParser.generate_contrl``) passes
    the trading partner's real identity (``partner.get_unb_sender()`` /
    ``partner.get_unb_recipient()`` — C1) with ``require_real=True``.

    See module docstring for the payload schema. Returns the interchange as bytes
    (latin-1 — EDIFACT UNOC level-C is a single-byte set; ASCII content here).
    """
    original_ref = _require(payload, "original_ref")
    orig_sender_id = _require(payload, "original_sender_id")
    orig_sender_qual = _require(payload, "original_sender_qual")
    orig_recipient_id = _require(payload, "original_recipient_id")
    orig_recipient_qual = _require(payload, "original_recipient_qual")
    action = str(payload.get("action") or ACTION_INTERCHANGE_RECEIVED)
    date = str(payload.get("date") or "000000")
    time = str(payload.get("time") or "0000")

    ref = pad_ref(msg_ref)

    # Envelope: supplier -> Animates, CONTRL (no trailing ++++1).
    unb = build_unb(
        supplier_gln, ctrl_ref, date, time, contrl=True, inbound=False,
        sender_qualifier=sender_qualifier, recipient=recipient,
        recipient_qualifier=recipient_qualifier, require_real=require_real,
    )
    unh = build_unh(ref, "CONTRL", version="D", release="3", agency="UN", assoc="EAN004")

    # UCI: interchange response. S002 = ORIGINAL sender, S003 = ORIGINAL recipient
    # (verbatim from the acked interchange's UNB, i.e. the reverse of this envelope).
    uci = Segment("UCI", [
        [original_ref],
        [orig_sender_id, orig_sender_qual],
        [orig_recipient_id, orig_recipient_qual],
        [action],
    ])

    body = [unh, uci]
    seg_count = len(body) + 1  # UNH..UNT inclusive (UNH + UCI + UNT)
    unt = build_unt(seg_count, ref)
    unz = build_unz(1, ctrl_ref)

    segments = [unb, unh, uci, unt, unz]
    text = serialize(segments, Delims(), una=DEFAULT_UNA)
    return text.encode("latin-1")


def parse_contrl(text) -> dict:
    """Parse a CONTRL interchange and extract the UCI correlation fields, with
    a positive/negative classification the processor can act on directly.

    Accepts str or bytes. Returns a dict the inbound-ack consumer uses to correlate
    Animates' acknowledgements to the interchanges we sent:

        {
            "original_ref":           str,  # UCI DE0020 — acked interchange ctrl ref
            "action":                 str,  # UCI DE0083, e.g. "8"
            "original_sender_id":     str,
            "original_sender_qual":   str,
            "original_recipient_id":  str,
            "original_recipient_qual":str,
            "interchange_ref":        str,  # this CONTRL interchange's own UNB0020
            "positive":               bool, # True iff action == ACTION_INTERCHANGE_RECEIVED
        }

    ``positive`` is True for any acceptance code in ``ACCEPTED_ACTIONS``.
    The MIG documents only ``8`` (interchange received) — that is the code WE
    emit — but SPS Commerce acknowledges with ``7`` and rejects with ``4`` in
    practice, so acceptance must not be keyed to ``8`` alone. Anything outside
    the accepted set (a syntax rejection, an undocumented code, a malformed
    UCI) stays unambiguously negative for the processor to alert on rather
    than being silently treated as success.

    Raises EdifactError if no UCI segment is present (not a CONTRL message).
    """
    if isinstance(text, bytes):
        text = text.decode("latin-1")
    _, segs = tokenize(text)

    uci = next((s for s in segs if s.tag == "UCI"), None)
    if uci is None:
        raise EdifactError("CONTRL has no UCI segment")
    unb = next((s for s in segs if s.tag == "UNB"), None)

    action = uci.comp(3, 0)
    return {
        "original_ref": uci.comp(0, 0),
        "original_sender_id": uci.comp(1, 0),
        "original_sender_qual": uci.comp(1, 1),
        "original_recipient_id": uci.comp(2, 0),
        "original_recipient_qual": uci.comp(2, 1),
        "action": action,
        "interchange_ref": unb.comp(4, 0) if unb is not None else "",
        "positive": action in ACCEPTED_ACTIONS,
    }
