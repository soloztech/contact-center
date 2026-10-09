"""Real PostgreSQL checks for cumulative commercial-period pre-migrations."""
import runpy
from pathlib import Path

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestScopeDecisionMigration(TransactionCase):
    def _migrate(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "migrations/16.0.1.5.0/pre-migration.py"
        )
        runpy.run_path(str(path))["migrate"](self.env.cr, "16.0.1.2.0")

    def test_upgrade_before_period_columns_is_idempotent(self):
        self.env.cr.execute(
            "CREATE TEMP TABLE contact_center_crm_conversation_link "
            "(id integer, state varchar) ON COMMIT DROP"
        )
        self.env.cr.execute(
            "INSERT INTO contact_center_crm_conversation_link VALUES "
            "(1,'linked'),(2,'unlinked')"
        )
        self._migrate()
        self._migrate()
        self.env.cr.execute(
            "SELECT scope_decision_mode FROM contact_center_crm_conversation_link ORDER BY id"
        )
        self.assertEqual(self.env.cr.fetchall(), [("none",), ("none",)])

    def test_confirmed_active_and_retired_periods_stay_human(self):
        self.env.cr.execute(
            "CREATE TEMP TABLE contact_center_crm_conversation_link "
            "(id integer, state varchar, scope_state varchar, scope_decision_mode varchar) "
            "ON COMMIT DROP"
        )
        self.env.cr.execute(
            "INSERT INTO contact_center_crm_conversation_link VALUES "
            "(1,'linked','confirmed',NULL),(2,'unlinked','confirmed',NULL),"
            "(3,'linked','context',NULL),(4,'linked','confirmed','automatic')"
        )
        self._migrate()
        self._migrate()
        self.env.cr.execute(
            "SELECT scope_decision_mode FROM contact_center_crm_conversation_link ORDER BY id"
        )
        self.assertEqual(
            self.env.cr.fetchall(), [("human",), ("human",), ("none",), ("automatic",)]
        )
