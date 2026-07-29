# mml.edi/wizards/nimbrel_store_master_data.py
"""
Nimbrel NZ clinic/store master data, embedded as a static Python table.

GENERATED from docs/nimbrel/Nimbrel-Clinic-Store-Master-File-12112025.xlsx
(sheet "Clinic _ Store Master File", data rows 7-78) via a one-off offline
openpyxl read. Runtime (this wizard, and prod) must NOT need openpyxl or the
xlsx file — this module is the only artifact that ships.

Regenerate by re-running the same extraction against a newer xlsx export and
replacing _NIMBREL_STORES below; keep the row count / dedup logic in this
docstring in sync with whatever script you used.

── Source quirks (gate review AN-13 / OPS-5) ──────────────────────────────

1. Store codes are a MIX of formats: plain 2-digit numeric strings ("02"..
   "58", some stored as Excel ints e.g. 12 for Silverdale -> normalized to
   "12") and "R-xx" retail codes (R-59 Henderson, R-60 Upper Hutt). Codes
   are kept as opaque strings — never int-cast at runtime — because a
   leading zero is significant ("02" != "2").

2. The source file lists 66 site rows across 7 regions, but 10 of those
   codes are DELIBERATE duplicates: a vet clinic co-located inside a retail
   store shares that store's code (explicit note in the source file, row 1:
   "For vet clinics that are located within a retail store, the code will
   be the same."). E.g. code "08" is BOTH "Lincoln Road" (retail, Auckland
   North Region) and "Lincoln Road Clinic" (vet, Auckland/South Vet).

   NAD+ST in an inbound Nimbrel ORDERS carries a single store code with no
   second qualifier to disambiguate retail vs. clinic (parsers/nimbrel.py
   NAD+ST handling: seg.comp(1, 0) verbatim -> ParsedOrder.store_code), and
   ORDERS carries retail product lines, not clinic dispensary items. So for
   the 10 collided codes this table keeps ONE row — the RETAIL site name —
   collapsing the pair under the single ref the parser will ever resolve.
   Seeding both names under the same ref would leave res.partner search
   order to arbitrarily pick a winner (silent misrouting risk flagged by
   AN-13); collapsing at the data layer removes the ambiguity entirely.
   The remaining 7 codes are vet-only (no co-located retail counterpart at
   that code, e.g. "30" Petone Clinic) and are kept with their clinic name.

   66 source rows - 10 collapsed duplicates = 56 unique (code, name, region)
   rows below.

3. Names/regions are stripped of stray whitespace and a BOM character
   (U+FEFF) present in one source cell ("Hornby").

Columns: (store_code, store_name, region)
"""

_NIMBREL_STORES = [
    ("02", "Papanui", "South Region"),
    ("03", "Palmerston North", "Wellington Retail Region"),
    ("04", "Lower Hutt", "Wellington Retail Region"),
    ("05", "Kaiwharawhara", "Wellington Retail Region"),
    ("06", "Tauranga", "Central Region"),
    ("07", "Tower Junction", "South Region"),
    ("08", "Lincoln Road", "Auckland North Region"),
    ("09", "Botany Downs", "Auckland South Region"),
    ("10", "Three Kings", "Auckland South Region"),
    ("11", "Glenfield", "Auckland North Region"),
    ("12", "Silverdale", "Auckland North Region"),
    ("13", "Te Rapa", "Auckland South Region"),
    ("14", "Albany", "Auckland North Region"),
    ("15", "Lunn Avenue", "Auckland South Region"),
    ("16", "Westgate", "Auckland North Region"),
    ("17", "Manukau", "Auckland South Region"),
    ("18", "Takanini", "Auckland South Region"),
    ("19", "Anzac Parade", "Auckland South Region"),
    ("20", "St Lukes", "Auckland North Region"),
    ("21", "Sylvia Park", "Auckland South Region"),
    ("22", "Rotorua", "Central Region"),
    ("23", "Nelson", "South Region"),
    ("24", "New Plymouth", "Wellington Retail Region"),
    ("25", "Richmond Road", "Auckland North Region"),
    ("26", "Hastings", "Central Region"),
    ("27", "Pukekohe", "Auckland South Region"),
    ("28", "Mount Maunganui", "Central Region"),
    ("29", "Dunedin", "South Region"),
    ("30", "Petone Clinic", "Wellington Vet Region"),
    ("31", "Miramar Clinic", "Wellington Vet Region"),
    ("32", "Khandallah Clinic", "Wellington Vet Region"),
    ("33", "Glen Innes", "Auckland South Region"),
    ("34", "Porirua", "Wellington Retail Region"),
    ("35", "Coastlands", "Wellington Retail Region"),
    ("36", "Linwood", "South Region"),
    ("37", "Taupo", "Central Region"),
    ("38", "Whangarei", "Auckland North Region"),
    ("39", "Pitama Clinic", "Wellington Vet Region"),
    ("40", "Hokowhitu Clinic", "Wellington Vet Region"),
    ("41", "Paraparaumu Clinic", "Wellington Vet Region"),
    ("42", "Waikanae Clinic", "Wellington Vet Region"),
    ("43", "Masterton", "Wellington Retail Region"),
    ("44", "New Lynn", "Auckland North Region"),
    ("45", "Invercargill", "South Region"),
    ("46", "Whanganui", "Wellington Retail Region"),
    ("47", "Kilbirnie", "Wellington Retail Region"),
    ("48", "Rangiora", "South Region"),
    ("50", "Tauriko", "Central Region"),
    ("53", "Timaru", "South Region"),
    ("54", "Napier", "Central Region"),
    ("55", "Blenheim", "South Region"),
    ("56", "Gisborne", "Central Region"),
    ("57", "Hornby", "South Region"),
    ("58", "Warkworth", "Auckland North Region"),
    ("R-59", "Henderson", "Auckland North Region"),
    ("R-60", "Upper Hutt", "Wellington Retail Region"),
]


def get_nimbrel_stores():
    """Return the embedded Nimbrel store table as a tuple of
    (store_code, store_name, region) tuples. No I/O, no openpyxl — pure
    in-memory data, safe to call at runtime."""
    return tuple(_NIMBREL_STORES)
