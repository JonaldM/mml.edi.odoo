"""Outbound UN/EDIFACT D.01B INVOIC (tax invoice) builder for Animates.

Pure function (no Odoo). Builds the INVOIC interchange from a plain ``dict`` using
the shared :mod:`animates_edifact` helpers + :class:`Segment` for the body, then
returns the rendered interchange as ``bytes``.

Reverse-engineered against the verbatim MIG worked example
(``docs/animates/Animates_INVOIC.pdf`` p64-65 / ``tests/fixtures/animates_invoic_expected.edi``).

Message skeleton (segment order is build-critical)::

    UNA
    UNB  envelope (supplier:14 -> ANIMATES:ZZZ, business tail ++++1)
    UNH  INVOIC:D:01B:UN:EAN011
    BGM  388:::TAX INVOICE + invoice number + 9 (original)
    DTM  137 invoice date
    RFF  AAK  delivery/despatch reference
    RFF  CN   consignment / connote reference
    RFF  ON   purchase-order reference
    NAD  BY   buyer (Animates)
      RFF AMT buyer NZBN
    NAD  SU   supplier (us)
      RFF AMT supplier ABN
      CTA OC  + contact name
      COM     phone (TE)
      COM     email (EM)
    NAD  ST   ship-to store
    CUX  2:NZD:4  invoice currency (reference, 4dp rate qualifier)
    --- one SG (LINE) group per line item ---
    LIN  line number
    PIA  5 + buyer item code (IN = ISC)
    PIA  1 + supplier item code (SA = MML)
    IMD  F + description
    QTY  47 invoiced quantity (+ unit) ; QTY 59 number of consumer units
    MOA  128 line amount EX tax (incl allowances/charges)   (4dp)
    MOA  369 line tax amount                                (4dp)
    MOA  203 line item amount INCL tax                      (4dp)
    PRI  AAA unit price (net, excl tax)                     (4dp)
    TAX  7 + GST + rate (5118, 2dp)
    --- summary ---
    UNS  S
    CNT  2 : <line count>
    MOA  39 total payable (incl tax)         (2dp)
    MOA  128 total EX tax                    (2dp)
    MOA  369 total tax                       (2dp)
    UNT
    UNZ

    GST semantics (Animates_INVOIC.pdf p.52 Segment Group 27 + p.60-61 Segment
    Group 50 data-element notes; corrects gate-review finding AN-INVOIC/#21
    which had this inverted): at BOTH line and summary level, MOA 128 is
    "Total amount excluding GST but including allowances or charges" (the
    EX-tax base), MOA 369 is the GST amount, and the INCL-tax total is a
    SEPARATE qualifier per level — MOA 203 at line level ("Line item amount
    ... including GST"), MOA 39 at summary level ("Total amount for the
    invoice including GST" / total payable). PRI+AAA is always the net unit
    price excluding tax. The worked-example fixture values already satisfy
    this (128 + 369 ~= 203/39); only the docstring/comments were wrong — no
    builder logic or fixture change was needed.

PAYLOAD SCHEMA
--------------
All money/quantity values are passed as EXACT decimal STRINGS (precision is
build-critical; the builder never re-formats them). The caller is responsible for
formatting line amounts/price to 4dp and summary amounts/tax rate to 2dp.

::

    {
      # --- envelope timestamp ---
      "date_yymmdd": "200918",   # UNB date  (str, YYMMDD)
      "time_hhmm":   "2130",     # UNB time  (str, HHMM)

      # --- BGM (document) ---
      "invoice_number": "INV566343",   # BGM C106 (1004)
      "message_function": "9",          # BGM 1225 (9 = original).  Default "9".

      # --- DTM ---
      "invoice_date": "20200918",       # DTM 137 value (CCYYMMDD); format 102

      # --- header references ---
      "ref_aak": "25488",        # RFF AAK  (despatch advice / delivery note no.)
      "ref_cn":  "9900857",      # RFF CN   (consignment / connote)
      "ref_on":  "POR169603",    # RFF ON   (purchase order)

      # --- parties ---
      "buyer": {                 # NAD BY
        "code": "ANIMATES",      # C082 party id (qualifier 92 = assigned by buyer)
        "name": "Animates NZ Holding LTD",
        "street": "PO BOX 11959 Ellerslie",
        "city": "Auckland",
        "state": "",             # C059/3164-like state element (empty for NZ here)
        "postcode": "1051",
        "country": "NZ",
        "nzbn": "9429040432250", # RFF AMT under the buyer NAD
      },
      "supplier": {              # NAD SU
        "code": "V1058",         # Animates-assigned supplier code (qualifier 92)
        "name": "M&M Pty Ltd",
        "street": "PO BOX 999",
        "city": "Richmond",
        "state": "VIC",
        "postcode": "3121",
        "country": "AU",
        "abn": "12345678901",    # RFF AMT under the supplier NAD
        "contact_name": "Ms M",  # CTA OC  (C056 component 2)
        "phone": "03 9077 0683", # COM ... :TE
        "email": "MM@mimo.com.au",  # COM ... :EM
      },
      "ship_to": {               # NAD ST
        "code": "12345",         # store code (qualifier 92)
        "name": "Animates Invercargill",
        "street": "186 Tay Street",
        "city": "Invercargill",
        "state": "",
        "postcode": "9810",
        "country": "NZ",
      },

      # --- currency ---
      "currency": "NZD",         # CUX 6345 (qualifier 2 = reference currency, rate 4)

      # --- line items (one SG per entry) ---
      "lines": [
        {
          "line_no": "1",                 # LIN 1082
          "buyer_item": "122134",         # PIA 5 + ... :IN  (ISC, buyer item code)
          "supplier_item": "5101000",     # PIA 1 + ... :SA  (MML item code)
          "description": "Product Description",  # IMD F + :::<desc>
          "qty_invoiced": "2",            # QTY 47 value
          "qty_unit": "EA",               # QTY 47 unit (omit/empty -> no unit comp)
          "qty_consumer_units": "1",      # QTY 59 value (no unit)
          "moa_128": "264.8800",          # line amount EX tax (+ allow/charge) (4dp str)
          "moa_369": "39.7300",           # line tax amount                     (4dp str)
          "moa_203": "304.6100",          # line item amount INCL tax           (4dp str)
          "price": "132.4400",            # PRI AAA unit price (net, excl tax)  (4dp str)
          "tax_rate": "15.00",            # TAX 5118 rate                 (2dp str)
          "tax_category": "GST",          # TAX C241 (5153). Default "GST".
        },
      ],

      # --- summary totals (2dp strings) ---
      "summary": {
        "moa_39":  "304.61",     # total amount payable, INCL tax
        "moa_128": "264.88",     # total amount EX tax
        "moa_369": "39.73",      # total tax amount
      },
    }

NOTE the fixture values above are the verbatim MIG worked example, whose line
amounts (264.88 EX-tax + 39.73 tax ~= 304.61 INCL-tax) and summary amounts are
internally consistent with the corrected EX/INCL semantics documented above —
double-checked against Animates_INVOIC.pdf's own Segment Group 50 example
(MOA+128:354.17 EX tax + MOA+369:53.13 tax = MOA+39:407.30 payable).
"""

