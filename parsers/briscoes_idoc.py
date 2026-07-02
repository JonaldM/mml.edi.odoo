# mml.edi/parsers/briscoes_idoc.py
"""
Briscoes EDI Parser — SAP iDOC XML (ORDERS05 / CIMTYP ORDERSEXT).

This is the format Briscoes actually sends MML over the EDIS VAN (NOT EDIFACT —
see briscoes.py for the EDIFACT D96A variant). Parses ORDERS (new) and ORDCHG
(change) inbound iDOCs and generates ORDRSP (order response / ACK) iDOCs.

Ported from the proven .NET `BriscoesEDI.iDocHandler` (GenerateEDIOrders /
acknowledgement builder) and validated against real production sample files.

iDOC structure (one <IDOC> per order document):
  EDI_DC40   interchange control — DOCNUM, MESTYP (ORDERS/ORDCHG/ORDRSP),
             SNDPOR (SAPBRP=live / SAPBRP_TEST=test), SERIAL, CREDAT/CRETIM
  E1EDK01    header — BSART (ZNR single / ZNB bulk multi-store / ZDSP dropship),
             BELNR (PO number), KZABS (X = ACK requested), CURCY, ZTERM
  E1EDK02    reference docs (QUALF 001 = PO ref)
  E1EDK03    dates — IDDAT 012 = order date, 011 = header delivery date
  E1EDKA1    header partners — PARVW AG=buyer, LF=vendor, WE=ship-to (single store)
  E1EDP01    line items, repeating:
               POSEX   line position (e.g. 00010)   -> line_number
               ACTION  001=add 002=change 003=delete (ORDCHG only)
               MENGE / MENEE   carton count + UoM (CT)
               BMNG2 / PMENE   EACH count + UoM (EA)  -> quantity  ** each, not cartons **
               VPREI   per-each price               -> unit_price
               NETWR   line net value (= BMNG2 x VPREI)
               WERKS   plant/store
               E1EDPA1 per-line ship-to (PARVW WE, LIFNR=store) — multi-store
               E1EDP20 EDATU = requested delivery date for this line
               E1EDP19 product ids: QUALF 003 = CASE/SHIPPER GTIN-14 (carton
                                    barcode, NOT the retail EAN-13),
                                    002 = MML internal code, 001 = Briscoes buyer
                                    article no (+ KTEXT description)
  E1EDS01    summary (SUMME = order total)

Store handling: one ParsedOrder per ship-to store. Store code is taken from the
line's E1EDPA1[WE].LIFNR (multi-store), else the header E1EDKA1[WE].LIFNR
(single store), else WERKS. A single-store ZNR collapses to one ParsedOrder.

Product matching: product_code = the MML internal code (QUALF 002), matched to
Odoo's product.default_code (case-insensitively) — the proven .NET match. The
QUALF 003 value is the CASE/SHIPPER GTIN-14 (Briscoes uses separate shipper vs
retail barcodes); it is NOT Odoo's retail product.barcode and does not reliably
reduce to it, so it is not used for matching. vendor_code repeats the MML code and
buyer_article_no carries the Briscoes article (QUALF 001) so the processor's match
cascade can also resolve via default_code / supplier_sku for differently
configured partners.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Optional

from .base_parser import BaseEDIParser, EDIParseError, ParsedOrder, ParsedOrderLine

_logger = logging.getLogger(__name__)

# ── iDOC constants ───────────────────────────────────────────────────────────
_MESTYP_NEW = "ORDERS"
_MESTYP_CHANGE = "ORDCHG"
_MESTYP_RESPONSE = "ORDRSP"

_ACTION_ADD = "001"
_ACTION_CHANGE = "002"
_ACTION_DELETE = "003"

# ABGRU (reason-for-rejection) code used on ORDRSP lines that are short-supplied
# or rejected. "11" = "cannot supply" per the Briscoes implementation guide / the
# legacy .NET handler.
_ABGRU_UNAVAILABLE = "11"


# ── small XML helpers ────────────────────────────────────────────────────────

def _text(elem: Optional[ET.Element], tag: str, default: str = "") -> str:
    """Return stripped text of child <tag> of elem, or default."""
    if elem is None:
        return default
    child = elem.find(tag)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def _to_float(value: str) -> float:
    try:
        return float((value or "0").strip())
    except (ValueError, AttributeError):
        return 0.0


def _parse_idoc_date(value: str) -> Optional[date]:
    """Parse an 8-char yyyyMMdd iDOC date, else None."""
    value = (value or "").strip()
    if len(value) == 8:
        try:
            return datetime.strptime(value, "%Y%m%d").date()
        except ValueError:
            return None
    return None


def _parse_posex(value: str) -> int:
    """'00010' -> 10. Returns 0 on failure."""
    value = (value or "").strip()
    try:
        return int(value)
    except ValueError:
        return 0


def _we_lifnr(parent: ET.Element, segment_tag: str) -> Optional[str]:
    """Return LIFNR (or PARTN) of the PARVW='WE' partner segment under parent."""
    for seg in parent.findall(segment_tag):
        if _text(seg, "PARVW") == "WE":
            return _text(seg, "LIFNR") or _text(seg, "PARTN") or None
    return None


# ── Parser ───────────────────────────────────────────────────────────────────

class BriscoesIDOCParser(BaseEDIParser):
    """Parser for Briscoes SAP iDOC ORDERSEXT purchase orders + ORDRSP ACKs."""

    # ── inbound: parse ───────────────────────────────────────────────────────

    def parse_file(self, raw_content: bytes, trading_partner) -> list:
        """Parse raw iDOC XML bytes into a list of ParsedOrder (one per store)."""
        raw_text = (
            raw_content.decode("utf-8", errors="replace")
            if isinstance(raw_content, (bytes, bytearray))
            else raw_content
        )
        try:
            root = ET.fromstring(raw_text)
        except ET.ParseError as exc:
            raise EDIParseError("Invalid iDOC XML: %s" % exc)

        orders: list = []
        # The root is normally <ORDERSEXT> containing one or more <IDOC>.
        idocs = root.findall("IDOC") or ([root] if root.tag == "IDOC" else [])
        for idoc in idocs:
            orders.extend(self._parse_idoc(idoc, raw_text))

        _logger.info(
            "[BriscoesIDOC] Parsed %d store order(s), %d total lines from iDOC",
            len(orders), sum(len(o.lines) for o in orders),
        )
        return orders

    def _parse_idoc(self, idoc: ET.Element, raw_text: str) -> list:
        dc40 = idoc.find("EDI_DC40")
        mestyp = _text(dc40, "MESTYP").upper()

        if mestyp == _MESTYP_RESPONSE:
            _logger.warning("[BriscoesIDOC] ORDRSP (response) iDOC received inbound — skipping")
            return []
        document_type = "change_order" if mestyp == _MESTYP_CHANGE else "new_order"

        k01 = idoc.find("E1EDK01")
        po_number = _text(k01, "BELNR")
        if not po_number:
            raise EDIParseError("iDOC missing PO number (E1EDK01/BELNR)")

        # Dates: IDDAT 012 = order date, 011 = header delivery date.
        order_date: Optional[date] = None
        header_due: Optional[date] = None
        for k03 in idoc.findall("E1EDK03"):
            iddat = _text(k03, "IDDAT")
            if iddat == "012":
                order_date = _parse_idoc_date(_text(k03, "DATUM"))
            elif iddat == "011":
                header_due = _parse_idoc_date(_text(k03, "DATUM"))

        header_store = _we_lifnr(idoc, "E1EDKA1")

        # Group lines by ship-to store, preserving first-seen order.
        store_lines: dict = {}
        store_due: dict = {}
        store_seq: list = []

        for p01 in idoc.findall("E1EDP01"):
            store = (
                _we_lifnr(p01, "E1EDPA1")
                or header_store
                or (_text(p01, "WERKS") or None)
            )
            # Register the store up-front — even for a deleted line — so that a
            # fully-cancelled store (every line ACTION=003) still yields a
            # line-less ParsedOrder the engine can act on (its change-order diff
            # then sees all existing lines as removed → cancellation).
            if store not in store_lines:
                store_lines[store] = []
                store_seq.append(store)

            action = _text(p01, "ACTION")
            # In a change order, a deleted line (ACTION 003) is omitted from the
            # line list so the processor's diff treats it as removed (matches .NET).
            if document_type == "change_order" and action == _ACTION_DELETE:
                continue

            p20 = p01.find("E1EDP20")
            line_due = (
                _parse_idoc_date(_text(p20, "EDATU")) if p20 is not None else None
            ) or header_due

            carton_gtin = mml_code = buyer_article = ""
            description = ""
            for p19 in p01.findall("E1EDP19"):
                qualf = _text(p19, "QUALF")
                idtnr = _text(p19, "IDTNR")
                if qualf == "003":
                    carton_gtin = idtnr          # CASE/shipper GTIN-14 (not retail)
                elif qualf == "002":
                    mml_code = idtnr             # MML internal code -> Odoo default_code
                elif qualf == "001":
                    buyer_article = idtnr.lstrip("0")
                    description = _text(p19, "KTEXT")

            # Product match key = the MML internal code (QUALF 002 -> default_code),
            # the proven .NET match. QUALF 003 is the CASE/SHIPPER GTIN-14, NOT the
            # retail EAN-13 Odoo stores on product.barcode, and it does not reliably
            # reduce to it (Briscoes assigns some carton barcodes off an independent
            # base — verified on real data), so it must not be used as the match key.
            # vendor_code repeats the MML code so the processor's cascade still
            # resolves the product if a partner is configured to match on barcode.
            menge = _to_float(_text(p01, "MENGE"))   # carton count
            line = ParsedOrderLine(
                product_code=mml_code or carton_gtin,
                description=description,
                quantity=_to_float(_text(p01, "BMNG2")),   # EACH count
                unit_price=_to_float(_text(p01, "VPREI")),
                line_number=_parse_posex(_text(p01, "POSEX")),
                uom=_text(p01, "PMENE") or "EA",
                carton_qty=menge or None,
                buyer_article_no=buyer_article or None,
                vendor_code=mml_code or None,          # cascade -> default_code
            )

            store_lines[store].append(line)
            if store not in store_due and line_due:
                store_due[store] = line_due

        orders = []
        for store in store_seq:
            orders.append(ParsedOrder(
                po_number=po_number,
                order_date=order_date,
                store_code=store,
                requested_delivery_date=store_due.get(store) or header_due,
                lines=store_lines[store],
                document_type=document_type,
                raw_data=raw_text,
            ))
        return orders

    # ── outbound: ORDRSP / ACK ───────────────────────────────────────────────

    def generate_ack(self, review_record) -> bytes:
        """
        Generate an iDOC ORDRSP (order response) for a processed review.

        Strategy mirrors the proven .NET handler: echo the stored inbound PO iDOC
        back, transformed into an ORDRSP, so POSEX and every SAP field line up
        exactly (POSEX mismatch is the known cause of EDIS rejections). Lines are
        filtered to this review's store; confirmed quantities come from the linked
        sale order (so short-supply is reflected), and rejected reviews flag every
        line with ABGRU.
        """
        raw = getattr(review_record, "edi_raw_data", None)
        if not raw:
            raise EDIParseError(
                "Cannot generate iDOC ORDRSP: review has no stored raw PO data"
            )
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise EDIParseError("Stored raw PO is not valid iDOC XML: %s" % exc)

        po_ref = getattr(review_record, "customer_po_number", "") or ""

        # Per-PO ACK: Briscoes expects ONE ORDRSP covering every store/line of the
        # PO (verified against real production ACKs — e.g. PO 4500178971's ORDRSP
        # carried 1173 lines across 47 stores). Confirmations are therefore
        # aggregated across ALL of this PO's store-reviews, and every line is
        # echoed (no per-store filter).
        confirmed, has_review_data = self._gather_confirmations(review_record)

        now = datetime.now()
        serial = now.strftime("%Y%m%d%H%M%S")

        out = ['<?xml version="1.0" encoding="UTF-8"?>', "<ORDERSEXT>"]
        idocs = root.findall("IDOC") or ([root] if root.tag == "IDOC" else [])
        # Multi-PO safety: EDIS can batch several POs into one inbound file, and
        # edi_raw_data stores the WHOLE file. Echoing every IDOC would emit the
        # OTHER PO's lines into this ACK carrying THIS PO's confirmations (wrong
        # quantities / force-rejected segments). When the file holds more than
        # one distinct PO, keep only the IDOC(s) whose E1EDK01/BELNR matches this
        # review's PO. Single-PO files (the common case) are deliberately left
        # untouched — output stays byte-for-byte identical.
        belnrs = [_text(i.find("E1EDK01"), "BELNR") for i in idocs]
        if len(set(belnrs)) > 1:
            matched = [i for i, b in zip(idocs, belnrs) if b == po_ref]
            if not matched:
                # Fail CLOSED: an ORDRSP built from another PO's iDOC would
                # confirm/reject lines Briscoes never ordered on this PO.
                raise EDIParseError(
                    "Stored raw file holds POs %s but none match review PO %r; "
                    "refusing to generate a cross-PO ORDRSP"
                    % (sorted(set(belnrs)), po_ref)
                )
            idocs = matched
        for idoc in idocs:
            out.extend(self._build_ordrsp(
                idoc, po_ref, confirmed, now, serial, has_review_data,
            ))
        out.append("</ORDERSEXT>")
        return ("\n".join(out) + "\n").encode("utf-8")

    def _gather_confirmations(self, review_record):
        """
        Collect confirmed each-quantities per POSEX across ALL store-reviews of
        this PO (per-PO ACK). Returns a tuple
        ``(confirmed, has_review_data)`` where ``confirmed`` is
        {posex:int -> (confirmed_each:float, rejected:bool)} and
        ``has_review_data`` is True when the PO's reviews were resolved against a
        live Odoo env (the authoritative production path).

        ``has_review_data`` governs the default for a POSEX that has NO
        confirmation row (see _build_ordrsp): with review data present a missing
        POSEX is a line MML never processed → it is rejected (0 + ABGRU) rather
        than silently confirmed in full. It is False only on the pure unit-test /
        mock path (no Odoo env), where echoing the ordered quantity is intended.
        """
        try:
            reviews = list(review_record.env["edi.order.review"].search([
                ("customer_po_number", "=", review_record.customer_po_number),
                ("trading_partner_id", "=", review_record.trading_partner_id.id),
            ])) or [review_record]
            has_review_data = True
        except (AttributeError, TypeError):
            # No Odoo env on the record (pure unit-test / mock path): there is no
            # ORM to query, so review data legitimately cannot exist. Echoing the
            # ordered quantity as full supply is the intended test behaviour.
            reviews = [review_record]
            has_review_data = False
        except Exception as exc:
            # An UNEXPECTED error reading real review data (ORM/DB hiccup, access
            # error, registry failure, ...). Do NOT silently degrade into
            # echo-all — that would tell Briscoes we will fully supply lines we
            # may have rejected or short-supplied. Fail ACK generation so it is
            # retried rather than fabricating a full-supply ORDRSP.
            po = getattr(review_record, "customer_po_number", "") or ""
            try:
                review_record.env["edi.log"].log(
                    getattr(review_record, "trading_partner_id", None),
                    "outbound",
                    "ack_generate",
                    "error",
                    "Failed reading order-review data for ORDRSP (PO %s); "
                    "aborting ACK to avoid a false full-supply confirmation" % po,
                    review=review_record,
                    detail=repr(exc),
                )
            except Exception:
                # edi.log itself is unavailable — fall back to the Python logger
                # but still raise (never swallow the error into echo-all).
                _logger.critical(
                    "[EDI] ORDRSP ACK aborted: failed reading review data for "
                    "PO %s: %r", po, exc,
                )
            raise

        confirmed: dict = {}
        for rev in reviews:
            rejected = getattr(rev, "state", "") == "rejected"
            so = getattr(rev, "sale_order_id", None)
            order_lines = (getattr(so, "order_line", []) or []) if so is not None else []
            for sol in order_lines:
                ln = getattr(sol, "edi_line_number", None)
                if ln:
                    confirmed[int(ln)] = (
                        float(getattr(sol, "product_uom_qty", 0) or 0),
                        rejected,
                    )
        return confirmed, has_review_data

    def _build_ordrsp(self, idoc, po_ref, confirmed, now, serial, has_review_data=False):
        dc40 = idoc.find("EDI_DC40")
        k01 = idoc.find("E1EDK01")
        docnum = _text(dc40, "DOCNUM")
        sndpor = _text(dc40, "SNDPOR") or "SAPBRP"
        sndprn = _text(dc40, "SNDPRN")
        belnr = _text(k01, "BELNR")

        seg = []
        # EDI_DC40 -> ORDRSP (DIRECT=2 outbound)
        seg.append('<IDOC BEGIN="1">')
        seg.append('<EDI_DC40 SEGMENT="1">')
        seg.append("<TABNAM>EDI_DC40</TABNAM>")
        seg.append("<MANDT>300</MANDT>")
        seg.append("<DOCNUM>%s</DOCNUM>" % docnum)
        seg.append("<DOCREL>701</DOCREL>")
        seg.append("<STATUS>53</STATUS>")
        seg.append("<DIRECT>2</DIRECT>")
        seg.append("<IDOCTYP>ORDERS05</IDOCTYP>")
        seg.append("<MESTYP>ORDRSP</MESTYP>")
        seg.append("<STD>E</STD>")
        seg.append("<STDMES>ORDRSP</STDMES>")
        seg.append("<SNDPOR>%s</SNDPOR>" % sndpor)
        seg.append("<SNDPRT>LS</SNDPRT>")
        seg.append("<SNDPRN>%s</SNDPRN>" % sndprn)
        seg.append("<RCVPOR>%s</RCVPOR>" % sndpor)
        seg.append("<RCVPRT>LS</RCVPRT>")
        seg.append("<RCVPRN>%s</RCVPRN>" % sndprn)
        seg.append("<CREDAT>%s</CREDAT>" % _text(dc40, "CREDAT"))
        seg.append("<CRETIM>%s</CRETIM>" % _text(dc40, "CRETIM"))
        seg.append("<ARCKEY>%s</ARCKEY>" % docnum)
        seg.append("<SERIAL>%s</SERIAL>" % serial)
        seg.append("</EDI_DC40>")

        # E1EDK01 (+ ACTION)
        seg.append('<E1EDK01 SEGMENT="1">')
        seg.append("<ACTION>001</ACTION>")
        seg.append("<KZABS>%s</KZABS>" % _text(k01, "KZABS"))
        seg.append("<CURCY>%s</CURCY>" % (_text(k01, "CURCY") or "NZD"))
        seg.append("<HWAER>%s</HWAER>" % (_text(k01, "HWAER") or "NZD"))
        seg.append("<WKURS>1</WKURS>")
        seg.append("<ZTERM>%s</ZTERM>" % _text(k01, "ZTERM"))
        seg.append("<BSART>%s</BSART>" % _text(k01, "BSART"))
        seg.append("<BELNR>%s</BELNR>" % belnr)
        seg.append("<RECIPNT_NO>%s</RECIPNT_NO>" % _text(k01, "RECIPNT_NO"))
        seg.append("</E1EDK01>")

        # E1EDK02 — echo PO ref (QUALF 001) then add response ref (QUALF 002)
        seg.append('<E1EDK02 SEGMENT="1">')
        seg.append("<QUALF>001</QUALF>")
        seg.append("<BELNR>%s</BELNR>" % (belnr or po_ref))
        seg.append("<DATUM>%s</DATUM>" % now.strftime("%Y%m%d"))
        seg.append("</E1EDK02>")
        seg.append('<E1EDK02 SEGMENT="1">')
        seg.append("<QUALF>002</QUALF>")
        seg.append("<BELNR>%s</BELNR>" % serial)
        seg.append("<DATUM>%s</DATUM>" % now.strftime("%Y%m%d"))
        seg.append("<UZEIT>%s</UZEIT>" % now.strftime("%H%M%S"))
        seg.append("</E1EDK02>")

        # Lines — ALL stores echoed (per-PO ACK)
        line_count = 0
        for p01 in idoc.findall("E1EDP01"):
            posex = _text(p01, "POSEX")
            posex_int = _parse_posex(posex)
            vprei = _to_float(_text(p01, "VPREI"))
            ordered_each = _to_float(_text(p01, "BMNG2"))
            ordered_cartons = _to_float(_text(p01, "MENGE"))

            conf = confirmed.get(posex_int)
            if conf is None:
                if has_review_data:
                    # The PO was reviewed but this POSEX produced no confirmed
                    # line (e.g. its store failed mid-file, or the line was never
                    # processed). Do NOT promise full supply — reject it so we
                    # never tell Briscoes we will ship a line we never handled.
                    confirmed_each, rej_line = 0.0, True
                else:
                    # No review data at all (pure mock / no Odoo env): echo the
                    # ordered quantity as full supply.
                    confirmed_each, rej_line = ordered_each, False
            else:
                confirmed_each, rej_line = conf
            if rej_line:
                confirmed_each = 0.0
            short = confirmed_each < ordered_each

            # Scale cartons + value to the confirmed quantity.
            if ordered_each > 0:
                conf_cartons = ordered_cartons * (confirmed_each / ordered_each)
            else:
                conf_cartons = ordered_cartons
            netwr = round(confirmed_each * vprei, 2)

            seg.append('<E1EDP01 SEGMENT="1">')
            seg.append("<POSEX>%s</POSEX>" % posex)
            seg.append("<ACTION>001</ACTION>")
            seg.append("<PSTYP>%s</PSTYP>" % (_text(p01, "PSTYP") or "0"))
            seg.append("<KZABS>%s</KZABS>" % _text(p01, "KZABS"))
            seg.append("<MENGE>%.3f</MENGE>" % conf_cartons)
            seg.append("<MENEE>%s</MENEE>" % (_text(p01, "MENEE") or "CT"))
            seg.append("<BMNG2>%.3f</BMNG2>" % confirmed_each)
            seg.append("<PMENE>%s</PMENE>" % (_text(p01, "PMENE") or "EA"))
            seg.append("<VPREI>%s</VPREI>" % _text(p01, "VPREI"))
            seg.append("<PEINH>%s</PEINH>" % (_text(p01, "PEINH") or "1"))
            seg.append("<NETWR>%.2f</NETWR>" % netwr)
            seg.append("<GEWEI>%s</GEWEI>" % _text(p01, "GEWEI"))
            seg.append("<MATKL>%s</MATKL>" % _text(p01, "MATKL"))
            seg.append("<BPUMN>%s</BPUMN>" % (_text(p01, "BPUMN") or "1"))
            seg.append("<BPUMZ>%s</BPUMZ>" % (_text(p01, "BPUMZ") or "1"))
            seg.append("<WERKS>%s</WERKS>" % _text(p01, "WERKS"))
            seg.append("<LGORT>%s</LGORT>" % (_text(p01, "LGORT") or "0001"))
            # Reason-for-rejection flag on short / rejected lines.
            if rej_line or short:
                seg.append("<ABGRU>%s</ABGRU>" % _ABGRU_UNAVAILABLE)
            # E1EDP02 — link response line back to PO line (ZEILE = POSEX)
            seg.append('<E1EDP02 SEGMENT="1">')
            seg.append("<QUALF>001</QUALF>")
            seg.append("<BELNR>%s</BELNR>" % belnr)
            seg.append("<ZEILE>%s</ZEILE>" % posex)
            seg.append("</E1EDP02>")
            # E1EDP20 — schedule
            p20 = p01.find("E1EDP20")
            seg.append('<E1EDP20 SEGMENT="1">')
            # WMENG is the confirmed quantity in the ORDER unit (MENEE = CT /
            # cartons), NOT the EA base qty. The proven .NET ACK and every
            # Briscoes-accepted ORDRSP set WMENG == MENGE (cartons); emitting the
            # EA value (BMNG2) here over-confirms every line by the carton factor.
            seg.append("<WMENG>%.3f</WMENG>" % conf_cartons)
            seg.append("<AMENG>0.000</AMENG>")
            seg.append("<EDATU>%s</EDATU>" % (_text(p20, "EDATU") if p20 is not None else ""))
            seg.append("</E1EDP20>")
            # E1EDP19 — echo product ids 001 (article) and 003 (GTIN) only
            for p19 in p01.findall("E1EDP19"):
                qualf = _text(p19, "QUALF")
                if qualf in ("001", "003"):
                    seg.append('<E1EDP19 SEGMENT="1">')
                    seg.append("<QUALF>%s</QUALF>" % qualf)
                    seg.append("<IDTNR>%s</IDTNR>" % _text(p19, "IDTNR"))
                    seg.append("</E1EDP19>")
            seg.append("</E1EDP01>")
            line_count += 1

        # E1EDS01 order-value summary — echo the PO's summary segment(s). The
        # proven .NET ACK (and every Briscoes-accepted ORDRSP) carries
        # SUMID=002 / SUMME=<order value> / SUNIT=<currency>; omitting it
        # diverges from the format Briscoes expects.
        for s01 in idoc.findall("E1EDS01"):
            seg.append('<E1EDS01 SEGMENT="1">')
            sumid = _text(s01, "SUMID")
            if sumid:
                seg.append("<SUMID>%s</SUMID>" % sumid)
            seg.append("<SUMME>%s</SUMME>" % _text(s01, "SUMME"))
            sunit = _text(s01, "SUNIT")
            if sunit:
                seg.append("<SUNIT>%s</SUNIT>" % sunit)
            seg.append("</E1EDS01>")

        seg.append("</IDOC>")
        return seg
