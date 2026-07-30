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

# --- Counterparty GLN --------------------------------------------------------
#
# WIRE-PROTOCOL ROUTING DATA — NOT fixture content, NOT a brand label.
#
# The buyer's GS1 Global Location Number is written verbatim into the live
# outbound DESADV envelope (UNB S003) and the NAD+BY segment below, so it is
# ACCOUNT-SPECIFIC data: it identifies one deployment's counterparty, never the
# product. It therefore carries NO built-in default — a hardcoded GLN here
# would put one customer's counterparty on every other customer's wire, and a
# *fictionalised* one is worse than an invalid one (a syntactically valid but
# wrong GLN is silently misrouted or rejected by the partner rather than caught
# locally).
#
# The value is supplied per call as ``despatch['buyer_gln']`` by the Odoo
# adapter (services/edi_service.py), which reads ir.config_parameter
# ``mml_edi.kestrelby_buyer_gln``. Generation fails closed when it is missing —
# see ``_validate``. Registered in scripts/cadence_transform/rename_map.yaml as
# `config_extract: kestrelby_buyer_gln`.
DEFAULT_BUYER_GLN = ''
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
        van_sender_id  str   REQUIRED — our own sender identity on the VAN
                             (ir.config_parameter ``mml_edi.sender_id``)
        ctrl_ref       str   Interchange control reference (unique per interchange)
        buyer_gln      str   REQUIRED — the counterparty's GLN
                             (ir.config_parameter ``mml_edi.kestrelby_buyer_gln``).
                             No built-in default: see DEFAULT_BUYER_GLN above.
        deliveries     list  [{'store_gln': str, 'lines': [{'ean13': str, 'qty': int, 'seq': int}]}]
    """

    def generate(self, despatch: dict) -> str:
        """Generate and return the full EDIFACT DESADV message as a string."""
        self._validate(despatch)
        buyer_gln = despatch.get('buyer_gln') or DEFAULT_BUYER_GLN

        now = datetime.now(timezone.utc)
        date_yymmdd = now.strftime('%y%m%d')
        time_hhmm = now.strftime('%H%M')

        segments = []

        # Interchange header
        segments.append(
            'UNB+UNOA:3+{mml}:14+{kestrelby}:14+{date}:{time}+{ref}++DESADV'.format(
                mml=_edifact_escape(despatch['van_sender_id']),
                kestrelby=buyer_gln,
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
        segments.append('NAD+SE+{mml}::14'.format(mml=_edifact_escape(despatch['van_sender_id'])))

        # Buyer (Kestrelby Group)
        segments.append('NAD+BY+{gln}::14'.format(gln=buyer_gln))

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
        """Validate the routing identities and every EAN-13 barcode.

        Fail-closed on the two account-specific routing identities. Neither has
        a built-in default any more (they used to be hardcoded to one
        deployment's values), so an unconfigured install must be stopped HERE
        rather than emitting an interchange addressed to an empty mailbox —
        which a VAN accepts syntactically and then black-holes.
        """
        if not str(despatch.get('van_sender_id') or '').strip():
            raise ValueError(
                "DESADV: van_sender_id is required and not configured. Set the "
                "ir.config_parameter 'mml_edi.sender_id' to the sender identity "
                "the VAN provisioned for this deployment."
            )
        if not str(despatch.get('buyer_gln') or '').strip():
            raise ValueError(
                "DESADV: buyer_gln is required and not configured. Set the "
                "ir.config_parameter 'mml_edi.kestrelby_buyer_gln' to the "
                "counterparty GLN, or pass 'buyer_gln' in the despatch dict."
            )
        for delivery in despatch.get('deliveries', []):
            for line in delivery.get('lines', []):
                ean = str(line.get('ean13', ''))
                if not _ean13_valid(ean):
                    raise ValueError(
                        "Invalid EAN-13 barcode in despatch line (seq=%s): '%s'. "
                        "Check the product barcode before generating ASN." % (line.get('seq'), ean)
                    )
