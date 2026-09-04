import uuid
from contextlib import ExitStack
from unittest.mock import patch

from lxml import etree
from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from ..models import mapping_inventory as mapping_inventory_module


class TestCrmMappingInventory(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        admin_group = cls.env.ref("contact_center_base.group_contact_center_admin")
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        crm_manager_group = cls.env.ref("sales_team.group_sale_manager")
        crm_user_group = cls.env.ref("sales_team.group_sale_salesman")
        cls.mapping_admin = cls._create_user(
            "mapping-admin", admin_group | crm_manager_group
        )
        cls.contact_center_admin_only = cls._create_user(
            "mapping-cc-admin-only", admin_group
        )
        cls.operator = cls._create_user(
            "mapping-operator", agent_group | crm_user_group
        )
        cls.crm_team = cls.env["crm.team"].create(
            {
                "name": "CRM Candidate %s" % uuid.uuid4(),
                "company_id": cls.env.company.id,
            }
        )
        cls.crm_member = cls.env["crm.team.member"].create(
            {
                "crm_team_id": cls.crm_team.id,
                "user_id": cls.operator.id,
            }
        )

    @classmethod
    def _create_user(cls, label, groups):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": label,
                    "login": "{}-{}".format(label, uuid.uuid4()),
                    "email": "%s@example.invalid" % label,
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, groups.ids)],
                }
            )
        )

    def _team(self, name=None, users=None):
        return self.env["contact.center.team"].create(
            {
                "name": name or "Mapping Team %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "agent_ids": [(6, 0, (users or self.env["res.users"]).ids)],
            }
        )

    def _pipeline(self, name):
        pipeline = self.env["contact.center.pipeline"].create(
            {
                "name": name,
                "code": "mapping-%s" % uuid.uuid4(),
                "company_id": self.env.company.id,
            }
        )
        self.env["contact.center.pipeline.stage"].create(
            {
                "name": "Initial Mapping Stage",
                "code": "initial",
                "pipeline_id": pipeline.id,
                "is_initial": True,
            }
        )
        return pipeline

    def _inventory(self, include_inactive=False):
        inventory = (
            self.env["contact.center.crm.mapping.inventory"]
            .with_user(self.mapping_admin)
            .create(
                {
                    "company_id": self.env.company.id,
                    "include_inactive": include_inactive,
                }
            )
        )
        inventory.action_refresh()
        return inventory

    def _team_lines(self, inventory, team):
        return inventory.line_ids.filtered(
            lambda line: line.mapping_kind == "team"
            and line.contact_center_team_id == team
        )

    def _pipeline_lines(self, inventory, pipeline):
        return inventory.line_ids.filtered(
            lambda line: line.mapping_kind == "pipeline"
            and line.pipeline_id == pipeline
        )

    def test_zero_candidates_is_explicit(self):
        team = self._team()

        lines = self._team_lines(self._inventory(), team)

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.candidate_state, "no_candidate")
        self.assertFalse(lines.crm_team_id)
        self.assertEqual(lines.candidate_count, 0)
        self.assertFalse(lines.can_accept)

    def test_single_candidate_explains_roster_overlap(self):
        team = self._team(users=self.operator)

        lines = self._team_lines(self._inventory(), team)

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.crm_team_id, self.crm_team)
        self.assertEqual(lines.candidate_state, "candidate")
        self.assertEqual(lines.candidate_count, 1)
        self.assertIn("roster", lines.evidence.lower())
        self.assertTrue(lines.can_accept)

    def test_multiple_candidates_are_marked_ambiguous(self):
        team = self._team(users=self.operator)
        other_crm_team = self.env["crm.team"].create(
            {
                # A CRM user can have only one effective team membership in this
                # Odoo version.  Use the independent normalized-name signal so
                # the second candidate does not invalidate the original roster
                # candidate that this test intends to keep.
                "name": team.name,
                "company_id": self.env.company.id,
            }
        )

        lines = self._team_lines(self._inventory(), team)

        self.assertEqual(len(lines), 2)
        self.assertEqual(
            set(lines.mapped("crm_team_id").ids),
            {self.crm_team.id, other_crm_team.id},
        )
        self.assertEqual(set(lines.mapped("candidate_state")), {"ambiguous"})
        self.assertEqual(set(lines.mapped("candidate_count")), {2})

    def test_company_scope_excludes_cross_company_candidate(self):
        team = self._team(name="Shared Candidate Name", users=self.operator)
        other_company = self.env["res.company"].create(
            {"name": "Mapping Other Company %s" % uuid.uuid4()}
        )
        other_crm_team = (
            self.env["crm.team"]
            .sudo()
            .create(
                {
                    "name": team.name,
                    "company_id": other_company.id,
                }
            )
        )

        lines = self._team_lines(self._inventory(), team)

        self.assertNotIn(other_crm_team, lines.mapped("crm_team_id"))
        self.assertEqual(lines.mapped("crm_team_id"), self.crm_team)

    def test_admin_accept_is_explicit_and_idempotent(self):
        team = self._team(users=self.operator)
        line = self._team_lines(self._inventory(), team)

        action = line.with_user(self.mapping_admin).action_accept_candidate()
        binding = self.env["contact.center.crm.team.binding"].browse(action["res_id"])
        second_action = line.with_user(self.mapping_admin).action_accept_candidate()

        self.assertEqual(binding.contact_center_team_id, team)
        self.assertEqual(binding.crm_team_id, self.crm_team)
        self.assertTrue(binding.active)
        self.assertEqual(second_action["res_id"], binding.id)
        self.assertEqual(
            self.env["contact.center.crm.team.binding"].search_count(
                [("contact_center_team_id", "=", team.id)]
            ),
            1,
        )

    def test_archived_exact_binding_can_only_be_reactivated_explicitly(self):
        team = self._team(users=self.operator)
        binding = self.env["contact.center.crm.team.binding"].create(
            {
                "contact_center_team_id": team.id,
                "crm_team_id": self.crm_team.id,
            }
        )
        binding.write({"active": False})
        line = self._team_lines(self._inventory(), team)

        self.assertEqual(line.candidate_state, "reactivable")
        line.with_user(self.mapping_admin).action_accept_candidate()

        self.assertTrue(binding.active)
        self.assertEqual(
            self.env["contact.center.crm.team.binding"]
            .with_context(active_test=False)
            .search_count([("contact_center_team_id", "=", team.id)]),
            1,
        )

    def test_stale_snapshot_must_be_refreshed(self):
        team = self._team(users=self.operator)
        line = self._team_lines(self._inventory(), team)
        self.crm_team.write({"name": "Changed after inventory %s" % uuid.uuid4()})

        with self.assertRaisesRegex(ValidationError, "changed after the scan"):
            line.with_user(self.mapping_admin).action_accept_candidate()

        self.assertFalse(team.crm_team_id)

    def test_new_candidate_requires_rescan_before_acceptance(self):
        team = self._team(users=self.operator)
        inventory = self._inventory()
        line = self._team_lines(inventory, team)
        other_crm_team = self.env["crm.team"].create(
            {
                # Add a new candidate without mutating the roster evidence that
                # made the selected candidate valid at scan time.
                "name": team.name,
                "company_id": self.env.company.id,
            }
        )

        with self.assertRaisesRegex(ValidationError, "candidate set changed"):
            line.with_user(self.mapping_admin).action_accept_candidate()

        self.assertFalse(team.crm_team_id)
        inventory.action_refresh()
        self.assertEqual(
            set(self._team_lines(inventory, team).mapped("crm_team_id").ids),
            {self.crm_team.id, other_crm_team.id},
        )
        refreshed_line = self._team_lines(inventory, team).filtered(
            lambda candidate: candidate.crm_team_id == self.crm_team
        )
        self.assertEqual(refreshed_line.candidate_state, "ambiguous")
        refreshed_line.with_user(self.mapping_admin).action_accept_candidate()
        self.assertEqual(team.crm_team_id, self.crm_team)

    def test_candidate_that_becomes_invalid_is_rejected(self):
        team = self._team(users=self.operator)
        line = self._team_lines(self._inventory(), team)
        self.crm_team.write({"active": False})

        with self.assertRaisesRegex(ValidationError, "no longer valid"):
            line.with_user(self.mapping_admin).action_accept_candidate()

        self.assertFalse(team.crm_team_id)

    def test_inventory_requires_both_contact_center_and_crm_authority(self):
        inventory = (
            self.env["contact.center.crm.mapping.inventory"]
            .sudo()
            .create({"company_id": self.env.company.id})
        )

        with self.assertRaises(AccessError):
            inventory.with_user(self.operator).action_refresh()
        with self.assertRaisesRegex(AccessError, "CRM Sales Manager"):
            inventory.with_user(self.contact_center_admin_only).action_refresh()
        with self.assertRaisesRegex(AccessError, "CRM Sales Manager"):
            self.env["contact.center.crm.mapping.inventory"].with_user(
                self.contact_center_admin_only
            ).search([])

    def test_cross_company_binding_blocker_does_not_disclose_source(self):
        candidate_name = "Global CRM Candidate %s" % uuid.uuid4()
        global_crm_team = (
            self.env["crm.team"]
            .sudo()
            .create({"name": candidate_name, "company_id": False})
        )
        other_company = (
            self.env["res.company"]
            .sudo()
            .create({"name": "Private Mapping Company %s" % uuid.uuid4()})
        )
        private_team_name = "Private Team %s" % uuid.uuid4()
        private_team = (
            self.env["contact.center.team"]
            .sudo()
            .create({"name": private_team_name, "company_id": other_company.id})
        )
        self.env["contact.center.crm.team.binding"].sudo().create(
            {
                "contact_center_team_id": private_team.id,
                "crm_team_id": global_crm_team.id,
            }
        )
        local_team = self._team(name=candidate_name)

        line = self._team_lines(self._inventory(), local_team).filtered(
            lambda candidate: candidate.crm_team_id == global_crm_team
        )

        self.assertEqual(line.candidate_state, "blocked")
        self.assertIn("outside the active company scope", line.blockers)
        self.assertNotIn(private_team_name, line.blockers)

    def test_acceptance_locks_binding_graph_before_contact_center_source(self):
        team = self._team(users=self.operator)
        line = self._team_lines(self._inventory(), team)
        calls = []
        line_class = type(line)
        original_graph = mapping_inventory_module.lock_binding_graph

        def lock_graph(*args, **kwargs):
            calls.append("binding_graph")
            return original_graph(*args, **kwargs)

        with patch.object(
            mapping_inventory_module,
            "lock_binding_graph",
            side_effect=lock_graph,
        ), patch.object(
            line_class,
            "_lock_source",
            autospec=True,
            side_effect=lambda record: calls.append("source"),
        ):
            line._lock_authorities()

        self.assertEqual(calls, ["binding_graph", "source"])

    def test_integrity_race_returns_concurrently_created_binding(self):
        team = self._team(users=self.operator)
        line = self._team_lines(self._inventory(), team)
        binding = self.env["contact.center.crm.team.binding"].create(
            {
                "contact_center_team_id": team.id,
                "crm_team_id": self.crm_team.id,
            }
        )
        line_class = type(line)

        with patch.object(
            line_class,
            "_create_binding",
            autospec=True,
            side_effect=IntegrityError,
        ):
            result = line._create_binding_idempotently()

        self.assertEqual(result, binding)

    def test_acceptance_revalidates_then_fences_before_create(self):
        pipeline = self._pipeline(self.crm_team.name)
        line = self._pipeline_lines(self._inventory(), pipeline).with_user(
            self.mapping_admin
        )
        calls = []
        line_class = type(line)
        original_revalidate = line._revalidate_candidate
        original_barrier = line._write_acceptance_barrier
        original_create = line._create_binding_idempotently

        def revalidate(record):
            calls.append("revalidate")
            return original_revalidate()

        def barrier(record):
            calls.append("barrier")
            return original_barrier()

        def create(record):
            calls.append("create")
            return original_create()

        with patch.object(
            line_class,
            "_revalidate_candidate",
            autospec=True,
            side_effect=revalidate,
        ), patch.object(
            line_class,
            "_write_acceptance_barrier",
            autospec=True,
            side_effect=barrier,
        ), patch.object(
            line_class,
            "_create_binding_idempotently",
            autospec=True,
            side_effect=create,
        ):
            line.action_accept_candidate()

        self.assertEqual(calls, ["revalidate", "barrier", "create"])

    def test_pipeline_candidate_reports_catalog_and_accepts_explicitly(self):
        pipeline = self._pipeline(self.crm_team.name)
        inventory = self._inventory()
        lines = self._pipeline_lines(inventory, pipeline)

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.crm_team_id, self.crm_team)
        self.assertEqual(lines.candidate_state, "candidate")

        action = lines.with_user(self.mapping_admin).action_accept_candidate()
        binding = self.env["contact.center.crm.pipeline.binding"].browse(
            action["res_id"]
        )
        self.assertEqual(binding.pipeline_id, pipeline)
        self.assertEqual(binding.crm_team_id, self.crm_team)
        self.assertTrue(binding.stage_binding_ids)
        second_action = lines.with_user(self.mapping_admin).action_accept_candidate()
        self.assertEqual(second_action["res_id"], binding.id)
        self.assertEqual(
            self.env["contact.center.crm.pipeline.binding"].search_count(
                [("pipeline_id", "=", pipeline.id)]
            ),
            1,
        )

    def test_pipeline_snapshot_tracks_archived_crm_stage_changes(self):
        pipeline = self._pipeline(self.crm_team.name)
        self.env["crm.stage"].create(
            {
                "name": "Active inventory stage %s" % uuid.uuid4(),
                "team_id": self.crm_team.id,
            }
        )
        archived_stage = self.env["crm.stage"].create(
            {
                "name": "Archived inventory stage %s" % uuid.uuid4(),
                "team_id": self.crm_team.id,
            }
        )
        archived_stage.action_archive()
        line = self._pipeline_lines(self._inventory(), pipeline)

        archived_stage.write({"name": "Changed archived stage %s" % uuid.uuid4()})

        with self.assertRaisesRegex(ValidationError, "changed after the scan"):
            line.with_user(self.mapping_admin).action_accept_candidate()

    def test_refresh_batches_candidate_dependency_searches(self):
        self._team(name=self.crm_team.name, users=self.operator)
        pipeline = self._pipeline(self.crm_team.name)
        self.env["contact.center.crm.pipeline.binding"].create(
            {"pipeline_id": pipeline.id, "crm_team_id": self.crm_team.id}
        )
        for index in range(2):
            self.env["crm.team"].create(
                {
                    "name": "%s batch %s" % (self.crm_team.name, index),
                    "company_id": self.env.company.id,
                }
            )
        inventory = (
            self.env["contact.center.crm.mapping.inventory"]
            .with_user(self.mapping_admin)
            .create({"company_id": self.env.company.id})
        )
        model_names = (
            "contact.center.crm.team.binding",
            "contact.center.crm.pipeline.binding",
            "crm.team.member",
            "crm.stage",
            "contact.center.pipeline.stage",
            "contact.center.crm.stage.binding",
            "contact.center.case",
            "contact.center.account",
        )
        search_counts = {model_name: 0 for model_name in model_names}

        with ExitStack() as stack:
            for model_name in model_names:
                model_class = type(self.env[model_name])
                original_search = model_class.search

                def counted_search(
                    recordset,
                    *args,
                    _model_name=model_name,
                    _original_search=original_search,
                    **kwargs,
                ):
                    search_counts[_model_name] += 1
                    return _original_search(recordset, *args, **kwargs)

                stack.enter_context(
                    patch.object(
                        model_class,
                        "search",
                        autospec=True,
                        side_effect=counted_search,
                    )
                )
            inventory.action_refresh()

        self.assertEqual(
            search_counts,
            {model_name: 1 for model_name in model_names},
        )

    def test_snapshot_validity_recomputes_each_source_once(self):
        team = self._team(name=self.crm_team.name)
        other_crm_team = self.env["crm.team"].create(
            {
                "name": self.crm_team.name,
                "company_id": self.env.company.id,
            }
        )
        inventory = self._inventory()
        lines = self._team_lines(inventory, team).filtered(
            lambda line: line.crm_team_id in (self.crm_team | other_crm_team)
        )
        self.assertEqual(len(lines), 2)
        inventory_class = type(inventory)
        original_snapshot = inventory_class._candidate_set_snapshot
        calls = []

        def candidate_set_snapshot(recordset, kind, source, *args, **kwargs):
            calls.append((kind, source.id, kwargs.get("snapshot_context")))
            return original_snapshot(recordset, kind, source, *args, **kwargs)

        with patch.object(
            inventory_class,
            "_candidate_set_snapshot",
            autospec=True,
            side_effect=candidate_set_snapshot,
        ):
            lines._compute_snapshot_validity()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ("team", team.id))
        self.assertIsNotNone(calls[0][2])
        self.assertEqual(set(lines.mapped("snapshot_validity")), {"current"})

    def test_pipeline_snapshot_ignores_other_source_binding(self):
        pipeline = self._pipeline(self.crm_team.name)
        line = self._pipeline_lines(self._inventory(), pipeline).filtered(
            lambda candidate: candidate.crm_team_id == self.crm_team
        )
        other_pipeline = self._pipeline("Unrelated pipeline %s" % uuid.uuid4())
        self.env["contact.center.crm.pipeline.binding"].create(
            {
                "pipeline_id": other_pipeline.id,
                "crm_team_id": self.crm_team.id,
            }
        )

        current = line._current_payload()

        self.assertEqual(current["snapshot_hash"], line.snapshot_hash)
        line.invalidate_recordset(["snapshot_validity", "can_accept"])
        self.assertEqual(line.snapshot_validity, "current")
        action = line.with_user(self.mapping_admin).action_accept_candidate()
        binding = self.env["contact.center.crm.pipeline.binding"].browse(
            action["res_id"]
        )
        self.assertEqual(binding.pipeline_id, pipeline)
        self.assertEqual(binding.crm_team_id, self.crm_team)

    def test_crm_stage_search_exposes_archived_filter(self):
        view = self.env.ref("contact_center_crm.view_crm_stage_search_contact_center")

        self.assertEqual(
            view.inherit_id,
            self.env.ref("crm.crm_lead_stage_search"),
        )
        arch = etree.fromstring(view.arch_db.encode())
        filters = arch.xpath("//filter[@name='contact_center_archived']")
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0].get("domain"), "[('active', '=', False)]")
