"""Outbound UN/EDIFACT D.01B DESADV (Despatch Advice / ASN) builder for Nimbrel.

Pure function — no Odoo. Builds the message body with the shared
``nimbrel_edifact`` helpers (envelope, escaping, validation, comparison) so the
envelope handling can never drift between the Nimbrel messages.

Reference: ``docs/nimbrel/Nimbrel_DESADV.pdf`` p.57 (pallet+cartons, CNT+2:3)
and p.58 (split shipment, UNT=27, CNT+2:1). The p.57 fixture's UNT count is
corrected to 40 (self-consistent with every unit opening its own CPS per
AN-18 — see the module docstring below) rather than the MIG's published-but-
miscounted 39.

Message anatomy
---------------
Header:
    BGM+351+<advice_no>+9                 despatch advice document, function 9 (original)
    DTM+137:<yyyymmdd>:102                document/message date
    DTM+11:<yyyymmdd>:102                 despatch date
    [ALI+++165]                           split-shipment reason (165) — only when ``split`` truthy
    RFF+ON:<po>                           buyer order number
    RFF+CN:<connote>                      carrier / connote reference
    NAD+BY+<buyer>::92                    buyer (Nimbrel)   -- DESADV NAD order is BY/ST/SU
    NAD+ST+<ship_to>::92                  ship-to store
    NAD+SU+<supplier>::92                 supplier code

Pack hierarchy:
    CPS+1++1E                             shipment level (1E)
    PAC+<n_pallets>++09                   shipment total pallets   (only if pallets present)
    PAC+<n_units>++<CT|...>              shipment total handling units
    <per unit ...>
    CNT+2:<lin_total>                     total LIN segments across ALL CPS groups

Per logistic unit (one ``units[i]``), CPS index runs 2..N+1 (every unit opens
its own CPS — see "CPS nesting" below):
    CPS+<idx>+1+3                         unit, parent=1 (shipment), hierarchy level 3
    PAC+1++<09|CT>                        packaging level (09 pallet / CT carton)
    PCI+33E                               marked with SSCC
    GIN+AW+<sscc18>                       the SSCC (pre-minted, supplied in payload)
    [PAC+<inner_cartons>++CT]             pallet only: inner carton count
    LIN+<line_no>[++<gtin>:SRV]          line; GTIN component present only when ``gtin`` given
    PIA+5+<isc>:IN                        ISC (buyer item code) qualifier IN
    PIA+1+<vendor_code>:SA               supplier product code qualifier SA
    QTY+12:<qty>:EA                       despatched quantity, always EA (M-CT-EA)
    [QVR+<committed>:66+BP]               variance: committed qty to follow (split only)
    [DTM+17:<yyyymmdd>:102]              estimated delivery date (split only)

Payload schema (plain dict; every value that varies in the fixtures is carried)
------------------------------------------------------------------------------
{
    "advice_no": "95703",            # BGM 1004 despatch advice number
    "doc_date": "20200921",          # DTM+137 (message/document date)
    "despatch_date": "20200922",     # DTM+11
    "po": "PO0319333",               # RFF+ON
    "connote": "SY00857",            # RFF+CN
    "buyer": "NIMBREL",             # NAD+BY
    "ship_to": "12345",              # NAD+ST (store code)
    "supplier": "V1058",             # NAD+SU (supplier code)
    "split": False,                  # True -> emit ALI+++<ali_code> (default 165)
    "ali_code": "165",               # optional; "165" split (default when split=True)
                                      # or "164" shipment-completes-order (scenario 5B:
                                      # the DESADV that finishes a split order still
                                      # emits ALI, but with 164 not 165 — MIG p.19)
    "shipment_totals": {             # optional; PAC totals under CPS+1 (shipment)
        "pallets": 1,                # -> PAC+<pallets>++09  (omitted if 0/absent)
        "units": 2,                  # -> PAC+<units>++<unit_pac_type>
        "unit_pac_type": "CT",       # packaging code for the units total (default "CT")
    },
    "units": [                       # one entry per logistic unit (pallet or carton)
        {
            "cps_idx": 2,            # CPS hierarchy index (1=shipment; units run 2..N)
            "pac_type": "09",        # "09" pallet, "CT" carton
            "sscc": "00502000000045350114",   # GIN+AW value (18-digit, verbatim)
            "inner_cartons": 8,      # pallet only -> inner PAC+<n>++CT (omit/None for cartons)
            "line_no": "1",          # LIN line number
            "gtin": "0200000126124", # optional -> LIN+n++<gtin>:SRV  (None/absent -> bare LIN+n)
            "isc": "2581281",        # PIA+5 ... :IN
            "vendor_code": "VEN111", # PIA+1 ... :SA
            "qty": "96",             # QTY+12:<qty>:EA
            "committed": "200",      # optional -> QVR+<committed>:66+BP (split variance)
            "eta": "20201015",       # optional -> DTM+17:<eta>:102 (split variance)
        },
        ...
    ],
}

CPS nesting (``cps_idx``)
-------------------------
AN-18 (gate-review fix): EVERY unit — pallet or carton, whether physically carried on a
pallet or standing free — opens its OWN ``CPS`` segment. The MIG worked example (p.56-57)
is explicit: CPS+2 for the pallet, CPS+3 for the pallet-contained carton, CPS+4 for the
free-standing carton — three units, three CPS groups, in strict index order with no gaps
and no suppression. An earlier revision of this builder (and its golden fixture) suppressed
the pallet-contained carton's CPS on the theory that it nests "inside" the pallet's group;
that was a fixture transcription error, not a documented MIG shape, and it made the
builder's own UNT segment count fail to match ``validate_interchange``'s real span.

``cps_idx`` — the CPS index for this unit. Defaults to the unit's 1-based position + 1
when absent, which reproduces the simple split shipment (single unit -> CPS+2). Callers
with pallet+carton hierarchies pass it explicitly to control numbering.

The total LIN count for ``CNT+2`` is derived from ``len(units)`` (one LIN per unit
across all CPS groups), so the build is self-consistent with ``validate_interchange``.
"""

