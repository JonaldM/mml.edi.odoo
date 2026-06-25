"""Animates UN/EDIFACT D.01B ORDRSP (Purchase Order Response) builder.

Pure function (no Odoo). Builds an outbound MML -> Animates PO-response interchange
from a plain dict, using the shared envelope/serialize helpers in ``animates_edifact``.

Reverse-engineered verbatim from the MIG golden fixture
``tests/fixtures/animates_ordrsp_expected.edi`` (UNT 39). All control-count and
reference invariants are validated by ``animates_edifact.validate_interchange``.

Message shape (per Animates ORDRSP MIG p49-50 + octo v2 corrections)
-------------------------------------------------------------------
    UNB  envelope  (supplier:14 / ANIMATES:ZZZ, trailing ++++1)
    UNH  ORDRSP:D:01B:UN:EAN009
    BGM  231 (response) + PO-response doc number + 1225 acknowledgement code
         (4 = no action / acknowledged; 29 = accepted, 27 = rejected/changes per MIG)
    DTM  137 (document/message date) + 2 (delivery/requested date)
    RFF  ON = buyer's PO number
    NAD  BY (buyer) / SU (supplier) / ST (ship-to)  -- this fixed order
    CUX  2:NZD:9   (reference currency, rate qualifier 9 for ORDRSP/ORDERS)
    --- per PO line (ALL ordered lines echoed) ---
    LIN  <line_no> + <1229 action: 5 accepted | 7 rejected | 3 changed>
    PIA  5 + <buyer item code / ISC>:IN          (always)
    PIA  1 + <supplier/MML code>:SA              (optional; omit if not supplied)
    IMD  F++:::<description>
    QTY  21:<ordered>:EA                         (optional; omit when not echoed)
    QTY  59:<pack/units-per>                      (always)
    QTY  113:<committed>:EA   (0 when rejected)  (always)
    FTX  LIN+++<reason>       MANDATORY when 1229 == "7" (rejected)
    PRI  AAA:<unit price, 4dp>
    TAX  7 + GST +++:::<rate, 2dp>
    --- summary ---
    UNS  S
    CNT  2:<LIN count>
    UNT  <segment count> + <msg ref>
    UNZ  <message count> + <interchange ctrl ref>

Payload schema
--------------
    {
        "po_response_no": str,        # BGM doc number, e.g. "POR-278156789"
        "ack_code": str,              # BGM 1225, e.g. "4"
        "message_date": str,          # DTM 137 CCYYMMDD, e.g. "20200916"
        "requested_date": str,        # DTM 2  CCYYMMDD, e.g. "20200918"
        "po_number": str,             # RFF ON value, e.g. "PO169603"
        "buyer": str,                 # NAD BY id, e.g. "ANIMATES"
        "supplier": str,              # NAD SU id, e.g. "V1058"
        "ship_to": str,               # NAD ST id, e.g. "12345"
        "currency": str,              # CUX currency, e.g. "NZD"
        "interchange": {              # optional; defaults shown
            "date": str,              # UNB date YYMMDD, default "200916"
            "time": str,              # UNB time HHMM, default "0730"
        },
        "lines": [
            {
                "line_no": str | int,         # LIN line number, e.g. 1
                "action": str,                # LIN 1229: "5"|"7"|"3"
                "buyer_item": str,            # PIA 5 ...:IN value (ISC)
                "supplier_item": str | None,  # PIA 1 ...:SA value; None/"" -> omit PIA+1
                "description": str,           # IMD F free text
                "qty_ordered": str|int|None,  # QTY 21 value; None -> omit QTY+21
                "qty_pack": str | int,        # QTY 59 value
                "qty_committed": str | int,   # QTY 113 value (use "0" on reject)
                "reason": str | None,         # FTX+LIN reason; required when action == "7"
                "price": str,                 # PRI AAA value, EXACT 4dp string ("12.0000")
                "tax_rate": str,              # TAX value, EXACT 2dp string ("15.00")
            },
            ...
        ],
    }

Decimal precision is build-critical: pass ``price`` and ``tax_rate`` as the exact
formatted strings expected on the wire (no float rounding here).
"""

from .animates_edifact import (
    Delims,
    Segment,
    serialize,
    build_unb,
    build_unh,
    build_unt,
    build_unz,
    pad_ref,
    EdifactError,
)

