import threading
import uuid
from unittest import mock

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import SavepointCase, TransactionCase

from ..models import binding as binding_module, crm_roster as crm_roster_module


class TestCrmBindingAuthority(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "CRM binding authority user",
                    "login": "crm-binding-authority-%s" % uuid.uuid4(),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, cls.env.ref("base.group_user").ids)],
                }
            )
        )
        cls.admin_user = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "CRM binding authority administrator",
                    "login": "crm-binding-admin-%s" % uuid.uuid4(),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [
                        (
                            6,
                            0,
                            (
                                cls.env.ref("base.group_user")
                                | cls.env.ref(
                                    "contact_center_base.group_contact_center_admin"
                                )
                            ).ids,
                        )
                    ],
                }
            )
        )

    def _crm_team(self):
        return self.env["crm.team"].create(
            {
                "name": "CRM authority %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "user_id": self.user.id,
            }
        )

    def _user(self, label):
        return (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "CRM authority %s" % label,
                    "login": "crm-binding-%s-%s" % (label, uuid.uuid4()),
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [(6, 0, self.env.ref("base.group_user").ids)],
                }
            )
        )

    def _cc_team(self):
        return self.env["contact.center.team"].create(
            {
                "name": "CC authority %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
            }
        )

    def _admin_restricted_to_another_company(self):
        company = self.env["res.company"].create(
            {"name": "Other CRM authority company %s" % uuid.uuid4()}
        )
        groups = self.env.ref("base.group_user") | self.env.ref(
            "contact_center_base.group_contact_center_admin"
        )
        user = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Other-company CC administrator",
                    "login": "other-company-cc-admin-%s" % uuid.uuid4(),
                    "company_id": company.id,
                    "company_ids": [(6, 0, company.ids)],
                    "groups_id": [(6, 0, groups.ids)],
                }
            )
        )
        return company, user

    def test_catalog_fence_materializes_global_and_team_authorities(self):
        crm_team = self._crm_team()
        binding_module.lock_catalog_authorities(self.env, crm_team.ids)
        Authority = self.env["contact.center.crm.catalog.authority"].sudo()
        global_authority = Authority.search([("authority_key", "=", "global")])
        team_authority = Authority.search(
            [("authority_key", "=", "team:%s" % crm_team.id)]
        )
        self.assertEqual(len(global_authority), 1)
        self.assertEqual(len(team_authority), 1)
        self.assertFalse(global_authority.crm_team_id)
        self.assertEqual(team_authority.crm_team_id, crm_team)
        before = (
            global_authority.authority_revision,
            team_authority.authority_revision,
        )

        binding_module.lock_catalog_authorities(self.env, crm_team.ids)
        global_authority.invalidate_recordset(["authority_revision"])
        team_authority.invalidate_recordset(["authority_revision"])
        self.assertGreater(global_authority.authority_revision, before[0])
        self.assertGreater(team_authority.authority_revision, before[1])

    def test_catalog_fence_uses_single_upsert_statement_per_authority(self):
        crm_team = self._crm_team()
        cursor_type = type(self.env.cr)
        original_execute = cursor_type.execute
        statements = []

        def record_execute(cursor, query, params=None, log_exceptions=None):
            normalized = " ".join(str(query).split())
            if "contact_center_crm_catalog_authority" in normalized:
                statements.append(normalized)
            return original_execute(cursor, query, params, log_exceptions)

        with mock.patch.object(
            cursor_type, "execute", autospec=True, side_effect=record_execute
        ):
            binding_module.lock_catalog_authorities(self.env, crm_team.ids)

        writes = [item for item in statements if item.startswith("INSERT INTO")]
        self.assertEqual(len(writes), 2)
        self.assertTrue(all("ON CONFLICT" in item for item in writes))
        self.assertTrue(all("DO UPDATE" in item for item in writes))
        self.assertFalse(any(item.startswith("UPDATE") for item in statements))

    def test_first_team_binding_locks_binding_authority_before_core(self):
        crm_team = self._crm_team()
        cc_team = self._cc_team()
        events = []
        original_graph = binding_module.lock_binding_graph
        account_type = type(self.env["contact.center.account"])
        original_core = account_type._contact_center_lock_access_topology

        def record_graph(*args, **kwargs):
            events.append("binding")
            return original_graph(*args, **kwargs)

        def record_core(record, *args, **kwargs):
            events.append("core")
            return original_core(record, *args, **kwargs)

        with mock.patch.object(
            binding_module,
            "lock_binding_graph",
            side_effect=record_graph,
        ), mock.patch.object(
            account_type,
            "_contact_center_lock_access_topology",
            autospec=True,
            side_effect=record_core,
        ):
            self.env["contact.center.crm.team.binding"].create(
                {
                    "contact_center_team_id": cc_team.id,
                    "crm_team_id": crm_team.id,
                }
            )

        self.assertLess(events.index("binding"), events.index("core"))

    def test_crm_membership_locks_binding_graph_before_roster_projection(self):
        crm_team = self._crm_team()
        member = self._user("membership-lock-order")
        events = []
        original_graph = crm_roster_module.lock_binding_graph
        team_type = type(crm_team)
        original_sync = team_type._contact_center_sync_bound_rosters

        def record_graph(*args, **kwargs):
            events.append("binding_graph")
            return original_graph(*args, **kwargs)

        def record_sync(record, *args, **kwargs):
            events.append("roster_projection")
            return original_sync(record, *args, **kwargs)

        with mock.patch.object(
            crm_roster_module,
            "lock_binding_graph",
            side_effect=record_graph,
        ), mock.patch.object(
            team_type,
            "_contact_center_sync_bound_rosters",
            autospec=True,
            side_effect=record_sync,
        ):
            self.env["crm.team.member"].create(
                {"crm_team_id": crm_team.id, "user_id": member.id}
            )

        self.assertEqual(events, ["binding_graph", "roster_projection"])

    def test_team_binding_is_tombstoned_but_uninstall_cleanup_is_allowed(self):
        binding = self.env["contact.center.crm.team.binding"].create(
            {
                "contact_center_team_id": self._cc_team().id,
                "crm_team_id": self._crm_team().id,
            }
        )
        with self.assertRaises(AccessError):
            binding.unlink()
        with self.assertRaises(AccessError):
            binding.with_user(self.admin_user).check_access_rights("unlink")
        with self.assertRaises(AccessError):
            binding.with_user(self.admin_user).with_context(
                module_uninstall=True
            ).unlink()
        binding.sudo().with_context(module_uninstall=True).unlink()
        self.assertFalse(binding.exists())

    def test_pipeline_binding_is_tombstoned_but_uninstall_cleanup_is_allowed(self):
        crm_team = self._crm_team()
        self.env["crm.stage"].create(
            {
                "name": "Pipeline lifecycle stage %s" % uuid.uuid4(),
                "team_id": crm_team.id,
            }
        )
        pipeline = self.env["contact.center.pipeline"].create(
            {
                "name": "Pipeline lifecycle %s" % uuid.uuid4(),
                "code": "pipeline-lifecycle-%s" % uuid.uuid4(),
                "company_id": self.env.company.id,
            }
        )
        binding = self.env["contact.center.crm.pipeline.binding"].create(
            {"pipeline_id": pipeline.id, "crm_team_id": crm_team.id}
        )

        with self.assertRaises(AccessError):
            binding.unlink()
        with self.assertRaises(AccessError):
            binding.with_user(self.admin_user).check_access_rights("unlink")
        with self.assertRaises(AccessError):
            binding.with_user(self.admin_user).with_context(
                module_uninstall=True
            ).unlink()
        binding.sudo().with_context(module_uninstall=True).unlink()

        self.assertFalse(binding.exists())

    def test_active_pipeline_binding_rejects_inactive_authorities(self):
        pipeline = self.env["contact.center.pipeline"].create(
            {
                "name": "Inactive CRM authority %s" % uuid.uuid4(),
                "code": "inactive-crm-authority-%s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "active": False,
            }
        )
        with self.assertRaises(ValidationError):
            self.env["contact.center.crm.pipeline.binding"].create(
                {"pipeline_id": pipeline.id, "crm_team_id": self._crm_team().id}
            )

    def test_inner_stage_catalog_sync_does_not_reacquire_binding_graph(self):
        crm_team = self._crm_team()
        crm_stage = self.env["crm.stage"].create(
            {
                "name": "Local authority stage %s" % uuid.uuid4(),
                "team_id": crm_team.id,
            }
        )
        pipeline = self.env["contact.center.pipeline"].create(
            {
                "name": "Catalog authority %s" % uuid.uuid4(),
                "code": "catalog-authority-%s" % uuid.uuid4(),
                "company_id": self.env.company.id,
            }
        )
        binding = self.env["contact.center.crm.pipeline.binding"].create(
            {"pipeline_id": pipeline.id, "crm_team_id": crm_team.id}
        )
        stage_binding = binding.stage_binding_ids.filtered(
            lambda item: item.crm_stage_id == crm_stage
        )
        self.assertEqual(len(stage_binding), 1)
        # Force the catalog repair branch to call StageBinding.write().  Doing
        # this through SQL is deliberate: its public ORM lifecycle forbids an
        # inconsistent active mapping, which is precisely what sync repairs.
        self.env.cr.execute(
            "UPDATE contact_center_crm_stage_binding SET active = FALSE "
            "WHERE id = %s",
            [stage_binding.id],
        )
        stage_binding.invalidate_recordset(["active"])
        self.assertFalse(stage_binding.active)
        original = binding_module.lock_binding_graph
        calls = []

        def record(*args, **kwargs):
            calls.append(kwargs)
            return original(*args, **kwargs)

        with mock.patch.object(
            binding_module, "lock_binding_graph", side_effect=record
        ):
            binding.action_sync_stage_catalog()

        self.assertEqual(len(calls), 1)
        stage_binding.invalidate_recordset(["active"])
        self.assertTrue(stage_binding.active)

    def test_overlapping_supervisor_and_agent_grants_retain_agent_role(self):
        replacement = self._user("replacement")
        second_leader = self._user("second-leader")
        first_crm_team = self._crm_team()
        second_crm_team = self.env["crm.team"].create(
            {
                "name": "Second CRM authority %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "user_id": second_leader.id,
            }
        )
        self.env["crm.team.member"].create(
            {"crm_team_id": second_crm_team.id, "user_id": self.user.id}
        )
        self.env["contact.center.crm.team.binding"].create(
            {
                "contact_center_team_id": self._cc_team().id,
                "crm_team_id": first_crm_team.id,
            }
        )
        self.env["contact.center.crm.team.binding"].create(
            {
                "contact_center_team_id": self._cc_team().id,
                "crm_team_id": second_crm_team.id,
            }
        )
        agent_group = self.env.ref("contact_center_base.group_contact_center_agent")
        supervisor_group = self.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        self.user.invalidate_recordset(["groups_id"])
        self.assertIn(agent_group, self.user.groups_id)
        self.assertIn(supervisor_group, self.user.groups_id)

        first_crm_team.write({"user_id": replacement.id})

        self.user.invalidate_recordset(["groups_id"])
        self.assertIn(agent_group, self.user.groups_id)
        self.assertNotIn(supervisor_group, self.user.groups_id)

    def test_unauthorized_group_write_is_rejected_before_aggregate_lock(self):
        supervisor_group = self.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        initial_group_ids = set(self.admin_user.groups_id.ids)
        target = self.admin_user.with_user(self.user)
        with mock.patch.object(
            binding_module,
            "lock_roster_aggregate",
            side_effect=AssertionError("privileged aggregate lock must not run"),
        ) as aggregate_lock, self.assertRaises(AccessError):
            target.write({"groups_id": [(4, supervisor_group.id)]})

        aggregate_lock.assert_not_called()
        self.admin_user.invalidate_recordset(["groups_id"])
        self.assertEqual(set(self.admin_user.groups_id.ids), initial_group_ids)

    def test_unauthorized_dynamic_group_write_precedes_aggregate_lock(self):
        supervisor_group = self.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        initial_group_ids = set(self.admin_user.groups_id.ids)
        target = self.admin_user.with_user(self.user)
        with mock.patch.object(
            binding_module,
            "lock_roster_aggregate",
            side_effect=AssertionError("privileged aggregate lock must not run"),
        ) as aggregate_lock, self.assertRaises(AccessError):
            target.write({"in_group_%s" % supervisor_group.id: True})

        aggregate_lock.assert_not_called()
        self.admin_user.invalidate_recordset(["groups_id"])
        self.assertEqual(set(self.admin_user.groups_id.ids), initial_group_ids)

    def test_other_company_admin_cannot_sync_team_roster(self):
        binding = self.env["contact.center.crm.team.binding"].create(
            {
                "contact_center_team_id": self._cc_team().id,
                "crm_team_id": self._crm_team().id,
            }
        )
        company, restricted_admin = self._admin_restricted_to_another_company()
        revision = binding.authority_revision

        with self.assertRaises(AccessError):
            binding.with_user(restricted_admin).with_context(
                allowed_company_ids=company.ids
            ).action_sync_roster()

        binding.invalidate_recordset(["authority_revision"])
        self.assertEqual(binding.authority_revision, revision)

    def test_other_company_admin_cannot_sync_stage_catalog(self):
        pipeline = self.env[
            "contact.center.pipeline"
        ]._contact_center_provision_default_pipeline(self.env.company)
        binding = self.env["contact.center.crm.pipeline.binding"].create(
            {
                "pipeline_id": pipeline.id,
                "crm_team_id": self._crm_team().id,
            }
        )
        company, restricted_admin = self._admin_restricted_to_another_company()
        revision = binding.authority_revision

        with self.assertRaises(AccessError):
            binding.with_user(restricted_admin).with_context(
                allowed_company_ids=company.ids
            ).action_sync_stage_catalog()

        binding.invalidate_recordset(["authority_revision"])
        self.assertEqual(binding.authority_revision, revision)


