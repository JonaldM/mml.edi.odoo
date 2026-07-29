# mml.edi/tests/test_nimbrel_ordrsp_live_odoo.py
"""Odoo TransactionCase tests for NimbrelParser.generate_ack / _review_to_
ordrsp_payload against real ORM records (Wave1-C, go-live gate review AN-01,
SS-6, sibling aggregation, scenario 4A price/qty correction, BGM selection).

Complements tests/test_nimbrel_ordrsp_live.py (pure, duck-typed
SimpleNamespace review/SOL). These exercise the SAME logic through real
sale.order.line fields (edi_price/edi_qty_shortfall/edi_ordered_qty compute +
store behaviour) and real edi.trading.partner identity (get_unb_sender/
get_unb_recipient — C1) and edi.order.review sibling search (finding #11),
none of which a duck-typed namespace can prove.

Requires a live Odoo DB (odoo-bin --test-enable)."""
import unittest

from odoo.tests.common import TransactionCase, tagged

from .common import EDITestSetup

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")

_NIMBREL_ORDERS_TWO_LINES = (
    "UNA:+.? '"
    "UNB+UNOC:3+NIMBREL:ZZZ+SUPPLIER_GLN:14+260703:0900+55001++++1'"
    "UNH+1+ORDERS:D:01B:UN:EAN011'"
    "BGM+220+PO-LIVE-001+9'"
    "DTM+137:20260703:102'"
    "DTM+2:20260710:102'"
    "NAD+BY+NIMBREL::92++Nimbrel NZ Holding LTD'"
    "NAD+SU+V1058::92++M&M Pty Ltd'"
    "NAD+ST+12345::92++Nimbrel Invercargill'"
    "LIN+1'"
    "PIA+5+ISC001:IN'"
    "PIA+1+9780000000002:SA'"
    "IMD+F++:::Test Product'"
    "QTY+21:2:EA'"
    "QTY+59:1'"
    "PRI+AAA:9.99'"
    "UNS+S'"
    "CNT+2:1'"
    "UNT+16+1'"
    "UNZ+1+55001'"
)


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")
class _NimbrelOrdrspLiveOdooBase(EDITestSetup, TransactionCase):
    """Shared setup: an Nimbrel (EDIFACT D.01B) trading partner with real C1
    identity fields set, one product matching the fixture's barcode/PIA+1."""

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.trading_partner.write({
            "edi_format": "edifact_d01b",
            "parser_class": "mml_edi.parsers.nimbrel.NimbrelParser",
            "order_split_mode": "single",
            "edi_sender_id": "0200000000004T",
            "edi_sender_qualifier": "ZZZ",
            "supplier_gln": "0200000000004",
            "nimbrel_vendor_code": "V1058",
            "environment": "test",
        })
        self.test_product.barcode = "9780000000002"

        from odoo.addons.mml_edi.parsers.nimbrel import NimbrelParser
        self.parser = NimbrelParser()

    def _make_so(self, po="PO-LIVE-001", ref_suffix="A"):
        return self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "edi_trading_partner_id": self.trading_partner.id,
            "client_order_ref": "%s_%s" % (po, ref_suffix),
        })

    def _make_sol(self, so, **overrides):
        vals = dict(
            order_id=so.id,
            product_id=self.test_product.id,
            product_uom_qty=2.0,
            price_unit=9.99,
            edi_line_number=1,
            edi_ordered_qty=2.0,
            edi_qty_shortfall=0.0,
            edi_price=9.99,
        )
        vals.update(overrides)
        return self.env["sale.order.line"].create(vals)

    def _make_review(self, so, po="PO-LIVE-001", file_hash="livehash0000001",
                      state="auto_approved", store_code="12345"):
        return self.env["edi.order.review"].create({
            "trading_partner_id": self.trading_partner.id,
            "customer_po_number": po,
            "store_code": store_code,
            "sale_order_id": so.id,
            "edi_file_hash": file_hash,
            "edi_raw_data": _NIMBREL_ORDERS_TWO_LINES,
            "document_type": "new_order",
            "state": state,
        })

    def _segs(self, review):
        from odoo.addons.mml_edi.parsers import nimbrel_edifact as edifact
        out = self.parser.generate_ack(review)
        self.assertIsInstance(out, bytes)
        _, segs = edifact.tokenize(out.decode("latin-1"))
        return segs

    def _seg(self, segs, tag):
        return [s for s in segs if s.tag == tag]


class TestGenerateAckLiveQtyOdoo(_NimbrelOrdrspLiveOdooBase):
    """Committed qty must reflect the LIVE sale.order.line, not a parse-time
    recompute — proven here via a real ORM write after review creation."""

    def test_accepted_in_full_reads_live_product_uom_qty(self):
        so = self._make_so()
        self._make_sol(so, product_uom_qty=2.0, edi_qty_shortfall=0.0)
        review = self._make_review(so)

        segs = self._segs(review)
        self.assertEqual(self._seg(segs, "BGM")[0].comp(2, 0), "29")
        qty113 = [q for q in self._seg(segs, "QTY") if q.comp(0, 0) == "113"][0]
        self.assertEqual(qty113.comp(0, 1), "2")

    def test_operator_edits_qty_after_creation_ack_reflects_live_value(self):
        """Simulates a reviewer correcting the SO line qty post-parse — the
        ACK must commit the CURRENT value, not whatever shortfall was
        recorded at parse time (finding #10)."""
        so = self._make_so()
        sol = self._make_sol(so, product_uom_qty=2.0, edi_ordered_qty=2.0,
                              edi_qty_shortfall=0.0)
        review = self._make_review(so)

        sol.write({"product_uom_qty": 1.0})  # operator correction, no shortfall

        segs = self._segs(review)
        self.assertEqual(self._seg(segs, "LIN")[0].comp(1, 0), "3")
        qty113 = [q for q in self._seg(segs, "QTY") if q.comp(0, 0) == "113"][0]
        self.assertEqual(qty113.comp(0, 1), "1")