from .animates_edifact import (
    DEFAULT_UNA,
    Delims,
    Segment,
    build_unb,
    build_unh,
    build_unt,
    build_unz,
    pad_ref,
    serialize,
    validate_interchange,
    tokenize,
)


# EANCOM association assigned code carried in UNH S009/0057 for Animates INVOIC.
_INVOIC_ASSOC = "EAN011"
# NAD party-id agency: 92 = "assigned by buyer or buyer's agent".
_PARTY_AGENCY = "92"


# MIG maximum lengths for the NAD free-text sub-elements (EANCOM D.01B).
_NAD_NAME_MAX = 35    # NAD040-010 Party name
_NAD_STREET_MAX = 35  # NAD050-010 Street and number
_NAD_CITY_MAX = 35    # NAD060 City name


def _clip(value, limit):
    """Trim a free-text element to the guideline's maximum length."""
    text = str(value or "")
    return text[:limit] if len(text) > limit else text


def _nad(qualifier, party):
    """Build a NAD segment shaped like the Animates worked example.

    Layout: ``NAD+<qual>+<code>::92++<name>+<street>+<city>+<state>+<postcode>+<country>``
    The 4th element (C080 alt name) is intentionally empty (``++``).

    Free-text party/address elements are clipped to the MIG's maximum lengths —
    SPS rejects an over-length value outright ("The length of Sub-Element
    NAD040-010 (Party name) is '38'. The maximum allowed length is '35'"), and an
    Odoo partner name is not bounded by the guideline.
    """
    return Segment(
        "NAD",
        [
            [qualifier],
            [party.get("code", ""), "", _PARTY_AGENCY],
            [""],  # C080 party name (empty; structured name fields used instead)
            [_clip(party.get("name", ""), _NAD_NAME_MAX)],
            [_clip(party.get("street", ""), _NAD_STREET_MAX)],
            [_clip(party.get("city", ""), _NAD_CITY_MAX)],
            [party.get("state", "")],
            [party.get("postcode", "")],
            [party.get("country", "")],
        ],
    )