from .nimbrel_edifact import (
    Delims,
    Segment,
    serialize,
    tokenize,
    build_unb,
    build_unh,
    build_unt,
    build_unz,
    pad_ref,
)

# DESADV uses the EANCOM subset association code in the fixtures (UNH ...:EAN008).
_DESADV_ASSOC = "EAN008"


def _seg(tag, *elements):
    """Convenience: build a Segment from already-split elements (lists of components)."""
    return Segment(tag, [list(el) for el in elements])


def _unit_segments(unit):
    """Return the ordered segment list for a single logistic unit (CPS body minus CPS)."""
    out = []
    pac_type = unit["pac_type"]
    out.append(_seg("PAC", ["1"], [""], [pac_type]))      # PAC+1++<09|CT>
    out.append(_seg("PCI", ["33E"]))                       # PCI+33E
    out.append(_seg("GIN", ["AW"], [unit["sscc"]]))        # GIN+AW+<sscc>
    inner = unit.get("inner_cartons")
    if inner:
        out.append(_seg("PAC", [str(inner)], [""], ["CT"]))  # pallet inner carton count

    gtin = unit.get("gtin")
    if gtin:
        out.append(_seg("LIN", [str(unit["line_no"])], [""], [str(gtin), "SRV"]))
    else:
        out.append(_seg("LIN", [str(unit["line_no"])]))

    out.append(_seg("PIA", ["5"], [str(unit["isc"]), "IN"]))           # PIA+5+<isc>:IN
    out.append(_seg("PIA", ["1"], [str(unit["vendor_code"]), "SA"]))   # PIA+1+<vendor>:SA
    out.append(_seg("QTY", ["12", str(unit["qty"]), "EA"]))            # QTY+12:<qty>:EA

    committed = unit.get("committed")
    if committed is not None:
        out.append(_seg("QVR", [str(committed), "66"], ["BP"]))        # QVR+<qty>:66+BP
    eta = unit.get("eta")
    if eta:
        out.append(_seg("DTM", ["17", str(eta), "102"]))              # DTM+17:<eta>:102
    return out


def build_desadv(payload, *, supplier_gln="SUPPLIER_GLN", ctrl_ref=78401, msg_ref=1):
    """Build an outbound Nimbrel DESADV interchange. Returns ``bytes``.

    See the module docstring for the payload schema. Handles both the pallet+carton
    hierarchy (p.57) and the split-shipment variance shape (p.58) from one function.
    """
    ref = pad_ref(msg_ref)

    segs = [
        build_unb(supplier_gln, ctrl_ref, payload.get("doc_date", "")[2:8] or "000000",
                  "0730"),
        build_unh(ref, "DESADV", version="D", release="01B", agency="UN",
                  assoc=_DESADV_ASSOC),
    ]

    body = []
    # --- Header ---
    body.append(_seg("BGM", ["351"], [str(payload["advice_no"])], ["9"]))
    body.append(_seg("DTM", ["137", str(payload["doc_date"]), "102"]))
    body.append(_seg("DTM", ["11", str(payload["despatch_date"]), "102"]))
    if payload.get("split") or payload.get("ali_code"):
        # ALI+++165 (split, subsequent shipment(s) to follow) or ALI+++164
        # (this shipment completes an order previously split — scenario 5B).
        ali_code = payload.get("ali_code") or "165"
        body.append(_seg("ALI", [""], [""], [str(ali_code)]))
    body.append(_seg("RFF", ["ON", str(payload["po"])]))
    body.append(_seg("RFF", ["CN", str(payload["connote"])]))
    # DESADV NAD order is BY / ST / SU (differs from ORDRSP/INVOIC = BY/SU/ST).
    body.append(_seg("NAD", ["BY"], [str(payload["buyer"]), "", "92"]))
    body.append(_seg("NAD", ["ST"], [str(payload["ship_to"]), "", "92"]))
    body.append(_seg("NAD", ["SU"], [str(payload["supplier"]), "", "92"]))

    # --- Shipment-level CPS + totals ---
    body.append(_seg("CPS", ["1"], [""], ["1E"]))
    totals = payload.get("shipment_totals") or {}
    pallets = totals.get("pallets")
    if pallets:
        body.append(_seg("PAC", [str(pallets)], [""], ["09"]))
    units_total = totals.get("units")
    if units_total is not None:
        body.append(_seg("PAC", [str(units_total)], [""], [totals.get("unit_pac_type", "CT")]))

    # --- Per-unit groups (parent=1, level 3). Every unit — pallet or carton,
    # carried on a pallet or free-standing — opens its own CPS (AN-18). ---
    for i, unit in enumerate(payload["units"]):
        cps_idx = unit.get("cps_idx", i + 2)
        body.append(_seg("CPS", [str(cps_idx)], ["1"], ["3"]))
        body.extend(_unit_segments(unit))

    # --- Control: CNT+2 = total LIN across ALL CPS groups ---
    body.append(_seg("CNT", ["2", str(len(payload["units"]))]))

    segs.extend(body)
    # UNT count = UNH..UNT inclusive = UNH + body + UNT
    seg_count = 1 + len(body) + 1
    segs.append(build_unt(seg_count, ref))
    segs.append(build_unz(1, ctrl_ref))

    text = serialize(segs, Delims())
    return text.encode("latin-1")
