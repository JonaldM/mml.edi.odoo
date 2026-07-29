# mml.edi/tests/test_nimbrel_pipeline_odoo.py
"""
Odoo TransactionCase tests for the Nimbrel inbound pipeline fixes
(Wave1-B / go-live gate review: AN-02 CONTRL emission, inbound-CONTRL
routing, latin-1 ingest, envelope validation, C5 cancellation routing).

Complements tests/test_nimbrel_pipeline.py (pure, no Odoo). These exercise
the same code paths through real Odoo models: sale.order, edi.order.review,
edi.log — with FTP replaced by RecordingFTPHandler (no network) exactly like
tests/test_ack_send_claim.py.

Run with: ./odoo-bin --test-enable -d <db> --test-tags mml_edi
"""
import unittest
from unittest import mock

from odoo import fields
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger

from .common import EDITestSetup, RecordingFTPHandler

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")

_NIMBREL_ORDERS = (
    "UNA:+.? '"
    "UNB+UNOC:3+NIMBREL:ZZZ+SUPPLIER_GLN:14+210114:0915+88301++++1'"
    "UNH+1+ORDERS:D:01B:UN:EAN011'"
    "BGM+220+PO-ODOO-001+9'"
    "DTM+137:20210114:102'"
    "NAD+BY+NIMBREL::92++Nimbrel NZ Holding LTD'"
    "NAD+SU+V1058::92++M&M Pty Ltd'"
    "NAD+ST+12345::92++Nimbrel Invercargill'"
    "LIN+1'"
    "PIA+5+ISC001:IN'"
    "PIA+1+9780000000002:SA'"
    "IMD+F++:::Test Product'"
    "QTY+21:5:EA'"
    "PRI+AAA:9.99'"
    "UNS+S'"
    "CNT+2:1'"
    "UNT+13+1'"
    "UNZ+1+88301'"
)

_NIMBREL_CANCEL = (
    "UNA:+.? '"
    "UNB+UNOC:3+NIMBREL:ZZZ+SUPPLIER_GLN:14+210114:0915+88302++++1'"
    "UNH+1+ORDERS:D:01B:UN:EAN011'"
    "BGM+220+PO-ODOO-001+1'"
    "DTM+137:20210114:102'"
    "NAD+BY+NIMBREL::92++Nimbrel NZ Holding LTD'"
    "NAD+SU+V1058::92++M&M Pty Ltd'"
    "NAD+ST+12345::92++Nimbrel Invercargill'"
    "UNS+S'"
    "CNT+2:0'"
    "UNT+9+1'"
    "UNZ+1+88302'"
)


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")
class _NimbrelOdooTestBase(EDITestSetup, TransactionCase):
    """Shared setup: an Nimbrel-format (EDIFACT D.01B) trading partner with
    the real NimbrelParser allow-listed, FTP replaced by a recording fake."""

    def setUp(self):
        super().setUp()
        self.setup_edi_test_data()
        self.trading_partner.write({
            "edi_format": "edifact_d01b",
            "parser_class": "mml_edi.parsers.nimbrel.NimbrelParser",
            # Fictional counterparty VAN mailbox. The module defaults are
            # the REAL provisioned mailbox ids (wire routing data), so
            # fixtures configure their own rather than asserting on them.
            "unb_recipient_id": "NIMBREL",
            "unb_recipient_test_id": "TST1NIMBREL",
            "order_split_mode": "single",
        })
        # Match the barcode PIA+1 code used in the fixtures above.
        self.test_product.barcode = "9780000000002"

        try:
            from odoo.addons.mml_edi.models import edi_ftp as edi_ftp_mod
        except ImportError:
            from odoo.addons.mml_edi.models import edi_ftp as edi_ftp_mod
        RecordingFTPHandler.reset()
        patcher = mock.patch.object(edi_ftp_mod, "EDIFTPHandler", RecordingFTPHandler)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _logs(self, event_type, status=None):
        domain = [
            ("trading_partner_id", "=", self.trading_partner.id),
            ("event_type", "=", event_type),
        ]
        if status:
            domain.append(("status", "=", status))
        return self.env["edi.log"].search(domain)


@mute_logger("odoo.addons.mml_edi.models.edi_processor",
             "odoo.addons.mml_edi.models.edi_order_review")
class TestLatin1IngestOdoo(_NimbrelOdooTestBase):
    """AN gate review verified missed finding: inbound stored via
    decode('utf-8', errors='replace') permanently corrupts Latin-1 bytes."""

    def test_review_raw_data_preserves_latin1_byte(self):
        raw_with_latin1 = _NIMBREL_ORDERS.replace(
            "Test Product", "Caf\xe9 Product"
        ).encode("iso-8859-1")

        failures = self.processor._process_file(
            raw_with_latin1, "hash-latin1", "orders_latin1.edi",
            self.trading_partner,
        )
        self.assertEqual(failures, [])

        review = self.env["edi.order.review"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("customer_po_number", "=", "PO-ODOO-001"),
        ], limit=1)
        self.assertTrue(review)
        self.assertIn("Caf\xe9", review.edi_raw_data)
        self.assertNotIn("�", review.edi_raw_data)