# Fixed coded values for the Animates ORDRSP.
_MSG_TYPE = "ORDRSP"
_ASSOC = "EAN009"          # UNH S009 association assigned code
_BGM_RESPONSE = "231"      # 1001 document name code: order response
_DTM_MESSAGE_QUAL = "137"  # document/message date/time
_DTM_REQUEST_QUAL = "2"    # delivery date/time, requested
_DTM_FORMAT = "102"        # CCYYMMDD
_RFF_PO_QUAL = "ON"        # order number (buyer)
_NAD_PARTY_QUAL = "92"     # 3055 responsible agency, assigned by buyer
_CUX_USAGE = "2"           # reference currency
_CUX_RATE_QUAL = "9"       # ORDRSP/ORDERS rate qualifier
_PIA_BUYER = "5"           # product id function: product identification
_PIA_SUPPLIER = "1"        # additional identification
_CODE_ISC = "IN"           # buyer's item number
_CODE_MML = "SA"           # supplier's article number
_IMD_FORMAT = "F"          # free-form item description
_QTY_ORDERED = "21"        # ordered quantity
_QTY_PACK = "59"           # number of units per pack
_QTY_COMMITTED = "113"     # quantity to be delivered / committed
_FTX_LIN = "LIN"           # line-item free text qualifier
_PRI_QUAL = "AAA"          # calculation net price
_TAX_FUNCTION = "7"        # tax
_TAX_TYPE = "GST"
_UOM_EA = "EA"
_ACTION_REJECTED = "7"


def _line_segments(line: dict) -> list:
    """Build the per-PO-line segment block. Pure; raises EdifactError on bad input."""
    action = str(line["action"])
    segs = [
        Segment("LIN", [[str(line["line_no"])], [action]]),
        Segment("PIA", [[_PIA_BUYER], [str(line["buyer_item"]), _CODE_ISC]]),
    ]

    supplier_item = line.get("supplier_item")
    if supplier_item:
        segs.append(Segment("PIA", [[_PIA_SUPPLIER], [str(supplier_item), _CODE_MML]]))

    segs.append(Segment("IMD", [[_IMD_FORMAT], [], ["", "", "", str(line["description"])]]))

    qty_ordered = line.get("qty_ordered")
    if qty_ordered is not None and qty_ordered != "":
        segs.append(Segment("QTY", [[_QTY_ORDERED, str(qty_ordered), _UOM_EA]]))

    segs.append(Segment("QTY", [[_QTY_PACK, str(line["qty_pack"])]]))
    segs.append(Segment("QTY", [[_QTY_COMMITTED, str(line["qty_committed"]), _UOM_EA]]))

    if action == _ACTION_REJECTED:
        reason = line.get("reason")
        if not reason:
            raise EdifactError(
                "FTX+LIN reason is mandatory when LIN 1229 == 7 (rejected); line %s"
                % line.get("line_no")
            )
        segs.append(Segment("FTX", [[_FTX_LIN], [], [], [str(reason)]]))

    segs.append(Segment("PRI", [[_PRI_QUAL, str(line["price"])]]))
    segs.append(Segment("TAX", [[_TAX_FUNCTION], [_TAX_TYPE], [], [], ["", "", "", str(line["tax_rate"])]]))
    return segs


def build_ordrsp(payload: dict, *, supplier_gln="SUPPLIER_GLN", ctrl_ref=12341, msg_ref=1) -> bytes:
    """Build an Animates ORDRSP interchange from ``payload`` (see module docstring).

    Returns the serialized interchange as latin-1 bytes (EDIFACT UNOC level).
    """
    if not payload.get("lines"):
        raise EdifactError("ORDRSP requires at least one PO line")

    delims = Delims()
    ref = pad_ref(msg_ref)
    interchange = payload.get("interchange") or {}
    unb_date = interchange.get("date", "200916")
    unb_time = interchange.get("time", "0730")

    lines = payload["lines"]

    body = [
        build_unh(ref, _MSG_TYPE, version="D", release="01B", agency="UN", assoc=_ASSOC),
        Segment("BGM", [[_BGM_RESPONSE], [str(payload["po_response_no"])], [str(payload["ack_code"])]]),
        Segment("DTM", [[_DTM_MESSAGE_QUAL, str(payload["message_date"]), _DTM_FORMAT]]),
        Segment("DTM", [[_DTM_REQUEST_QUAL, str(payload["requested_date"]), _DTM_FORMAT]]),
        Segment("RFF", [[_RFF_PO_QUAL, str(payload["po_number"])]]),
        Segment("NAD", [["BY"], [str(payload["buyer"]), "", _NAD_PARTY_QUAL]]),
        Segment("NAD", [["SU"], [str(payload["supplier"]), "", _NAD_PARTY_QUAL]]),
        Segment("NAD", [["ST"], [str(payload["ship_to"]), "", _NAD_PARTY_QUAL]]),
        Segment("CUX", [[_CUX_USAGE, str(payload["currency"]), _CUX_RATE_QUAL]]),
    ]

    for line in lines:
        body += _line_segments(line)

    body.append(Segment("UNS", [["S"]]))
    body.append(Segment("CNT", [["2", str(len(lines))]]))

    # UNT segment count: UNH .. UNT inclusive == body length (incl UNH) + 1 (UNT itself).
    unt_count = len(body) + 1
    body.append(build_unt(unt_count, ref))

    segments = [build_unb(supplier_gln, ctrl_ref, unb_date, unb_time)]
    segments += body
    segments.append(build_unz(1, ctrl_ref))

    text = serialize(segments, delims)
    return text.encode("latin-1")