class TestGenerateAckScenario4APriceCorrectionOdoo(_NimbrelOrdrspLiveOdooBase):
    """Scenario 4A: operator corrects sol.price_unit away from the
    EDI-received edi_price -> action 3 with the CORRECTED price on the wire."""

    def test_price_correction_emits_action_3_with_corrected_price(self):
        so = self._make_so()
        self._make_sol(so, price_unit=9.99, edi_price=9.99)
        review = self._make_review(so)

        so.order_line.write({"price_unit": 7.50})

        segs = self._segs(review)
        self.assertEqual(self._seg(segs, "BGM")[0].comp(2, 0), "4")
        self.assertEqual(self._seg(segs, "LIN")[0].comp(1, 0), "3")
        pri = self._seg(segs, "PRI")[0]
        self.assertEqual(pri.comp(0, 1), "7.5000")


class TestGenerateAckSS6FailClosedOdoo(_NimbrelOrdrspLiveOdooBase):
    """SS-6: a PO line with NO matching SOL defaults to REJECTED, never
    accepted-in-full — proven with a real (empty) sale.order.order_line."""

    def test_no_sol_for_edi_line_defaults_to_rejected(self):
        so = self._make_so()   # no SOL created at all — line 1 unmatched
        review = self._make_review(so)

        segs = self._segs(review)
        self.assertEqual(self._seg(segs, "LIN")[0].comp(1, 0), "7")
        ftx = self._seg(segs, "FTX")
        self.assertTrue(ftx and ftx[0].comp(3, 0))
        self.assertEqual(self._seg(segs, "BGM")[0].comp(2, 0), "4")


class TestGenerateAckSiblingAggregationOdoo(_NimbrelOrdrspLiveOdooBase):
    """Finding #11: sol_by_line must aggregate across ALL sibling reviews of
    the same PO — a real edi.order.review.search, not just this review's own
    sale_order_id."""

    def test_sibling_review_sol_resolves_this_reviews_missing_line(self):
        # THIS review's own SO has no matching line for EDI line 1...
        self_so = self._make_so(ref_suffix="SELF")
        self_review = self._make_review(self_so, file_hash="livehash-self")

        # ...but a SIBLING review (same PO, same partner) DOES carry it.
        sibling_so = self._make_so(ref_suffix="SIB")
        self._make_sol(sibling_so, product_uom_qty=2.0, edi_qty_shortfall=0.0)
        self._make_review(sibling_so, file_hash="livehash-sib", store_code="99999")

        segs = self._segs(self_review)
        self.assertEqual(self._seg(segs, "LIN")[0].comp(1, 0), "5")
        qty113 = [q for q in self._seg(segs, "QTY") if q.comp(0, 0) == "113"][0]
        self.assertEqual(qty113.comp(0, 1), "2")


class TestGenerateAckEnvelopeIdentityOdoo(_NimbrelOrdrspLiveOdooBase):
    """AN-01: the ORDRSP envelope must carry the real trading-partner
    identity (get_unb_sender/get_unb_recipient) — never the SUPPLIER_GLN/
    12341/frozen-2020-timestamp worked-example placeholders."""

    def test_envelope_uses_real_identity_not_placeholders(self):
        so = self._make_so()
        self._make_sol(so)
        review = self._make_review(so)

        segs = self._segs(review)
        unb = self._seg(segs, "UNB")[0]
        self.assertEqual(unb.elements[1], ["0200000000004T", "ZZZ"])
        self.assertEqual(unb.elements[2], ["TST1NIMBREL", "ZZZ"])
        self.assertNotIn(unb.comp(4, 0), ("12341", "99101", "78401"))

    def test_supplier_nad_uses_nimbrel_vendor_code(self):
        so = self._make_so()
        self._make_sol(so)
        review = self._make_review(so)

        segs = self._segs(review)
        nad_su = [s for s in self._seg(segs, "NAD") if s.comp(0, 0) == "SU"][0]
        self.assertEqual(nad_su.comp(1, 0), "V1058")

    def test_production_environment_recipient_is_nimbrel(self):
        self.trading_partner.write({"environment": "production"})
        so = self._make_so()
        self._make_sol(so)
        review = self._make_review(so)

        segs = self._segs(review)
        unb = self._seg(segs, "UNB")[0]
        self.assertEqual(unb.elements[2][0], "NIMBREL")


class TestGenerateAckCancellationRefusalOdoo(_NimbrelOrdrspLiveOdooBase):
    """AN-05 / C5: generate_ack must refuse cleanly for a cancellation
    review — proven against a real edi.order.review record carrying the
    production CANCELLATION_MARKER change_summary."""

    def test_refuses_review_with_cancellation_marker_change_summary(self):
        from odoo.addons.mml_edi.parsers.nimbrel import CancellationAckRefused

        so = self._make_so()
        review = self._make_review(so, state="rejected")
        review.write({
            "change_summary": "EDI-CANCELLATION: PO PO-LIVE-001 cancelled by customer (BGM 1225=1)",
        })

        with self.assertRaises(CancellationAckRefused):
            self.parser.generate_ack(review)