@mute_logger("odoo.addons.mml_edi.models.edi_processor",
             "odoo.addons.mml_edi.models.edi_order_review")
class TestContrlEmissionOdoo(_NimbrelOdooTestBase):
    """AN-02: every inbound ORDERS interchange gets exactly one CONTRL,
    end-to-end through the real poll pipeline pieces (_process_file +
    _send_file_responses), with FTP recorded not networked."""

    def _stub_generate_contrl(self):
        """The real NimbrelParser.generate_contrl may not exist yet on this
        checkout (Wave1-C's C4 work) — patch it onto the class so this test
        exercises the processor's C4 consumption contract regardless of
        landing order. If the real method already exists, this override is
        harmlessly equivalent for the purposes of this assertion (bytes
        payload content is not asserted here — only that emission occurs
        exactly once).
        """
        from odoo.addons.mml_edi.parsers.nimbrel import NimbrelParser
        patcher = mock.patch.object(
            NimbrelParser, "generate_contrl",
            lambda self, raw_text, partner: b"UNA:+.? 'UNB+...'UNZ+1+1'",
            create=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_contrl_uploaded_after_processing_orders_file(self):
        self._stub_generate_contrl()
        RecordingFTPHandler.reset()

        failures = self.processor._process_file(
            _NIMBREL_ORDERS.encode("iso-8859-1"), "hash-contrl-1",
            "orders1.edi", self.trading_partner,
        )
        self.assertEqual(failures, [])
        self.processor._send_file_responses(self.trading_partner, "hash-contrl-1")

        contrl_uploads = [f for f in RecordingFTPHandler.uploads if f.startswith("CONTRL_")]
        self.assertEqual(len(contrl_uploads), 1)
        self.assertEqual(len(self._logs("contrl_sent", "success")), 1)

    def test_contrl_not_duplicated_on_repeated_send_file_responses(self):
        self._stub_generate_contrl()
        RecordingFTPHandler.reset()

        failures = self.processor._process_file(
            _NIMBREL_ORDERS.encode("iso-8859-1"), "hash-contrl-2",
            "orders2.edi", self.trading_partner,
        )
        self.assertEqual(failures, [])
        self.processor._send_file_responses(self.trading_partner, "hash-contrl-2")
        self.processor._send_file_responses(self.trading_partner, "hash-contrl-2")

        contrl_uploads = [f for f in RecordingFTPHandler.uploads if f.startswith("CONTRL_")]
        self.assertEqual(len(contrl_uploads), 1, "CONTRL must be sent at most once per interchange")

    def test_no_contrl_support_is_a_strict_noop_kestrelby(self):
        """Preserve Kestrelby byte-for-byte: the iDOC parser has no
        generate_contrl, so no CONTRL upload must ever be attempted."""
        self.trading_partner.write({
            "edi_format": "idoc_xml",
            "parser_class": "mml_edi.parsers.kestrelby_idoc.KestrelbyIDOCParser",
        })
        from .common import make_idoc_raw
        RecordingFTPHandler.reset()

        raw = make_idoc_raw("PO-KESTRELBY-1").encode("utf-8")
        failures = self.processor._process_file(
            raw, "hash-kestrelby-1", "idoc1.xml", self.trading_partner,
        )
        self.assertEqual(failures, [])
        self.processor._send_file_responses(self.trading_partner, "hash-kestrelby-1")

        contrl_uploads = [f for f in RecordingFTPHandler.uploads if f.startswith("CONTRL_")]
        self.assertEqual(contrl_uploads, [])
        self.assertEqual(len(self._logs("contrl_sent")), 0)


@mute_logger("odoo.addons.mml_edi.models.edi_processor")
class TestInboundContrlRoutingOdoo(_NimbrelOdooTestBase):
    """AN: an inbound CONTRL must never be silently swallowed as
    'parsed 0 orders' — it must be routed to parse_contrl and logged."""

    _CONTRL_IN = (
        "UNA:+.? '"
        "UNB+UNOC:3+NIMBREL:ZZZ+SUPPLIER_GLN:14+200928:1030+99101'"
        "UNH+0001+CONTRL:D:3:UN:EAN004'"
        "UCI+72+SUPPLIER_GLN:14+NIMBREL:ZZZ+8'"
        "UNT+3+0001'"
        "UNZ+1+99101'"
    )

    def _stub_parse_contrl(self):
        from odoo.addons.mml_edi.parsers.nimbrel import NimbrelParser
        from odoo.addons.mml_edi.parsers.nimbrel_contrl import parse_contrl as real_parse_contrl
        patcher = mock.patch.object(
            NimbrelParser, "parse_contrl",
            staticmethod(lambda raw_text: real_parse_contrl(raw_text)),
            create=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_inbound_contrl_does_not_create_any_review(self):
        self._stub_parse_contrl()

        failures = self.processor._process_file(
            self._CONTRL_IN.encode("iso-8859-1"), "hash-inbound-contrl-1",
            "CONTRL_IN_1.edi", self.trading_partner,
        )
        self.assertEqual(failures, [])

        reviews = self.env["edi.order.review"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("edi_file_hash", "=", "hash-inbound-contrl-1"),
        ])
        self.assertFalse(reviews, "an inbound CONTRL must never create an order review")
        self.assertEqual(len(self._logs("contrl_received", "success")), 1)


@mute_logger("odoo.addons.mml_edi.models.edi_processor",
             "odoo.addons.mml_edi.models.edi_order_review")
class TestCancellationRoutingOdoo(_NimbrelOdooTestBase):
    """C5: a cancellation cancels the SO, audits via chatter, and NEVER
    queues an ORDRSP — CONTRL only. Also verifies the retry cron can never
    pick it up."""

    def _create_confirmed_so(self, po="PO-ODOO-001"):
        failures = self.processor._process_file(
            _NIMBREL_ORDERS.encode("iso-8859-1"), "hash-orig-1",
            "orders_orig.edi", self.trading_partner,
        )
        self.assertEqual(failures, [])
        review = self.env["edi.order.review"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("customer_po_number", "=", po),
        ], limit=1)
        self.assertTrue(review)
        so = review.sale_order_id
        so.action_confirm()
        return so

    def test_cancellation_cancels_so_and_creates_terminal_review(self):
        so = self._create_confirmed_so()
        RecordingFTPHandler.reset()

        failures = self.processor._process_file(
            _NIMBREL_CANCEL.encode("iso-8859-1"), "hash-cancel-1",
            "cancel1.edi", self.trading_partner,
        )
        self.assertEqual(failures, [])

        so.invalidate_recordset(["state"])
        self.assertEqual(so.state, "cancel")

        cancel_review = self.env["edi.order.review"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("edi_file_hash", "=", "hash-cancel-1"),
        ], limit=1)
        self.assertTrue(cancel_review)
        self.assertEqual(cancel_review.state, "rejected")
        self.assertTrue(
            cancel_review.change_summary.startswith(
                self.processor.CANCELLATION_MARKER))

    def test_cancellation_never_queues_ordrsp(self):
        self._create_confirmed_so()
        RecordingFTPHandler.reset()

        failures = self.processor._process_file(
            _NIMBREL_CANCEL.encode("iso-8859-1"), "hash-cancel-2",
            "cancel2.edi", self.trading_partner,
        )
        self.assertEqual(failures, [])
        self.processor._send_file_responses(self.trading_partner, "hash-cancel-2")

        ack_uploads = [f for f in RecordingFTPHandler.uploads if f.startswith("ACK_")]
        self.assertEqual(ack_uploads, [], "a cancellation must never send an ORDRSP")
        self.assertEqual(len(self._logs("ack_sent")), 0)

    def test_retry_pending_acks_never_touches_cancellation_review(self):
        self._create_confirmed_so()
        failures = self.processor._process_file(
            _NIMBREL_CANCEL.encode("iso-8859-1"), "hash-cancel-3",
            "cancel3.edi", self.trading_partner,
        )
        self.assertEqual(failures, [])
        RecordingFTPHandler.reset()

        # Must not raise and must not upload an ORDRSP for the cancellation.
        self.processor.retry_pending_acks()

        ack_uploads = [f for f in RecordingFTPHandler.uploads if f.startswith("ACK_")]
        self.assertEqual(ack_uploads, [])

    def test_cancel_before_original_logs_warning_creates_no_review(self):
        """No matching SO yet — nothing to cancel; must not create a
        phantom review that could confuse a later ORDERS for the same PO."""
        failures = self.processor._process_file(
            _NIMBREL_CANCEL.encode("iso-8859-1"), "hash-cancel-early",
            "cancel_early.edi", self.trading_partner,
        )
        self.assertEqual(failures, [])

        reviews = self.env["edi.order.review"].search([
            ("trading_partner_id", "=", self.trading_partner.id),
            ("edi_file_hash", "=", "hash-cancel-early"),
        ])
        self.assertFalse(reviews)
        self.assertEqual(len(self._logs("error", "warning")), 1)
