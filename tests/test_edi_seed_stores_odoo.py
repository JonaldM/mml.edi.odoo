# mml.edi/tests/test_edi_seed_stores_odoo.py
"""Odoo TransactionCase tests for edi.seed.stores.wizard (Wave2-F,
gate review AN-13/OPS-5).

Covers what pure pytest (tests/test_animates_store_master.py) cannot: real
res.partner ORM persistence, wizard mode auto-detection from
trading_partner_id.parser_class, idempotent re-run behaviour (no duplicates,
name drift synced), and parenting/ref-collision safety across two trading
partners' customers in the same database.

Requires a live Odoo DB (odoo-bin --test-enable)."""
import unittest

from odoo.tests.common import TransactionCase, tagged

_ODOO_AVAILABLE = hasattr(TransactionCase, "env")


@unittest.skipUnless(_ODOO_AVAILABLE, "Requires Odoo runtime — run with odoo-bin --test-enable")
@tagged("post_install", "-at_install")
class TestEDISeedStoresWizardAnimates(TransactionCase):

    def setUp(self):
        super().setUp()
        self.animates_customer = self.env["res.partner"].create({
            "name": "Animates NZ Holding LTD",
            "customer_rank": 1,
        })
        self.animates_partner = self.env["edi.trading.partner"].create({
            "name": "Animates",
            "code": "ANIMATES",
            "partner_id": self.animates_customer.id,
            "edi_format": "edifact_d01b",
            "parser_class": "mml_edi.parsers.animates.AnimatesParser",
            "ftp_protocol": "sftp",
            "ftp_host": "sftp.test.local",
            "ftp_port": 22,
            "environment": "test",
            "order_split_mode": "per_store",
            "product_match_field": "default_code",
            "client_ref_template": "{po_number}",
        })

    def _wizard(self, trading_partner):
        return self.env["edi.seed.stores.wizard"].create({
            "trading_partner_id": trading_partner.id,
        })

    # --- mode auto-detection -------------------------------------------

    def test_animates_partner_seeds_animates_store_table_not_briscoes(self):
        wizard = self._wizard(self.animates_partner)
        wizard.action_seed_stores()
        children = self.env["res.partner"].search([
            ("parent_id", "=", self.animates_customer.id),
        ])
        refs = set(children.mapped("ref"))
        # Animates-shaped refs present
        self.assertIn("02", refs)
        self.assertIn("R-59", refs)
        # Briscoes numeric refs must NOT appear under the Animates customer
        self.assertNotIn("1017", refs)

    def test_animates_seed_creates_exactly_56_unique_stores(self):
        wizard = self._wizard(self.animates_partner)
        wizard.action_seed_stores()
        self.assertEqual(wizard.result_created, 56)
        self.assertEqual(wizard.result_updated, 0)
        self.assertEqual(wizard.result_skipped, 0)
        self.assertEqual(wizard.state, "done")

    # --- idempotency -----------------------------------------------------

    def test_rerun_creates_no_duplicates(self):
        self._wizard(self.animates_partner).action_seed_stores()
        before_count = self.env["res.partner"].search_count([
            ("parent_id", "=", self.animates_customer.id),
        ])

        second = self._wizard(self.animates_partner)
        second.action_seed_stores()
        after_count = self.env["res.partner"].search_count([
            ("parent_id", "=", self.animates_customer.id),
        ])

        self.assertEqual(before_count, after_count)
        self.assertEqual(second.result_created, 0)
        self.assertEqual(second.result_skipped, 56)

    def test_rerun_syncs_drifted_name_without_creating_or_duplicating(self):
        self._wizard(self.animates_partner).action_seed_stores()

        store_08 = self.env["res.partner"].search([
            ("parent_id", "=", self.animates_customer.id),
            ("ref", "=", "08"),
        ], limit=1)
        self.assertTrue(store_08)
        original_id = store_08.id
        store_08.write({"name": "Manually Renamed By Mistake"})

        second = self._wizard(self.animates_partner)
        second.action_seed_stores()

        # same record, name corrected, no duplicate created for ref "08"
        matches = self.env["res.partner"].search([
            ("parent_id", "=", self.animates_customer.id),
            ("ref", "=", "08"),
        ])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches.id, original_id)
        self.assertEqual(matches.name, "Lincoln Road")
        self.assertEqual(second.result_updated, 1)
        self.assertEqual(second.result_created, 0)

    # --- parenting ---------------------------------------------------------

    def test_seeded_stores_are_delivery_type_children_of_customer(self):
        self._wizard(self.animates_partner).action_seed_stores()
        store = self.env["res.partner"].search([
            ("parent_id", "=", self.animates_customer.id),
            ("ref", "=", "12"),
        ], limit=1)
        self.assertTrue(store)
        self.assertEqual(store.name, "Silverdale")
        self.assertEqual(store.parent_id, self.animates_customer)
        self.assertEqual(store.type, "delivery")
        self.assertEqual(store.customer_rank, 1)

    def test_customerless_partner_is_rejected_by_orm(self):
        """A trading partner with no customer cannot exist: partner_id is
        required (NOT NULL at the DB), so the state the wizard's own
        no-customer guard defends against is unreachable through the ORM.
        Assert the ORM enforces that — the strongest protection. (Manual
        try/except sidesteps Odoo's assertRaises, which rejects a tuple.)"""
        from odoo.tools import mute_logger
        raised = False
        with mute_logger("odoo.sql_db"):
            try:
                with self.env.cr.savepoint():
                    self.env["edi.trading.partner"].create({
                        "name": "Animates Orphan",
                        "code": "ANIMATESORPHAN",
                        "edi_format": "edifact_d01b",
                        "parser_class": "mml_edi.parsers.animates.AnimatesParser",
                        "ftp_protocol": "sftp",
                        "environment": "test",
                        "order_split_mode": "per_store",
                        "product_match_field": "default_code",
                    })
                    self.env.cr.flush()
            except Exception:
                raised = True
        self.assertTrue(
            raised, "a trading partner without a customer must be rejected")

    # --- ref-collision safety across trading partners -----------------------

    def test_animates_and_briscoes_refs_never_collide_across_customers(self):
        """Two different trading partners' customers in the SAME database:
        seeding both must never let one partner's children leak into or
        collide with the other's, even though Animates uses short numeric
        refs that could coincidentally match a truncated Briscoes ref."""
        briscoes_customer = self.env["res.partner"].create({
            "name": "Briscoes Group Ltd",
            "customer_rank": 1,
        })
        briscoes_partner = self.env["edi.trading.partner"].create({
            "name": "Briscoes",
            "code": "BRISCOES",
            "partner_id": briscoes_customer.id,
            "edi_format": "idoc_xml",
            "parser_class": "mml_edi.parsers.briscoes_idoc.BriscoesIDOCParser",
            "ftp_protocol": "ftp",
            "ftp_host": "ftp.test.local",
            "ftp_port": 21,
            "environment": "test",
            "order_split_mode": "per_store",
            "product_match_field": "barcode",
            "client_ref_template": "{po_number}",
        })

        self._wizard(self.animates_partner).action_seed_stores()
        self._wizard(briscoes_partner).action_seed_stores()

        animates_children = self.env["res.partner"].search([
            ("parent_id", "=", self.animates_customer.id),
        ])
        briscoes_children = self.env["res.partner"].search([
            ("parent_id", "=", briscoes_customer.id),
        ])

        self.assertEqual(len(animates_children), 56)
        self.assertEqual(len(briscoes_children), 23)
        self.assertFalse(set(animates_children.ids) & set(briscoes_children.ids))
        # Briscoes store "1017" ref must not appear under Animates and
        # vice-versa (no cross-parent ref search happens anywhere)
        self.assertNotIn("1017", animates_children.mapped("ref"))
        self.assertNotIn("08", briscoes_children.mapped("ref"))
