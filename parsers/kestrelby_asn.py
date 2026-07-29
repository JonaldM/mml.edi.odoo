"""
KestrelbyASNGenerator — generates EDIFACT DESADV D96A for Kestrelby Group.

Synthesised from Kestrelby EDIFACT D96A conventions (from ORDERS implementation guide).
Format: UNOA:3, DESADV:D:96A:UN:EAN008, segment terminator '

DESADV structure:
  UNB - interchange envelope header
  UNH - message header
  BGM+351 - despatch advice
  DTM+137 - document creation date
  DTM+11  - actual despatch date
  NAD+SE  - seller (MML)
  NAD+BY  - buyer (Kestrelby Group)
  RFF+ON  - order reference (Kestrelby PO number)
  -- per store:
  CPS     - consignment packing sequence
  NAD+DP  - deliver-to (store GLN or store code)
  -- per line:
  LIN     - line item with EAN-13
  QTY+12  - despatch quantity
  UNS+S   - section control
  CNT+2   - line count
  UNT     - message trailer (segment count)
  UNZ     - interchange trailer
"""
import logging
from datetime import datetime, timezone

from .kestrelby import _edifact_escape

_logger = logging.getLogger(__name__)

_KESTRELBY_GLN = '0200099000008'
_SEG_TERM = "'"


def _ean13_valid(barcode: str) -> bool:
    """Validate EAN-13 barcode including check digit."""
    if not barcode or len(barcode) != 13 or not barcode.isdigit():
        return False
    digits = [int(c) for c in barcode]
    total = sum(d * (3 if i % 2 else 1) for i, d in enumerate(digits[:-1]))
    return (10 - total % 10) % 10 == digits[-1]


class KestrelbyASNGenerator:
    """Generates an EDIFACT DESADV D96A message for a Kestrelby despatch.

    Usage:
        gen = KestrelbyASNGenerator()
        edifact_str = gen.generate(despatch_dict)

    despatch dict schema:
        po_number      str   Kestrelby PO number (e.g. '4500038166')
        despatch_ref   str   Unique ASN reference (e.g. 'DASN-4500038166')
        despatch_date  str   YYYYMMDD
        mml_edis_id    str   MML sender ID on EDIS VAN (from ir.config_parameter)
        ctrl_ref       str   Interchange control reference (unique per interchange)
        deliveries     list  [{'store_gln': str, 'lines': [{'ean13': str, 'qty': int, 'seq': int}]}]
    """

    def generate(self, despatch: dict) -> str:
        """Generate and return the full EDIFACT DESADV message as a string."""
        self._validate(despatch)

        now = datetime.now(timezone.utc)
        date_yymmdd = now.strftime('%y%m%d')
        time_hhmm = now.strftime('%H%M')

        segments = []

        # Interchange header
        segments.append(
            'UNB+UNOA:3+{mml}:14+{kestrelby}:14+{date}:{time}+{ref}++DESADV'.format(
                mml=_edifact_escape(despatch['mml_edis_id']),
                kestrelby=_KESTRELBY_GLN,
                date=date_yymmdd,
                time=time_hhmm,
                ref=_edifact_escape(despatch['ctrl_ref']),
            )
        )

        # Message header
        segments.append('UNH+1+DESADV:D:96A:UN:EAN008')

        # Beginning of message (351 = despatch advice)
        segments.append('BGM+351+{ref}+9'.format(ref=_edifact_escape(despatch['despatch_ref'])))

        # Document date
        segments.append('DTM+137:{date}:102'.format(date=despatch['despatch_date']))

        # Despatch date
        segments.append('DTM+11:{date}:102'.format(date=despatch['despatch_date']))

        # Seller (MML)
        segments.append('NAD+SE+{mml}::14'.format(mml=_edifact_escape(despatch['mml_edis_id'])))

        # Buyer (Kestrelby Group)
        segments.append('NAD+BY+{gln}::14'.format(gln=_KESTRELBY_GLN))

        # PO reference
        segments.append('RFF+ON:{po}'.format(po=_edifact_escape(despatch['po_number'])))

        # Deliveries (one consignment packing sequence per store)
        lin_count = 0
        for cps_seq, delivery in enumerate(despatch['deliveries'], start=1):
            segments.append('CPS+{n}'.format(n=cps_seq))
            segments.append('NAD+DP+{gln}::92'.format(gln=delivery['store_gln']))

            for line in delivery['lines']:
                segments.append('LIN+{seq}++{ean}:EN'.format(
                    seq=line['seq'], ean=line['ean13']
                ))
                segments.append('QTY+12:{qty}:EA'.format(qty=int(line['qty'])))
                lin_count += 1

        # Section control
        segments.append('UNS+S')

        # Control total — LIN count
        segments.append('CNT+2:{n}'.format(n=lin_count))

        # Message trailer
        # Segment count = all segments from UNH to UNT inclusive
        # At this point segments has: UNB + body segments (no UNT/UNZ yet)
        # UNH is segments[1], UNT will be added next
        # Count = len(segments) - 1 (exclude UNB) + 1 (UNT itself)
        seg_count_for_unt = len(segments)  # excludes UNB, will include UNT
        segments.append('UNT+{n}+1'.format(n=seg_count_for_unt))

        # Interchange trailer
        segments.append('UNZ+1+{ref}'.format(ref=_edifact_escape(despatch['ctrl_ref'])))

        return _SEG_TERM.join(segments) + _SEG_TERM

    def _validate(self, despatch: dict) -> None:
        """Validate all EAN-13 barcodes in the despatch."""
        for delivery in despatch.get('deliveries', []):
            for line in delivery.get('lines', []):
                ean = str(line.get('ean13', ''))
                if not _ean13_valid(ean):
                    raise ValueError(
                        "Invalid EAN-13 barcode in despatch line (seq=%s): '%s'. "
                        "Check the product barcode before generating ASN." % (line.get('seq'), ean)
                    )
