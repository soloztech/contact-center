import threading
import uuid

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("-at_install", "post_install")
class TestAccessTopologyConcurrency(TransactionCase):
    """Prove the owner/team union cannot be emptied by concurrent writers."""

    WORKER_TIMEOUT_SECONDS = 12

    def _setup_committed_fixture(self, token):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            company = env.company
            agent_group = env.ref("contact_center_base.group_contact_center_agent")

            def create_agent(role):
                return (
                    env["res.users"]
                    .with_context(no_reset_password=True)
                    .create(
                        {
                            "name": "Access topology %s %s" % (role, token),
                            "login": "cc-access-%s-%s" % (role, token),
                            "company_id": company.id,
                            "company_ids": [(6, 0, company.ids)],
                            "groups_id": [(6, 0, agent_group.ids)],
                        }
                    )
                )

            owner = create_agent("owner")
            agent = create_agent("agent")
            candidate = create_agent("candidate")
            team = env["contact.center.team"].create(
                {
                    "name": "Access topology team %s" % token,
                    "company_id": company.id,
                    "agent_ids": [(6, 0, agent.ids)],
                }
            )
            account = env["contact.center.account"].create(
                {
                    "name": "Access topology account %s" % token,
                    "company_id": company.id,
                    "platform": "whatsapp",
                    "external_ref": "access-topology-%s" % token,
                    "owner_user_id": owner.id,
                    "default_team_id": team.id,
                }
            )
            connection = env["contact.center.provider.connection"].create(
                {
                    "name": "Access topology provider %s" % token,
                    "account_id": account.id,
                    "adapter_key": "test.fake",
                    "external_ref": "access-topology-provider-%s" % token,
                    "state": "connected",
                    "active": True,
                    "role": "primary",
                    "inbound_active": True,
                    "outbound_active": False,
                }
            )
            cr.commit()  # pylint: disable=invalid-commit
            return {
                "owner_id": owner.id,
                "agent_id": agent.id,
                "candidate_id": candidate.id,
                "team_id": team.id,
                "account_id": account.id,
                "connection_id": connection.id,
            }

    def _retry_scope_mutation(self, fixture, mutation):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            try:
                if mutation == "owner":
                    env["contact.center.account"].browse(fixture["account_id"]).write(
                        {"owner_user_id": False}
                    )
                else:
                    env["contact.center.team"].browse(fixture["team_id"]).write(
                        {"agent_ids": [(5, 0, 0)]}
                    )
                cr.commit()  # pylint: disable=invalid-commit
                return "committed"
            except ValidationError:
                cr.rollback()
                return "validation"
            except SerializationFailure:
                cr.rollback()
                return "serialization"

    def _concurrent_scope_mutation(self, fixture, mutation, barrier, results):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            account = env["contact.center.account"].browse(fixture["account_id"])
            team = env["contact.center.team"].browse(fixture["team_id"])
            # Establish the same pre-change REPEATABLE READ snapshot in both workers.
            self.assertTrue(account.owner_user_id)
            self.assertTrue(team.agent_ids)
            barrier.wait(timeout=self.WORKER_TIMEOUT_SECONDS)
            try:
                if mutation == "owner":
                    account.write({"owner_user_id": False})
                else:
                    team.write({"agent_ids": [(5, 0, 0)]})
                cr.commit()  # pylint: disable=invalid-commit
                results.append((mutation, "committed"))
            except ValidationError:
                cr.rollback()
                results.append((mutation, "validation"))
            except SerializationFailure:
                cr.rollback()
                results.append(
                    (mutation, self._retry_scope_mutation(fixture, mutation))
                )

    def _cleanup_committed_fixture(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["contact.center.provider.connection"].sudo().with_context(
                active_test=False
            ).browse(fixture["connection_id"]).unlink()
            env["contact.center.account"].sudo().with_context(active_test=False).browse(
                fixture["account_id"]
            ).unlink()
            env["contact.center.team"].sudo().with_context(active_test=False).browse(
                fixture["team_id"]
            ).unlink()
            env["res.users"].sudo().with_context(active_test=False).browse(
                [
                    fixture["owner_id"],
                    fixture["agent_id"],
                    fixture["candidate_id"],
                ]
            ).unlink()
            cr.commit()  # pylint: disable=invalid-commit

    def test_owner_and_roster_revocation_converge_without_orphaning_live_route(self):
        fixture = self._setup_committed_fixture(uuid.uuid4().hex)
        barrier = threading.Barrier(2)
        results = []
        workers = [
            threading.Thread(
                target=self._concurrent_scope_mutation,
                args=(fixture, mutation, barrier, results),
            )
            for mutation in ("owner", "roster")
        ]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(self.WORKER_TIMEOUT_SECONDS)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(
                sorted(state for _mutation, state in results),
                ["committed", "validation"],
            )
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                account = env["contact.center.account"].browse(fixture["account_id"])
                connection = env["contact.center.provider.connection"].browse(
                    fixture["connection_id"]
                )
                self.assertEqual(len(account._contact_center_effective_users()), 1)
                self.assertTrue(connection._contact_center_inbound_is_available())
        finally:
            self._cleanup_committed_fixture(fixture)

    def _retry_candidate_mutation(self, fixture, mutation):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            try:
                if mutation == "roster_add":
                    env["contact.center.team"].browse(fixture["team_id"]).write(
                        {"agent_ids": [(4, fixture["candidate_id"])]}
                    )
                else:
                    env["res.users"].browse(fixture["candidate_id"]).write(
                        {"active": False}
                    )
                cr.commit()  # pylint: disable=invalid-commit
                return "committed"
            except ValidationError:
                cr.rollback()
                return "validation"
            except SerializationFailure:
                cr.rollback()
                return "serialization"

    def _concurrent_candidate_mutation(self, fixture, mutation, barrier, results):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            team = env["contact.center.team"].browse(fixture["team_id"])
            candidate = env["res.users"].browse(fixture["candidate_id"])
            # Both workers start from an active candidate outside the roster.
            self.assertTrue(candidate.active)
            self.assertNotIn(candidate, team.agent_ids | team.supervisor_ids)
            barrier.wait(timeout=self.WORKER_TIMEOUT_SECONDS)
            try:
                if mutation == "roster_add":
                    team.write({"agent_ids": [(4, candidate.id)]})
                else:
                    candidate.write({"active": False})
                cr.commit()  # pylint: disable=invalid-commit
                results.append((mutation, "committed"))
            except ValidationError:
                cr.rollback()
                results.append((mutation, "validation"))
            except SerializationFailure:
                cr.rollback()
                results.append(
                    (mutation, self._retry_candidate_mutation(fixture, mutation))
                )

    def test_roster_add_and_user_archive_converge_to_valid_authorization(self):
        fixture = self._setup_committed_fixture(uuid.uuid4().hex)
        barrier = threading.Barrier(2)
        results = []
        workers = [
            threading.Thread(
                target=self._concurrent_candidate_mutation,
                args=(fixture, mutation, barrier, results),
            )
            for mutation in ("roster_add", "user_archive")
        ]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(self.WORKER_TIMEOUT_SECONDS)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(
                sorted(state for _mutation, state in results),
                ["committed", "validation"],
            )
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                team = env["contact.center.team"].browse(fixture["team_id"])
                candidate = (
                    env["res.users"]
                    .with_context(active_test=False)
                    .browse(fixture["candidate_id"])
                )
                self.assertFalse(candidate.active and candidate not in team.agent_ids)
                self.assertFalse(not candidate.active and candidate in team.agent_ids)
        finally:
            self._cleanup_committed_fixture(fixture)