def _qty(qualifier, value, unit=None):
    """QTY segment. With a unit -> ``QTY+47:2:EA``; without -> ``QTY+59:1``."""
    comps = [qualifier, str(value)]
    if unit:
        comps.append(unit)
    return Segment("QTY", [comps])


def _line_segments(line):
    """All segments for one invoice SG (LINE) group, in fixed MIG order."""
    segs = [
        Segment("LIN", [[str(line["line_no"])]]),
        # PIA 5 (additional identification) carrying the buyer item code (IN = ISC).
        Segment("PIA", [["5"], [str(line["buyer_item"]), "IN"]]),
        # PIA 1 (additional identification) carrying the supplier/MML item code (SA).
        Segment("PIA", [["1"], [str(line["supplier_item"]), "SA"]]),
        # IMD F (free-form) description in C273 component 4.
        Segment("IMD", [["F"], [""], ["", "", "", line.get("description", "")]]),
        _qty("47", line["qty_invoiced"], line.get("qty_unit")),
        _qty("59", line["qty_consumer_units"]),
        Segment("MOA", [["128", str(line["moa_128"])]]),
        Segment("MOA", [["369", str(line["moa_369"])]]),
        Segment("MOA", [["203", str(line["moa_203"])]]),
        Segment("PRI", [["AAA", str(line["price"])]]),
        # TAX 7 (tax) + category (GST) + rate in C243 component 4 (5278/5118).
        Segment(
            "TAX",
            [
                ["7"],
                [line.get("tax_category", "GST")],
                [""],
                [""],
                ["", "", "", str(line["tax_rate"])],
            ],
        ),
    ]
    return segs


