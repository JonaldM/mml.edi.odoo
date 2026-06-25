"""Animates (NZ) EDI parser — UN/EDIFACT D.01B over SPS Commerce.

Inbound: ORDERS -> ParsedOrder (becomes a sale.order in Odoo).
Outbound: generate_ack -> ORDRSP (built in B3, see animates_ordrsp.py).

Item identity (per the MIG + octo review):
  PIA+5+<isc>:IN   = Animates' item code (ISC)  -> buyer_article_no
  PIA+1+<code>:SA  = MML's article number       -> product_code + vendor_code
The processor cascade (_find_product) then matches product_code/vendor_code
(default_code) and falls back to buyer_article_no (supplier_sku / supplierinfo),
so a product keyed by MML's code OR the Animates ISC resolves either way.
"""
from datetime import date

from .base_parser import BaseEDIParser, ParsedOrder, ParsedOrderLine, EDIParseError
from . import animates_edifact as edifact

# BGM 1225 message function code -> our document_type
_BGM_NEW = {"9"}            # 9 = original
_BGM_CHANGE = {"4", "5"}    # 4 = change, 5 = replace
_BGM_CANCEL = {"1"}         # 1 = cancellation (message carries no order lines)


def _parse_dtm(seg):
    """DTM+<qual>:<YYYYMMDD>:<102> -> (qualifier, date|None)."""
    qual = seg.comp(0, 0)
    val = seg.comp(0, 1)
    d = None
    if val and len(val) == 8 and val.isdigit():
        d = date(int(val[0:4]), int(val[4:6]), int(val[6:8]))
    return qual, d


class AnimatesParser(BaseEDIParser):

    def parse_file(self, raw_content, trading_partner) -> list:
        text = raw_content.decode("iso-8859-1") if isinstance(raw_content, (bytes, bytearray)) else raw_content
        _, segments = edifact.tokenize(text)
        if not any(s.tag == "UNH" for s in segments):
            raise EDIParseError("No UNH segment — not a valid EDIFACT interchange")

        orders = []
        cur = None     # dict accumulating the current message
        line = None    # dict accumulating the current LIN group

        def _flush_line():
            nonlocal line
            if line is not None and cur is not None:
                cur["lines"].append(line)
            line = None

        def _flush_order():
            nonlocal cur
            _flush_line()
            if cur is not None and cur.get("po_number"):
                orders.append(cur)
            cur = None

        for seg in segments:
            t = seg.tag
            if t == "UNH":
                _flush_order()
                cur = {"po_number": None, "order_date": None, "delivery_date": None,
                       "store_code": None, "doc_type": "new_order", "lines": []}
            elif cur is None:
                continue
            elif t == "BGM":
                cur["po_number"] = seg.comp(1, 0)
                func = seg.comp(2, 0)
                if func in _BGM_CHANGE:
                    cur["doc_type"] = "change_order"
                elif func in _BGM_CANCEL:
                    cur["doc_type"] = "change_order"
                    cur["cancelled"] = True
                else:
                    cur["doc_type"] = "new_order"
            elif t == "DTM":
                qual, d = _parse_dtm(seg)
                if qual == "137":
                    cur["order_date"] = d
                elif qual in ("2", "63", "64"):  # requested/delivery date
                    cur["delivery_date"] = d
            elif t == "NAD" and seg.comp(0, 0) == "ST":
                cur["store_code"] = seg.comp(1, 0)
            elif t == "LIN":
                _flush_line()
                line = {"line_number": int(seg.comp(0, 0) or 0), "product_code": None,
                        "buyer_article_no": None, "vendor_code": None, "description": "",
                        "quantity": 0.0, "uom": None, "carton_qty": None, "unit_price": 0.0}
            elif t == "PIA" and line is not None:
                code = seg.comp(1, 0)
                kind = seg.comp(1, 1)
                if kind == "IN":          # Animates' item code (ISC)
                    line["buyer_article_no"] = code
                elif kind == "SA":        # MML's article number
                    line["product_code"] = code
                    line["vendor_code"] = code
            elif t == "IMD" and line is not None:
                # IMD+F++:::Description -> description is the last component of C273
                comps = seg.elements[2] if len(seg.elements) > 2 else []
                line["description"] = next((c for c in reversed(comps) if c), "")
            elif t == "QTY" and line is not None:
                q = seg.comp(0, 0)
                val = seg.comp(0, 1)
                fval = float(val) if val else 0.0
                if q == "21":             # ordered quantity
                    line["quantity"] = fval
                    line["uom"] = seg.comp(0, 2) or None
                elif q == "59":           # number of consumer units per pack
                    line["carton_qty"] = fval
            elif t == "PRI" and line is not None:
                if seg.comp(0, 0) in ("AAA", "AAB", "AAE", ""):
                    v = seg.comp(0, 1)
                    if v:
                        line["unit_price"] = float(v)
            elif t == "UNT":
                _flush_order()
        _flush_order()

        result = []
        for o in orders:
            lines = [ParsedOrderLine(
                product_code=ln["product_code"] or ln["buyer_article_no"] or "",
                description=ln["description"],
                quantity=ln["quantity"],
                unit_price=ln["unit_price"],
                line_number=ln["line_number"],
                uom=ln["uom"],
                carton_qty=ln["carton_qty"],
                buyer_article_no=ln["buyer_article_no"],
                vendor_code=ln["vendor_code"],
            ) for ln in o["lines"]]
            result.append(ParsedOrder(
                po_number=o["po_number"],
                order_date=o["order_date"] or date.today(),
                lines=lines,
                store_code=o["store_code"],
                requested_delivery_date=o["delivery_date"],
                delivery_address_code=o["store_code"],
                document_type=o["doc_type"],
            ))
        return result

    def generate_ack(self, review_record) -> bytes:
        """ORDRSP (D.01B) — implemented in B3 via animates_ordrsp.py."""
        from .animates_ordrsp import build_ordrsp
        return build_ordrsp(review_record)