@tagged("-at_install", "post_install")
class TestCrmCatalogAuthorityConcurrency(TransactionCase):
    def _touch(self, team_id):
        with self.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = '5s'")
            env = api.Environment(cr, SUPERUSER_ID, {})
            try:
                binding_module.lock_catalog_authorities(env, [team_id])
                cr.commit()  # pylint: disable=invalid-commit
                return "done"
            except SerializationFailure:
                cr.rollback()
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            binding_module.lock_catalog_authorities(env, [team_id])
            cr.commit()  # pylint: disable=invalid-commit
        return "done-after-retry"

    def _worker(self, team_id, barrier, results, key):
        with self.registry.cursor() as cr:
            cr.execute("SELECT id FROM crm_team WHERE id = %s", [team_id])
            barrier.wait(timeout=10)
        results[key] = self._touch(team_id)

    def test_first_authority_concurrent_upsert_converges(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            team = env["crm.team"].create(
                {"name": "Concurrent catalog authority %s" % uuid.uuid4()}
            )
            team_id = team.id
            cr.commit()  # pylint: disable=invalid-commit
        barrier = threading.Barrier(2)
        results = {}
        workers = [
            threading.Thread(
                target=self._worker,
                args=(team_id, barrier, results, "worker-%s" % index),
            )
            for index in range(2)
        ]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(12)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(len(results), 2)
            self.assertTrue(all(value.startswith("done") for value in results.values()))
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                authority = (
                    env["contact.center.crm.catalog.authority"]
                    .sudo()
                    .search([("authority_key", "=", "team:%s" % team_id)])
                )
                self.assertEqual(len(authority), 1)
                self.assertGreaterEqual(authority.authority_revision, 2)
        finally:
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                env["crm.team"].sudo().with_context(active_test=False).browse(
                    team_id
                ).unlink()
                cr.commit()  # pylint: disable=invalid-commit


@tagged("-at_install", "post_install")
class TestCrmRosterAggregateConcurrency(TransactionCase):
    """Exercise manual role ownership against every roster mutation path."""

    WORKER_TIMEOUT_SECONDS = 15

    def _create_user(self, env, token, label):
        return (
            env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "CRM roster %s %s" % (label, token),
                    "login": "crm-roster-%s-%s" % (label, token),
                    "company_id": env.company.id,
                    "company_ids": [(6, 0, env.company.ids)],
                    "groups_id": [(6, 0, env.ref("base.group_user").ids)],
                }
            )
        )

    def _setup_fixture(self):
        token = uuid.uuid4().hex
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            leader = self._create_user(env, token, "leader")
            member = self._create_user(env, token, "member")
            replacement = self._create_user(env, token, "replacement")
            crm_team = env["crm.team"].create(
                {
                    "name": "CRM roster source %s" % token,
                    "company_id": env.company.id,
                    "user_id": leader.id,
                }
            )
            membership = env["crm.team.member"].create(
                {"crm_team_id": crm_team.id, "user_id": member.id}
            )
            replacement_team = env["crm.team"].create(
                {
                    "name": "CRM roster replacement %s" % token,
                    "company_id": env.company.id,
                    "user_id": replacement.id,
                }
            )
            pipeline = env[
                "contact.center.pipeline"
            ]._contact_center_provision_default_pipeline(env.company)
            team = env["contact.center.team"].create(
                {
                    "name": "CC roster aggregate %s" % token,
                    "company_id": env.company.id,
                    "pipeline_ids": [(6, 0, pipeline.ids)],
                    "default_pipeline_id": pipeline.id,
                }
            )
            binding = env["contact.center.crm.team.binding"].create(
                {
                    "contact_center_team_id": team.id,
                    "crm_team_id": crm_team.id,
                }
            )
            account = env["contact.center.account"].create(
                {
                    "name": "CRM roster aggregate %s" % token,
                    "company_id": env.company.id,
                    "platform": "whatsapp",
                    "external_ref": "crm-roster-%s" % token,
                    "default_team_id": team.id,
                    "default_pipeline_id": pipeline.id,
                }
            )
            agent_group = env.ref("contact_center_base.group_contact_center_agent")
            grant = (
                env["contact.center.crm.role.grant"]
                .sudo()
                .search(
                    [
                        ("binding_id", "=", binding.id),
                        ("user_id", "=", member.id),
                        ("group_id", "=", agent_group.id),
                    ]
                )
            )
            self.assertEqual(len(grant), 1)
            self.assertEqual(grant.state, "active")
            cr.commit()  # pylint: disable=invalid-commit
            return {
                "account_id": account.id,
                "binding_id": binding.id,
                "crm_member_id": membership.id,
                "crm_team_id": crm_team.id,
                "group_id": agent_group.id,
                "replacement_crm_team_id": replacement_team.id,
                "team_id": team.id,
                "user_ids": [leader.id, member.id, replacement.id],
                "member_id": member.id,
            }

    def _execute(self, env, fixture, operation):
        if operation == "manual":
            env["res.users"].browse(fixture["member_id"]).write(
                {"groups_id": [(4, fixture["group_id"])]}
            )
        elif operation == "archive":
            env["contact.center.crm.team.binding"].browse(fixture["binding_id"]).write(
                {"active": False}
            )
        elif operation == "repoint":
            env["contact.center.crm.team.binding"].browse(fixture["binding_id"]).write(
                {"crm_team_id": fixture["replacement_crm_team_id"]}
            )
        elif operation == "sync":
            env["contact.center.crm.team.binding"].browse(
                fixture["binding_id"]
            ).action_sync_roster()
        else:  # pragma: no cover - test programming error
            raise AssertionError("Unsupported roster operation")

    def _execute_with_retry(self, fixture, operation):
        for retry in range(3):
            with self.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '7s'")
                cr.execute("SET LOCAL statement_timeout = '12s'")
                env = api.Environment(cr, SUPERUSER_ID, {})
                try:
                    self._execute(env, fixture, operation)
                    cr.commit()  # pylint: disable=invalid-commit
                    return {"outcome": "done", "retries": retry}
                except SerializationFailure:
                    cr.rollback()
        return {"outcome": "serialization_exhausted", "retries": 3}

    def _worker(self, fixture, operation, barrier, results):
        try:
            with self.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '7s'")
                cr.execute("SET LOCAL statement_timeout = '12s'")
                cr.execute(
                    "SELECT id FROM contact_center_crm_role_grant "
                    "WHERE binding_id = %s ORDER BY id",
                    [fixture["binding_id"]],
                )
                barrier.wait(timeout=self.WORKER_TIMEOUT_SECONDS)
                env = api.Environment(cr, SUPERUSER_ID, {})
                try:
                    self._execute(env, fixture, operation)
                    cr.commit()  # pylint: disable=invalid-commit
                    results[operation] = {"outcome": "done", "retries": 0}
                except SerializationFailure:
                    cr.rollback()
                    result = self._execute_with_retry(fixture, operation)
                    result["retries"] += 1
                    results[operation] = result
        except Exception as error:  # pragma: no cover - asserted by parent thread
            results[operation] = {
                "outcome": "error",
                "class": error.__class__.__name__,
                "message": str(error),
            }

    def _run_concurrent(self, fixture, mutation):
        barrier = threading.Barrier(2)
        results = {}
        workers = [
            threading.Thread(
                target=self._worker,
                args=(fixture, operation, barrier, results),
            )
            for operation in ("manual", mutation)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(self.WORKER_TIMEOUT_SECONDS)
        self.assertTrue(all(not worker.is_alive() for worker in workers), results)
        self.assertEqual(set(results), {"manual", mutation})
        self.assertEqual(
            {result["outcome"] for result in results.values()}, {"done"}, results
        )

    def _assert_member_role(self, fixture, *, expected_grant_state):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            user = env["res.users"].browse(fixture["member_id"])
            group = env["res.groups"].browse(fixture["group_id"])
            grants = (
                env["contact.center.crm.role.grant"]
                .sudo()
                .search(
                    [
                        ("binding_id", "=", fixture["binding_id"]),
                        ("user_id", "=", fixture["member_id"]),
                        ("group_id", "=", fixture["group_id"]),
                    ]
                )
            )
            self.assertEqual(len(grants), 1)
            self.assertEqual(grants.state, expected_grant_state)
            self.assertFalse(grants.managed)
            self.assertIn(group, user.groups_id)
            self.assertLessEqual(
                len(grants.filtered(lambda item: item.state == "active")), 1
            )

    def _cleanup(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["contact.center.crm.team.binding"].sudo().with_context(
                active_test=False, module_uninstall=True
            ).browse(fixture["binding_id"]).exists().unlink()
            env["contact.center.account"].sudo().with_context(active_test=False).browse(
                fixture["account_id"]
            ).exists().unlink()
            env["contact.center.team"].sudo().with_context(active_test=False).browse(
                fixture["team_id"]
            ).exists().unlink()
            env["crm.team.member"].sudo().with_context(active_test=False).browse(
                fixture["crm_member_id"]
            ).exists().unlink()
            env["crm.team"].sudo().with_context(active_test=False).browse(
                [fixture["crm_team_id"], fixture["replacement_crm_team_id"]]
            ).exists().unlink()
            users = (
                env["res.users"]
                .sudo()
                .with_context(active_test=False)
                .browse(fixture["user_ids"])
                .exists()
            )
            partners = users.mapped("partner_id")
            users.unlink()
            partners.exists().unlink()
            cr.commit()  # pylint: disable=invalid-commit

    def _exercise(self, mutation, expected_grant_state):
        fixture = self._setup_fixture()
        try:
            self._run_concurrent(fixture, mutation)
            self._assert_member_role(fixture, expected_grant_state=expected_grant_state)
        finally:
            self._cleanup(fixture)

    def test_manual_group_edit_converges_with_binding_archive(self):
        self._exercise("archive", "released")

    def test_manual_group_edit_converges_with_binding_repoint(self):
        self._exercise("repoint", "released")

    def test_manual_group_edit_converges_with_roster_sync(self):
        self._exercise("sync", "active")
