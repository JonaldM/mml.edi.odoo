# mml.edi/tests/test_animates_identity_odoo.py
"""Odoo TransactionCase tests for Animates identity (Wave1-A, C1/C2).

Covers what the pure test_animates_identity.py cannot: real edi.trading.partner
ORM records (field persistence, environment write triggering get_unb_recipient
to switch), and the C2 ir.sequence records actually resolving via
next_by_code (proves data/ir_sequence.xml is loaded correctly, monotonic,
and shared across the two Animates codes).

Requires a live Odoo DB (odoo-bin --test-enable)."""
import unittest

from odoo.tests.common import TransactionCase, tagged

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")
class TestAnimatesIdentityFields(TransactionCase):

    def setUp(self):
        super().setUp()
        self.customer = self.env["res.partner"].create({
            "name": "Animates NZ Holding LTD",
            "customer_rank": 1,
        })
        self.partner = self.env["edi.trading.partner"].create({
            "name": "Animates",
            "code": "ANIMATES",
            "partner_id": self.customer.id,
            "edi_format": "edifact_d01b",
            "parser_class": "mml_edi.parsers.animates.AnimatesParser",
            "ftp_protocol": "sftp",
            "ftp_host": "sftp.test.local",
            "ftp_port": 22,
            "environment": "test",
            "order_split_mode": "per_store",
            "product_match_field": "default_code",
            "client_ref_template": "{po_number}",
            "edi_sender_id": "9419416000008T",
            "edi_sender_qualifier": "ZZZ",
            "supplier_gln": "9419416000008",
            "animates_vendor_code": "V1058",
        })

    # --- field persistence -----------------------------------------------

    def test_identity_fields_persist(self):
        self.assertEqual(self.partner.edi_sender_id, "9419416000008T")
        self.assertEqual(self.partner.edi_sender_qualifier, "ZZZ")
        self.assertEqual(self.partner.supplier_gln, "9419416000008")
        self.assertEqual(self.partner.animates_vendor_code, "V1058")

    def test_edi_sender_qualifier_defaults_to_zzz(self):
        other = self.env["edi.trading.partner"].create({
            "name": "Animates No Qualifier",
            "code": "ANIMATESNQ",
            "partner_id": self.customer.id,
            "edi_format": "edifact_d01b",
            "parser_class": "mml_edi.parsers.animates.AnimatesParser",
            "ftp_protocol": "sftp",
            "environment": "test",
            "order_split_mode": "per_store",
            "product_match_field": "default_code",
        })
        self.assertEqual(other.edi_sender_qualifier, "ZZZ")

    # --- get_unb_sender() ---------------------------------------------------

    def test_get_unb_sender_uses_explicit_sender_id_over_gln(self):
        self.assertEqual(
            self.partner.get_unb_sender(), ("9419416000008T", "ZZZ")
        )

    def test_get_unb_sender_falls_back_to_gln(self):
        self.partner.write({"edi_sender_id": False})
        self.assertEqual(
            self.partner.get_unb_sender(), ("9419416000008", "14")
        )

    def test_get_unb_sender_raises_useerror_when_unconfigured(self):
        from odoo.exceptions import UserError
        self.partner.write({"edi_sender_id": False, "supplier_gln": False})
        with self.assertRaises(UserError):
            self.partner.get_unb_sender()

    # --- get_unb_recipient(): environment switching (C1) --------------------

    def test_get_unb_recipient_switches_with_environment_write(self):
        """The SAME partner record must flip TST1ANIMATES <-> ANIMATES purely
        from writing `environment` — this is the live onboarding->go-live
        transition (no other field should need to change)."""
        self.assertEqual(self.partner.environment, "test")
        self.assertEqual(
            self.partner.get_unb_recipient(), ("TST1ANIMATES", "ZZZ")
        )

        self.partner.write({"environment": "production"})
        self.assertEqual(
            self.partner.get_unb_recipient(), ("ANIMATES", "ZZZ")
        )

        self.partner.write({"environment": "test"})
        self.assertEqual(
            self.partner.get_unb_recipient(), ("TST1ANIMATES", "ZZZ")
        )

    # --- C2 sequences ---------------------------------------------------

    def test_animates_interchange_ref_sequence_resolves_and_increments(self):
        Seq = self.env["ir.sequence"].sudo()
        first = Seq.next_by_code("mml_edi.animates.interchange.ref")
        second = Seq.next_by_code("mml_edi.animates.interchange.ref")
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertGreater(int(second), int(first))

    def test_sscc_serial_sequence_resolves_and_increments(self):
        Seq = self.env["ir.sequence"].sudo()
        first = Seq.next_by_code("mml_edi.sscc.serial")
        second = Seq.next_by_code("mml_edi.sscc.serial")
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertGreater(int(second), int(first))

    def test_interchange_ref_and_sscc_serial_are_independent_counters(self):
        """The two C2 sequences must not share state — burning SSCC serials
        (e.g. for a large DESADV) must not skip interchange control refs."""
        Seq = self.env["ir.sequence"].sudo()
        interchange_before = int(Seq.next_by_code("mml_edi.animates.interchange.ref"))
        for _ in range(5):
            Seq.next_by_code("mml_edi.sscc.serial")
        interchange_after = int(Seq.next_by_code("mml_edi.animates.interchange.ref"))
        self.assertEqual(interchange_after, interchange_before + 1)

    # --- build_unb_for_partner wired to a real ORM record -------------------

    def test_build_unb_for_partner_with_real_orm_record(self):
        from odoo.addons.mml_edi.parsers.animates_edifact import build_unb_for_partner
        Seq = self.env["ir.sequence"].sudo()
        ctrl_ref = Seq.next_by_code("mml_edi.animates.interchange.ref")
        unb = build_unb_for_partner(self.partner, ctrl_ref, "260703", "0900")
        self.assertEqual(unb.elements[1], ["9419416000008T", "ZZZ"])
        self.assertEqual(unb.elements[2], ["TST1ANIMATES", "ZZZ"])
        self.assertEqual(unb.elements[4], [str(ctrl_ref)])
