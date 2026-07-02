"""Integration tests for the duplicate-SO DB backstop (go-live FIX IDEM-1b/1c).

- The partial unique index sale_order_edi_client_ref_uniq must reject a second
  live EDI SO for the same (partner, company, client_order_ref).
- Cancelled SOs are excluded (re-order after cancellation stays allowed).
- _process_file must convert a losing create-race (unique violation) into the
  standard duplicate_skipped path instead of failing the file.

Requires a live Odoo DB (odoo-bin --test-enable)."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from .common import EDITestSetup, make_clean_parsed_order

try:
    from odoo.addons.mml_edi.models.edi_processor import (
        SO_EDI_CLIENT_REF_INDEX,
        EDIProcessor,
    )
    from odoo.addons.mml_edi.models.edi_trading_partner import EDITradingPartner
except ImportError:
    from mml_edi.models.edi_processor import (
        SO_EDI_CLIENT_REF_INDEX,
        EDIProcessor,
    )
    from mml_edi.models.edi_trading_partner import EDITradingPartner

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")
class TestDuplicateSoDbBackstop(EDITestSetup, TransactionCase):

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()

    def _so_vals(self, ref):
        return {
            "partner_id": self.trading_partner.partner_id.id,
            "client_order_ref": ref,
            "edi_trading_partner_id": self.trading_partner.id,
            "company_id": self.env.company.id,
        }

    def test_unique_index_rejects_duplicate_live_edi_so(self):
        from odoo.tools import mute_logger
        self.env["sale.order"].create(self._so_vals("DUP-BACKSTOP-1"))
        self.env["sale.order"].flush_model()
        with self.assertRaises(Exception) as ctx:
            with mute_logger("odoo.sql_db"), self.env.cr.savepoint():
                self.env["sale.order"].create(self._so_vals("DUP-BACKSTOP-1"))
                self.env["sale.order"].flush_model()
        self.assertIn(SO_EDI_CLIENT_REF_INDEX, str(ctx.exception),
                      "Second live EDI SO must be rejected by the backstop index")

    def test_cancelled_so_does_not_block_reorder(self):
        so = self.env["sale.order"].create(self._so_vals("DUP-CANCEL-1"))
        so.action_cancel()
        self.assertEqual(so.state, "cancel")
        self.env["sale.order"].flush_model()
        # Re-order after cancellation is a deliberate processor path — the
        # partial index must not block it.
        self.env["sale.order"].create(self._so_vals("DUP-CANCEL-1"))
        self.env["sale.order"].flush_model()

    def test_non_edi_orders_are_not_constrained(self):
        vals = {
            "partner_id": self.trading_partner.partner_id.id,
            "client_order_ref": "NON-EDI-REF-1",
            "company_id": self.env.company.id,
        }
        self.env["sale.order"].create(vals)
        self.env["sale.order"].create(vals)
        self.env["sale.order"].flush_model()

    def test_unique_violation_converted_to_duplicate_skipped(self):
        """A losing create-race must land on the duplicate_skipped log path —
        never crash the poll loop or mark the file failed (IDEM-1c)."""
        fake_parser = SimpleNamespace(
            parse_file=lambda content, partner: [
                make_clean_parsed_order(po_number="RACE-PO-1")],
        )
        race_exc = Exception(
            'duplicate key value violates unique constraint "%s"'
            % SO_EDI_CLIENT_REF_INDEX
        )
        with patch.object(EDITradingPartner, "get_parser_instance",
                          return_value=fake_parser), \
             patch.object(EDIProcessor, "process_parsed_order",
                          side_effect=race_exc):
            failures = self.env["edi.processor"]._process_file(
                b"<race/>", "racehash001", "race.edi", self.trading_partner)
        self.assertEqual(failures, [],
                         "A lost race is NOT a failure — the SO exists (winner's)")
        dup_logs = self.env["edi.log"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("event_type", "=", "duplicate_skipped"),
            ("filename", "=", "race.edi"),
        ])
        self.assertEqual(len(dup_logs), 1)
        self.assertIn("concurrently", dup_logs.message)

    def test_unrelated_exception_still_fails_the_store(self):
        fake_parser = SimpleNamespace(
            parse_file=lambda content, partner: [
                make_clean_parsed_order(po_number="RACE-PO-2")],
        )
        with patch.object(EDITradingPartner, "get_parser_instance",
                          return_value=fake_parser), \
             patch.object(EDIProcessor, "process_parsed_order",
                          side_effect=Exception("boom")):
            failures = self.env["edi.processor"]._process_file(
                b"<boom/>", "boomhash001", "boom.edi", self.trading_partner)
        self.assertEqual(len(failures), 1,
                         "Non-duplicate errors keep the retry semantics")