def build_invoic(payload: dict, *, supplier_gln: str = "SUPPLIER_GLN",
                 ctrl_ref: int = 12341, msg_ref: int = 1,
                 sender_qualifier: str | None = None,
                 recipient: str | None = None,
                 recipient_qualifier: str | None = None) -> bytes:
    """Build an outbound Animates INVOIC interchange from ``payload``.

    See the module docstring for the full payload schema. Returns the rendered
    interchange as latin-1 bytes (EDIFACT UNOC:3 is a Latin-1 superset; the
    Animates worked examples are ASCII).

    ``sender_qualifier`` / ``recipient`` / ``recipient_qualifier`` forward to
    :func:`build_unb`. Callers holding an ``edi.trading.partner`` MUST pass them
    (from ``get_unb_sender``/``get_unb_recipient``), otherwise build_unb's
    backward-compatible defaults address the PRODUCTION mailbox (``ANIMATES``,
    sender qualifier ``14``) — silently misrouting TEST interchanges. See AN-01/C1.
    """
    delims = Delims()
    ref = pad_ref(msg_ref)

    buyer = payload["buyer"]
    supplier = payload["supplier"]
    ship_to = payload["ship_to"]
    summary = payload["summary"]
    lines = payload["lines"]

    unb_kwargs = {}
    if recipient is not None:
        unb_kwargs["recipient"] = recipient
    if sender_qualifier is not None:
        unb_kwargs["sender_qualifier"] = sender_qualifier
    if recipient_qualifier is not None:
        unb_kwargs["recipient_qualifier"] = recipient_qualifier

    segments = [
        build_unb(
            supplier_gln,
            ctrl_ref,
            payload["date_yymmdd"],
            payload["time_hhmm"],
            **unb_kwargs,
        ),
        build_unh(ref, "INVOIC", version="D", release="01B", agency="UN",
                  assoc=_INVOIC_ASSOC),
        # BGM 388 (commercial invoice) document name, free name "TAX INVOICE",
        # document number, and 1225 message function (9 = original).
        Segment(
            "BGM",
            [
                ["388", "", "", "TAX INVOICE"],
                [str(payload["invoice_number"])],
                [str(payload.get("message_function", "9"))],
            ],
        ),
        # DTM 137 = document/message date, format 102 (CCYYMMDD).
        Segment("DTM", [["137", str(payload["invoice_date"]), "102"]]),
        Segment("RFF", [["AAK", str(payload["ref_aak"])]]),
        Segment("RFF", [["CN", str(payload["ref_cn"])]]),
        Segment("RFF", [["ON", str(payload["ref_on"])]]),
    ]

    # A composite whose only value is blank still renders its separator, and SPS
    # rejects that: "There are extra trailing Sub-Element separators at the end
    # of Composite RFF010/CTA020". So every optional party detail below is
    # emitted ONLY when it actually has a value, never as an empty shell.
    def _val(mapping, key):
        return str(mapping.get(key) or "").strip()

    segments.append(_nad("BY", buyer))
    if _val(buyer, "nzbn"):
        segments.append(Segment("RFF", [["AMT", _val(buyer, "nzbn")]]))
    segments.append(_nad("SU", supplier))
    if _val(supplier, "abn"):
        segments.append(Segment("RFF", [["AMT", _val(supplier, "abn")]]))

    # CTA OC (information contact), contact name in C056 component 2. The COM
    # segments belong to this CTA group, so when there is no name we still emit
    # a bare CTA+OC (dropping it would orphan the COMs) — just without the
    # empty composite that trips CTA020.
    contact_name = _val(supplier, "contact_name")
    if contact_name:
        segments.append(Segment("CTA", [["OC"], ["", contact_name]]))
    else:
        segments.append(Segment("CTA", [["OC"]]))
    if _val(supplier, "phone"):
        segments.append(Segment("COM", [[_val(supplier, "phone"), "TE"]]))
    if _val(supplier, "email"):
        segments.append(Segment("COM", [[_val(supplier, "email"), "EM"]]))

    segments.append(_nad("ST", ship_to))
    # CUX 2 = reference currency, rate-precision qualifier 4.
    segments.append(Segment("CUX", [["2", payload.get("currency", "NZD"), "4"]]))

    for line in lines:
        segments.extend(_line_segments(line))

    # --- summary section ---
    segments.append(Segment("UNS", [["S"]]))
    # CNT 2 = number of line items in the message (envelope invariant: == LIN count).
    segments.append(Segment("CNT", [["2", str(len(lines))]]))
    segments.append(Segment("MOA", [["39", str(summary["moa_39"])]]))
    segments.append(Segment("MOA", [["128", str(summary["moa_128"])]]))
    segments.append(Segment("MOA", [["369", str(summary["moa_369"])]]))

    # --- trailers ---
    # UNT count = number of segments from UNH..UNT inclusive (computed, never hardcoded).
    unh_index = next(i for i, s in enumerate(segments) if s.tag == "UNH")
    seg_count = (len(segments) - unh_index) + 1  # +1 for the UNT we are about to add
    segments.append(build_unt(seg_count, ref))
    segments.append(build_unz(1, ctrl_ref))

    # Validate control-count + reference invariants before emitting.
    validate_interchange(segments)

    text = serialize(segments, delims, una=DEFAULT_UNA)
    return text.encode("latin-1")


__all__ = ["build_invoic"]
