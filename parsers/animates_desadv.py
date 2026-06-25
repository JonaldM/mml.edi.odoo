"""Outbound UN/EDIFACT D.01B DESADV (Despatch Advice / ASN) builder for Animates.

Pure function — no Odoo. Builds the message body with the shared
``animates_edifact`` helpers (envelope, escaping, validation, comparison) so the
envelope handling can never drift between the Animates messages.

Reference: ``docs/animates/Animates_DESADV.pdf`` p.57 (pallet+cartons, UNT=39,
CNT+2:3) and p.58 (split shipment, UNT=27, CNT+2:1) — both reproduced verbatim by
the golden fixtures ``tests/fixtures/animates_desadv_pallet.edi`` and
``animates_desadv_split.edi``.

Message anatomy
---------------
Header:
    BGM+351+<advice_no>+9                 despatch advice document, function 9 (original)
    DTM+137:<yyyymmdd>:102                document/message date
    DTM+11:<yyyymmdd>:102                 despatch date
    [ALI+++165]                           split-shipment reason (165) — only when ``split`` truthy
    RFF+ON:<po>                           buyer order number
    RFF+CN:<connote>                      carrier / connote reference
    NAD+BY+<buyer>::92                    buyer (Animates)   -- DESADV NAD order is BY/ST/SU
    NAD+ST+<ship_to>::92                  ship-to store
    NAD+SU+<supplier>::92                 supplier code

Pack hierarchy:
    CPS+1++1E                             shipment level (1E)
    PAC+<n_pallets>++09                   shipment total pallets   (only if pallets present)
    PAC+<n_units>++<CT|...>              shipment total handling units
    <per unit ...>
    CNT+2:<lin_total>                     total LIN segments across ALL CPS groups

Per logistic unit (one ``units[i]``), CPS index runs 2..N+1:
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
    "buyer": "ANIMATES",             # NAD+BY
    "ship_to": "12345",              # NAD+ST (store code)
    "supplier": "V1058",             # NAD+SU (supplier code)
    "split": False,                  # True -> emit ALI+++165 (split shipment)
    "shipment_totals": {             # optional; PAC totals under CPS+1 (shipment)
        "pallets": 1,                # -> PAC+<pallets>++09  (omitted if 0/absent)
        "units": 2,                  # -> PAC+<units>++<unit_pac_type>
        "unit_pac_type": "CT",       # packaging code for the units total (default "CT")
    },
    "units": [                       # one entry per logistic unit (pallet or carton)
        {
            "cps_idx": 2,            # CPS hierarchy index (1=shipment; units run 2..N)
            "emit_cps": True,        # whether THIS unit opens its own CPS group (see below)
            "pac_type": "09",        # "09" pallet, "CT" carton
            "sscc": "00593161000045350112",   # GIN+AW value (18-digit, verbatim)
            "inner_cartons": 8,      # pallet only -> inner PAC+<n>++CT (omit/None for cartons)
            "line_no": "1",          # LIN line number
            "gtin": "9310088126129", # optional -> LIN+n++<gtin>:SRV  (None/absent -> bare LIN+n)
            "isc": "2581281",        # PIA+5 ... :IN
            "vendor_code": "VEN111", # PIA+1 ... :SA
            "qty": "96",             # QTY+12:<qty>:EA
            "committed": "200",      # optional -> QVR+<committed>:66+BP (split variance)
            "eta": "20201015",       # optional -> DTM+17:<eta>:102 (split variance)
        },
        ...
    ],
}

CPS nesting (``emit_cps`` / ``cps_idx``)
---------------------------------------
The p.57 fixture nests a pallet's contained carton *inside* the pallet's CPS group:
the cartons carried on the pallet do NOT open their own ``CPS`` segment — they appear
as further PAC/PCI/GIN/LIN blocks under the pallet's ``CPS``. Free-standing units (the
shipment's own cartons, or every unit in the split shipment) DO open a ``CPS``.

So each unit carries:
- ``emit_cps`` (default ``True``) — emit a leading ``CPS+<cps_idx>+1+3`` for this unit.
  Set ``False`` for a carton physically contained by the preceding pallet.
- ``cps_idx`` — the CPS index used when ``emit_cps`` is True. The numbering in the MIG
  reserves an index per logical position even when a CPS segment is suppressed (pallet=2,
  the contained carton would be 3 but is omitted, the free carton is 4), so ``cps_idx`` is
  carried explicitly rather than auto-derived.

``cps_idx`` defaults to the unit's 1-based position + 1 when absent, which reproduces the
simple split shipment (single unit -> CPS+2).

The total LIN count for ``CNT+2`` is derived from ``len(units)`` (one LIN per unit
across all CPS groups), so the build is self-consistent with ``validate_interchange``.
"""

from .animates_edifact import (
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
    """Build an outbound Animates DESADV interchange. Returns ``bytes``.

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
    if payload.get("split"):
        body.append(_seg("ALI", [""], [""], ["165"]))            # ALI+++165
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

    # --- Per-unit groups (parent=1, level 3). Units contained by a pallet may
    # suppress their own CPS (emit_cps=False) and nest under the pallet's group. ---
    for i, unit in enumerate(payload["units"]):
        if unit.get("emit_cps", True):
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
