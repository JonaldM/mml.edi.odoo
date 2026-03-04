# mml.edi/tests/test_cascade_lookup.py
"""
Cascade product lookup tests.

Tests the _find_product() cascade logic:
  1. Try configured product_match_field with product_code
  2. If miss, try barcode with product_code
  3. If miss, try default_code with vendor_code
  4. If miss, try supplierinfo.product_code with buyer_article_no
  5. If all miss -> product_not_found blocking issue

Also tests:
  - sol.edi_matched_by is set correctly
  - A warning 'product_matched_by_fallback' issue is created on fallback
  - Primary match does NOT create a fallback warning issue

Run with: ./odoo-bin --test-enable -d <db> --test-tags mml_edi
"""
import unittest

from odoo.tests.common import TransactionCase

from .common import EDITestSetup, make_fallback_lookup_order

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
class TestCascadeLookup(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        # The trading partner uses product_match_field='barcode' from EDITestSetup.
        # For cascade tests we keep that — the primary match will fail,
        # forcing fallback through vendor_code (default_code).

    def _create_product_no_barcode(self, internal_ref="MML-INTERNAL-001"):
        """Product with default_code set but NO barcode — primary EAN lookup will miss."""
        return self.env["product.product"].create({
            "name": "Cascade Test Product",
            "default_code": internal_ref,
            "list_price": 9.99,
            "type": "product",
            # Intentionally NO barcode field set
        })

    def _create_supplier_coded_product(self, buyer_code="BRISCOES-ART-001"):
        """Product with neither barcode nor default_code, but a supplierinfo entry."""
        product = self.env["product.product"].create({
            "name": "Supplier Code Test Product",
            "list_price": 9.99,
            "type": "product",
        })
        self.env["product.supplierinfo"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "product_id": product.id,
            "product_code": buyer_code,
        })
        return product

    # ── Fallback to default_code ──────────────────────────────────────────

    def test_cascade_fallback_to_default_code(self):
        """
        EAN-13 (product_code) doesn't match. vendor_code matches default_code.
        SO line should be created, edi_matched_by='default_code', warning issue raised.
        """
        product = self._create_product_no_barcode("MML-INTERNAL-001")
        # Add to pricelist so no price discrepancy issue is raised
        self.env["product.pricelist.item"].create({
            "pricelist_id": self.trading_partner.pricelist_id.id,
            "product_id": product.id,
            "compute_price": "fixed",
            "fixed_price": 9.99,
        })

        order = make_fallback_lookup_order(
            primary_ean="NONEXISTENT_EAN_0000",
            vendor_code="MML-INTERNAL-001",
        )

        review = self.env["edi.order.review"].create({
            "trading_partner_id": self.trading_partner.id,
            "customer_po_number": order.po_number,
            "document_type": "new_order",
        })
        so = self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "edi_trading_partner_id": self.trading_partner.id,
            "client_order_ref": order.po_number,
        })

        # This call should succeed via fallback
        blocking = self.processor._process_order_line(order.lines[0], so, self.trading_partner, review)

        # SO line created (not blocking)
        self.assertEqual(len(blocking), 0, "Cascade fallback should not be blocking")
        sol = so.order_line
        self.assertEqual(len(sol), 1, "One SO line should be created")
        self.assertEqual(sol.edi_matched_by, "default_code",
                         "edi_matched_by should record the fallback strategy used")

        # Warning issue raised for fallback
        fallback_issues = review.issue_ids.filtered(
            lambda i: i.issue_type == "product_matched_by_fallback"
        )
        self.assertEqual(len(fallback_issues), 1,
                         "One fallback warning issue should be created")
        self.assertEqual(fallback_issues.severity, "warning")

    def test_cascade_fallback_to_supplier_sku(self):
        """
        EAN and vendor_code both miss. buyer_article_no matches supplierinfo.product_code.
        edi_matched_by='supplier_sku', warning issue raised.
        """
        product = self._create_supplier_coded_product("BRISCOES-ART-001")
        self.env["product.pricelist.item"].create({
            "pricelist_id": self.trading_partner.pricelist_id.id,
            "product_id": product.id,
            "compute_price": "fixed",
            "fixed_price": 9.99,
        })

        order = make_fallback_lookup_order(
            primary_ean="NONEXISTENT_EAN_9999",
            vendor_code="NONEXISTENT_INTERNAL",
            buyer_article_no="BRISCOES-ART-001",
        )

        review = self.env["edi.order.review"].create({
            "trading_partner_id": self.trading_partner.id,
            "customer_po_number": order.po_number,
            "document_type": "new_order",
        })
        so = self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "edi_trading_partner_id": self.trading_partner.id,
            "client_order_ref": order.po_number,
        })

        blocking = self.processor._process_order_line(order.lines[0], so, self.trading_partner, review)

        self.assertEqual(len(blocking), 0, "supplier_sku fallback should not be blocking")
        sol = so.order_line
        self.assertEqual(len(sol), 1, "One SO line should be created via supplier_sku fallback")
        self.assertEqual(sol.edi_matched_by, "supplier_sku",
                         "edi_matched_by should be supplier_sku when matched via buyer_article_no")

        fallback_issues = review.issue_ids.filtered(
            lambda i: i.issue_type == "product_matched_by_fallback"
        )
        self.assertEqual(len(fallback_issues), 1,
                         "One fallback warning issue should be created for supplier_sku match")

    def test_cascade_all_miss_product_not_found(self):
        """
        All four strategies fail. product_not_found blocking issue raised. No SO line.
        """
        order = make_fallback_lookup_order(
            primary_ean="DEAD_EAN",
            vendor_code="DEAD_INTERNAL",
            buyer_article_no="DEAD_BUYER_CODE",
        )
        review = self.env["edi.order.review"].create({
            "trading_partner_id": self.trading_partner.id,
            "customer_po_number": order.po_number,
            "document_type": "new_order",
        })
        so = self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "edi_trading_partner_id": self.trading_partner.id,
            "client_order_ref": order.po_number,
        })

        blocking = self.processor._process_order_line(order.lines[0], so, self.trading_partner, review)

        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0]["type"], "product_not_found")
        self.assertEqual(len(so.order_line), 0, "No SO line when product not found")

    def test_primary_match_no_fallback_issue(self):
        """
        Primary barcode match succeeds. edi_matched_by='barcode', no fallback issue.
        (Regression: cascade must not trigger for primary matches.)
        """
        # self.test_product from EDITestSetup has barcode='TEST001'
        order = make_fallback_lookup_order(
            primary_ean="TEST001",  # matches self.test_product.barcode
            vendor_code="SOME_CODE",
        )
        review = self.env["edi.order.review"].create({
            "trading_partner_id": self.trading_partner.id,
            "customer_po_number": order.po_number,
            "document_type": "new_order",
        })
        so = self.env["sale.order"].create({
            "partner_id": self.trading_partner.partner_id.id,
            "edi_trading_partner_id": self.trading_partner.id,
            "client_order_ref": order.po_number,
        })

        blocking = self.processor._process_order_line(order.lines[0], so, self.trading_partner, review)

        sol = so.order_line
        self.assertEqual(len(sol), 1)
        self.assertEqual(sol.edi_matched_by, "barcode",
                         "Primary match → edi_matched_by should be 'barcode'")

        fallback_issues = review.issue_ids.filtered(
            lambda i: i.issue_type == "product_matched_by_fallback"
        )
        self.assertEqual(len(fallback_issues), 0,
                         "No fallback issue on primary match")
