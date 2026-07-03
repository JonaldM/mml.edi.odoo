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


def _nad(qualifier, party):
    """Build a NAD segment shaped like the Animates worked example.

    Layout: ``NAD+<qual>+<code>::92++<name>+<street>+<city>+<state>+<postcode>+<country>``
    The 4th element (C080 alt name) is intentionally empty (``++``).
    """
    return Segment(
        "NAD",
        [
            [qualifier],
            [party.get("code", ""), "", _PARTY_AGENCY],
            [""],  # C080 party name (empty; structured name fields used instead)
            [party.get("name", "")],
            [party.get("street", "")],
            [party.get("city", "")],
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
                 ctrl_ref: int = 12341, msg_ref: int = 1) -> bytes:
    """Build an outbound Animates INVOIC interchange from ``payload``.

    See the module docstring for the full payload schema. Returns the rendered
    interchange as latin-1 bytes (EDIFACT UNOC:3 is a Latin-1 superset; the
    Animates worked examples are ASCII).
    """
    delims = Delims()
    ref = pad_ref(msg_ref)

    buyer = payload["buyer"]
    supplier = payload["supplier"]
    ship_to = payload["ship_to"]
    summary = payload["summary"]
    lines = payload["lines"]

    segments = [
        build_unb(
            supplier_gln,
            ctrl_ref,
            payload["date_yymmdd"],
            payload["time_hhmm"],
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
        _nad("BY", buyer),
        Segment("RFF", [["AMT", str(buyer["nzbn"])]]),
        _nad("SU", supplier),
        Segment("RFF", [["AMT", str(supplier["abn"])]]),
        # CTA OC (information contact) with the contact name in C056 component 2.
        Segment("CTA", [["OC"], ["", supplier.get("contact_name", "")]]),
        Segment("COM", [[supplier.get("phone", ""), "TE"]]),
        Segment("COM", [[supplier.get("email", ""), "EM"]]),
        _nad("ST", ship_to),
        # CUX 2 = reference currency, rate-precision qualifier 4.
        Segment("CUX", [["2", payload.get("currency", "NZD"), "4"]]),
    ]

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
