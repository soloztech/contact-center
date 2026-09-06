import uuid
from contextlib import ExitStack
from unittest import mock

from odoo import _
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase


class TestContactCenterCrm(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cc_agent = cls.env.ref("contact_center_base.group_contact_center_agent")
        crm_user = cls.env.ref("sales_team.group_sale_salesman")
        cls.user_a = cls._create_user("A", cc_agent | crm_user)
        cls.user_b = cls._create_user("B", cc_agent | crm_user)
        cls.admin_user = cls._create_user(
            "Admin",
            cls.env.ref("contact_center_base.group_contact_center_admin")
            | cls.env.ref("sales_team.group_sale_manager"),
        )

        cls.pipeline = cls.env[
            "contact.center.pipeline"
        ]._contact_center_provision_default_pipeline(cls.env.company)
        cls.crm_team = cls.env["crm.team"].create(
            {
                "name": "CRM Bridge %s" % uuid.uuid4(),
                "company_id": cls.env.company.id,
                "user_id": cls.user_a.id,
            }
        )
        cls.crm_member_b = cls.env["crm.team.member"].create(
            {
                "crm_team_id": cls.crm_team.id,
                "user_id": cls.user_b.id,
            }
        )
        cls.crm_stage_a = cls.env["crm.stage"].create(
            {
                "name": "Bridge Qualified %s" % uuid.uuid4(),
                "sequence": 101,
                "team_id": cls.crm_team.id,
            }
        )
        cls.crm_stage_b = cls.env["crm.stage"].create(
            {
                "name": "Bridge Proposal %s" % uuid.uuid4(),
                "sequence": 102,
                "team_id": cls.crm_team.id,
            }
        )
        cls.pipeline_binding = cls.env["contact.center.crm.pipeline.binding"].create(
            {
                "pipeline_id": cls.pipeline.id,
                "crm_team_id": cls.crm_team.id,
            }
        )
        cls.stage_a = cls._core_stage(cls.crm_stage_a)
        cls.stage_b = cls._core_stage(cls.crm_stage_b)

        cls.team_a = cls.env["contact.center.team"].create(
            {
                "name": "CC CRM Team A %s" % uuid.uuid4(),
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.user_a.ids)],
                "pipeline_ids": [(6, 0, cls.pipeline.ids)],
                "default_pipeline_id": cls.pipeline.id,
            }
        )
        cls.team_binding_a = cls.env["contact.center.crm.team.binding"].create(
            {
                "contact_center_team_id": cls.team_a.id,
                "crm_team_id": cls.crm_team.id,
            }
        )

        cls.team_account = cls._create_account("Shared CRM Inbox", team=cls.team_a)
        cls.owner_account_a = cls._create_account(
            "Exclusive CRM Inbox A", owner=cls.user_a
        )
        cls.owner_account_b = cls._create_account(
            "Exclusive CRM Inbox B", owner=cls.user_b
        )

    @classmethod
    def _create_user(cls, suffix, groups):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "CC CRM User %s" % suffix,
                    "login": "cc-crm-%s-%s" % (suffix.lower(), uuid.uuid4()),
                    "email": "cc-crm-%s@example.invalid" % suffix.lower(),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, groups.ids)],
                }
            )
        )

    @classmethod
    def _create_account(cls, name, *, team=None, owner=None, pipeline=None):
        pipeline = pipeline or cls.pipeline
        return cls.env["contact.center.account"].create(
            {
                "name": "%s %s" % (name, uuid.uuid4()),
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": str(uuid.uuid4()),
                "default_team_id": team.id if team else False,
                "owner_user_id": owner.id if owner else False,
                "default_pipeline_id": pipeline.id,
            }
        )

    @classmethod
    def _create_channel(cls, account, name, partner=None):
        guest = cls.env["mail.guest"].sudo().create({"name": name})
        identity = (
            cls.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": name,
                    "company_id": cls.env.company.id,
                    "mail_guest_id": guest.id,
                    "partner_id": partner.id if partner else False,
                    "partner_link_kind": "person" if partner else False,
                }
            )
        )
        channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=account,
            identity=identity,
            guest_ids=guest.ids,
        )
        cls.env["contact.center.channel.binding"].sudo().create(
            {
                "channel_id": channel.id,
                "account_id": account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": str(uuid.uuid4()),
            }
        )
        return channel

    @classmethod
    def _core_stage(cls, crm_stage):
        binding = cls.env["contact.center.crm.stage.binding"].search(
            [
                ("pipeline_binding_id", "=", cls.pipeline_binding.id),
                ("crm_stage_id", "=", crm_stage.id),
                ("active", "=", True),
            ]
        )
        assert len(binding) == 1
        return binding.stage_id

    def _default_case(self, channel):
        return channel.contact_center_case_ids.filtered("is_default")

    def test_two_cases_in_one_conversation_keep_two_leads_and_stages(self):
        channel = self._create_channel(self.team_account, "Two proposals")
        first = self._default_case(channel)
        first.with_user(self.user_a).action_transition(self.stage_a.id)
        second = (
            self.env["contact.center.case"]
            .with_user(self.user_a)
            .create(
                {
                    "name": "Second proposal",
                    "channel_id": channel.id,
                    "pipeline_id": self.pipeline.id,
                    "stage_id": self.stage_b.id,
                }
            )
        )

        first.with_user(self.user_a).action_create_crm_lead()
        second.with_user(self.user_a).action_create_crm_lead()
        first.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        second.invalidate_recordset(["crm_link_ids", "crm_lead_id"])

        self.assertNotEqual(first.crm_lead_id, second.crm_lead_id)
        self.assertEqual(first.crm_lead_id.stage_id, self.crm_stage_a)
        self.assertEqual(second.crm_lead_id.stage_id, self.crm_stage_b)

        first.with_user(self.user_a).action_transition(self.stage_b.id)
        self.assertEqual(first.crm_lead_id.stage_id, self.crm_stage_b)
        self.assertEqual(second.crm_lead_id.stage_id, self.crm_stage_b)
        self.assertEqual(second.stage_id, self.stage_b)

    def test_case_link_is_created_only_through_explicit_action(self):
        case = self._default_case(
            self._create_channel(self.team_account, "Explicit CRM link")
        )
        case.with_user(self.user_a).action_transition(self.stage_a.id)
        lead = self.env["crm.lead"].create(
            {
                "name": "Explicit existing lead",
                "company_id": self.env.company.id,
                "team_id": self.crm_team.id,
                "stage_id": self.crm_stage_a.id,
                "user_id": False,
            }
        )

        with self.assertRaises(AccessError):
            self.env["contact.center.crm.case.link"].with_user(self.user_a).create(
                {"case_id": case.id, "lead_id": lead.id, "origin": "linked"}
            )
        case.with_user(self.user_a).action_link_crm_lead(lead.id)
        case.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        self.assertEqual(case.crm_lead_id, lead)

    def test_native_lead_unlink_tombstones_bridge_and_case_can_relink(self):
        case = self._default_case(
            self._create_channel(self.team_account, "Deleted native CRM lead")
        )
        case.with_user(self.user_a).action_transition(self.stage_a.id)
        case.with_user(self.user_a).action_create_crm_lead()
        case.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        lead = case.crm_lead_id
        original_name = lead.display_name
        original_id = lead.id
        link = case.sudo().crm_link_ids
        open_wizard = self.env["contact.center.crm.link.wizard"].create(
            {"case_id": case.id, "lead_id": lead.id}
        )

        lead.with_user(self.admin_user).unlink()

        self.assertTrue(case.exists())
        self.assertFalse(open_wizard.exists())
        case.invalidate_recordset(["crm_link_ids", "crm_lead_id", "crm_lead_count"])
        link.invalidate_recordset(
            [
                "lead_id",
                "lead_company_id",
                "lead_name_snapshot",
                "lead_record_id_snapshot",
                "lead_team_id",
                "lead_user_id",
                "state",
                "unlinked_at",
                "unlinked_by_id",
                "unlinked_reason",
            ]
        )
        self.assertFalse(case.crm_lead_id)
        self.assertEqual(case.crm_lead_count, 0)
        self.assertFalse(link.lead_id)
        self.assertFalse(link.lead_company_id)
        self.assertFalse(link.lead_team_id)
        self.assertFalse(link.lead_user_id)
        self.assertEqual(link.state, "unlinked")
        self.assertEqual(link.lead_name_snapshot, original_name)
        self.assertEqual(link.lead_record_id_snapshot, original_id)
        self.assertTrue(link.unlinked_at)
        self.assertEqual(link.unlinked_by_id, self.admin_user)
        self.assertEqual(link.unlinked_reason, "lead_deleted")

        case.with_user(self.user_a).action_create_crm_lead()
        case.invalidate_recordset(["crm_link_ids", "crm_lead_id", "crm_lead_count"])
        self.assertTrue(case.crm_lead_id)
        self.assertEqual(case.crm_lead_count, 1)
        self.assertEqual(len(case.crm_link_ids), 2)
        self.assertEqual(
            case.crm_link_ids.filtered(lambda item: item.state == "unlinked"), link
        )

    def test_native_lead_unlink_rejects_before_bridge_tombstone(self):
        case = self._default_case(
            self._create_channel(self.team_account, "Rejected native CRM deletion")
        )
        case.with_user(self.user_a).action_transition(self.stage_a.id)
        case.with_user(self.user_a).action_create_crm_lead()
        case.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        lead = case.crm_lead_id
        link = case.sudo().crm_link_ids
        outsider = self._create_user(
            "NoCrmDelete",
            self.env.ref("base.group_user"),
        )

        with self.assertRaises(AccessError):
            lead.with_user(outsider).unlink()

        link.invalidate_recordset(["lead_id", "state", "unlinked_at"])
        self.assertTrue(lead.exists())
        self.assertEqual(link.state, "active")
        self.assertEqual(link.lead_id, lead)
        self.assertFalse(link.unlinked_at)

    def test_manual_unlink_tombstones_bridge_and_preserves_history(self):
        case = self._default_case(
            self._create_channel(self.team_account, "Manual CRM unlink")
        )
        case.with_user(self.user_a).action_transition(self.stage_a.id)
        case.with_user(self.user_a).action_create_crm_lead()
        case.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        link = case.crm_link_ids
        lead = case.crm_lead_id
        original_id = lead.id

        case.with_user(self.user_a).action_unlink_crm_lead()

        link.invalidate_recordset(
            [
                "lead_id",
                "lead_record_id_snapshot",
                "state",
                "unlinked_at",
                "unlinked_by_id",
                "unlinked_reason",
            ]
        )
        self.assertFalse(link.lead_id)
        self.assertEqual(link.lead_record_id_snapshot, original_id)
        self.assertEqual(link.state, "unlinked")
        self.assertEqual(link.unlinked_reason, "manual")
        self.assertEqual(link.unlinked_by_id, self.user_a)
        self.assertTrue(link.unlinked_at)
        self.assertTrue(lead.exists())
        with self.assertRaises(AccessError):
            link.sudo().unlink()

    def test_case_link_forged_uninstall_context_cannot_delete_evidence(self):
        case = self._default_case(
            self._create_channel(self.team_account, "Forged CRM unlink context")
        )
        case.with_user(self.user_a).action_transition(self.stage_a.id)
        case.with_user(self.user_a).action_create_crm_lead()
        link = case.sudo()._crm_link_sudo()

        with self.assertRaises(AccessError):
            link.with_user(self.admin_user).check_access_rights("unlink")
        with self.assertRaises(AccessError):
            link.with_user(self.admin_user).with_context(module_uninstall=True).unlink()

        self.assertTrue(link.exists())
        link.sudo().with_context(module_uninstall=True).unlink()
        self.assertFalse(link.exists())

    def test_create_lead_rechecks_original_user_after_graph_fence(self):
        case = self._default_case(
            self._create_channel(self.team_account, "CRM create authorization fence")
        ).with_user(self.user_a)
        case.action_transition(self.stage_a.id)
        case_type = type(case)
        original_check = case_type._check_crm_action_access
        calls = []

        def fail_after_wait(record, *args, **kwargs):
            calls.append(record.env.uid)
            if len(calls) == 2:
                raise AccessError(_("simulated concurrent authorization revocation"))
            return original_check(record, *args, **kwargs)

        leads_before = self.env["crm.lead"].search_count([])
        with mock.patch.object(
            case_type,
            "_check_crm_action_access",
            autospec=True,
            side_effect=fail_after_wait,
        ), self.assertRaisesRegex(AccessError, "authorization revocation"):
            case.action_create_crm_lead()

        self.assertEqual(calls, [self.user_a.id, self.user_a.id])
        self.assertEqual(self.env["crm.lead"].search_count([]), leads_before)
        self.assertFalse(case.sudo()._crm_link_sudo())

    def test_manual_unlink_rechecks_original_user_after_graph_fence(self):
        case = self._default_case(
            self._create_channel(self.team_account, "CRM unlink authorization fence")
        )
        case.with_user(self.user_a).action_transition(self.stage_a.id)
        case.with_user(self.user_a).action_create_crm_lead()
        link = case.sudo()._crm_link_sudo().with_user(self.user_a)
        link_type = type(link.case_id)
        original_check = link_type._check_crm_action_access
        calls = []

        def fail_after_wait(record, *args, **kwargs):
            calls.append(record.env.uid)
            if len(calls) == 2:
                raise AccessError(_("simulated concurrent authorization revocation"))
            return original_check(record, *args, **kwargs)

        with mock.patch.object(
            link_type,
            "_check_crm_action_access",
            autospec=True,
            side_effect=fail_after_wait,
        ), self.assertRaisesRegex(AccessError, "authorization revocation"):
            link.action_unlink()

        link.invalidate_recordset(["lead_id", "state"])
        self.assertEqual(calls, [self.user_a.id, self.user_a.id])
        self.assertEqual(link.state, "active")
        self.assertTrue(link.lead_id)

    def test_case_with_live_crm_link_cannot_be_archived(self):
        case = self._default_case(
            self._create_channel(self.team_account, "CRM archive guard")
        )
        case.with_user(self.user_a).action_transition(self.stage_a.id)
        case.with_user(self.user_a).action_create_crm_lead()

        with self.assertRaisesRegex(ValidationError, "CRM"):
            case.with_user(self.user_a).action_archive()

        self.assertTrue(case.active)

    def test_native_compatible_lead_merge_transfers_all_case_bridges(self):
        case_a = self._default_case(
            self._create_channel(self.team_account, "CRM merge case A")
        )
        case_b = self._default_case(
            self._create_channel(self.team_account, "CRM merge case B")
        )
        for case in case_a | case_b:
            case.with_user(self.user_a).action_transition(self.stage_a.id)
            case.with_user(self.user_a).action_create_crm_lead()
        (case_a | case_b).invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        leads = case_a.crm_lead_id | case_b.crm_lead_id
        original_lead_by_case = {
            case_a.id: case_a.sudo().crm_link_ids.original_lead_record_id_snapshot,
            case_b.id: case_b.sudo().crm_link_ids.original_lead_record_id_snapshot,
        }

        survivor = leads._merge_opportunity()

        self.assertTrue(survivor.exists())
        self.assertFalse((leads - survivor).exists())
        links = (
            self.env["contact.center.crm.case.link"]
            .sudo()
            .search(
                [
                    ("case_id", "in", (case_a | case_b).ids),
                    ("state", "=", "active"),
                ]
            )
        )
        self.assertEqual(len(links), 2)
        self.assertEqual(links.mapped("lead_id"), survivor)
        self.assertCountEqual(links.mapped("case_id").ids, (case_a | case_b).ids)
        for link in links:
            self.assertEqual(
                link.original_lead_record_id_snapshot,
                original_lead_by_case[link.case_id.id],
            )
            self.assertEqual(link.lead_record_id_snapshot, survivor.id)

    def test_native_incompatible_lead_merge_rolls_back_atomically(self):
        case = self._default_case(
            self._create_channel(self.team_account, "Rejected CRM merge")
        )
        case.with_user(self.user_a).action_transition(self.stage_a.id)
        case.with_user(self.user_a).action_create_crm_lead()
        case.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        linked_lead = case.crm_lead_id
        link = case.sudo().crm_link_ids
        other_team = self.env["crm.team"].create(
            {
                "name": "Incompatible merge team %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
            }
        )
        other_stage = self.env["crm.stage"].create(
            {
                "name": "Incompatible merge stage %s" % uuid.uuid4(),
                "team_id": other_team.id,
            }
        )
        other_lead = self.env["crm.lead"].create(
            {
                "name": "Incompatible merge survivor candidate",
                "company_id": self.env.company.id,
                "team_id": other_team.id,
                "stage_id": other_stage.id,
            }
        )

        with self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                (linked_lead | other_lead)._merge_opportunity(team_id=other_team.id)

        self.env.invalidate_all()
        self.assertTrue(linked_lead.exists())
        self.assertTrue(other_lead.exists())
        link.invalidate_recordset(["lead_id", "state"])
        self.assertEqual(link.lead_id, linked_lead)
        self.assertEqual(link.state, "active")

    def test_crm_team_binding_is_one_to_one(self):
        other_team = self.env["contact.center.team"].create(
            {
                "name": "Duplicate CRM roster target %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "agent_ids": [(6, 0, self.user_b.ids)],
                "pipeline_ids": [(6, 0, self.pipeline.ids)],
                "default_pipeline_id": self.pipeline.id,
            }
        )
        with self.assertRaisesRegex(ValidationError, "only one Contact Center team"):
            self.env["contact.center.crm.team.binding"].create(
                {
                    "contact_center_team_id": other_team.id,
                    "crm_team_id": self.crm_team.id,
                }
            )
        self.assertFalse(other_team.crm_team_id)

    def test_active_binding_projects_leader_and_active_members(self):
        supervisor_group = self.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        agent_group = self.env.ref("contact_center_base.group_contact_center_agent")

        self.assertEqual(self.team_a.supervisor_ids, self.user_a)
        self.assertEqual(self.team_a.agent_ids, self.user_b)
        self.assertIn(supervisor_group, self.user_a.groups_id)
        self.assertIn(agent_group, self.user_b.groups_id)

    def test_crm_roster_revision_is_internal_and_fences_binding_deactivation(self):
        revision = self.crm_team.contact_center_roster_revision
        with self.assertRaises(AccessError):
            self.crm_team.write({"contact_center_roster_revision": revision + 1})

        self.team_binding_a.write({"active": False})

        self.crm_team.invalidate_recordset(["contact_center_roster_revision"])
        self.assertGreater(self.crm_team.contact_center_roster_revision, revision)

    def test_leader_membership_is_classified_only_as_supervisor(self):
        self.env["crm.team.member"].create(
            {"crm_team_id": self.crm_team.id, "user_id": self.user_a.id}
        )

        self.assertIn(self.user_a, self.team_a.supervisor_ids)
        self.assertNotIn(self.user_a, self.team_a.agent_ids)
        self.assertIn(self.user_b, self.team_a.agent_ids)

    def test_member_addition_grants_role_and_existing_inbox_scope(self):
        crm_user = self.env.ref("sales_team.group_sale_salesman")
        new_user = self._create_user("NewAgent", crm_user)
        channel = self._create_channel(self.team_account, "Roster addition")

        self.env["crm.team.member"].create(
            {
                "crm_team_id": self.crm_team.id,
                "user_id": new_user.id,
            }
        )

        self.assertIn(
            self.env.ref("contact_center_base.group_contact_center_agent"),
            new_user.groups_id,
        )
        self.assertIn(new_user, self.team_a.agent_ids)
        channel.invalidate_recordset(["channel_member_ids"])
        self.assertIn(new_user.partner_id, channel.sudo().channel_member_ids.partner_id)
        self.assertEqual(
            self.env["contact.center.account"]
            .with_user(new_user)
            .search([("id", "=", self.team_account.id)]),
            self.team_account,
        )

    def test_crm_member_ids_inverse_is_also_authoritative(self):
        crm_user = self.env.ref("sales_team.group_sale_salesman")
        replacement = self._create_user("InverseMember", crm_user)

        self.crm_team.write({"member_ids": [(6, 0, replacement.ids)]})

        self.assertEqual(self.team_a.agent_ids, replacement)
        self.assertEqual(self.team_a.supervisor_ids, self.user_a)
        self.assertIn(
            self.env.ref("contact_center_base.group_contact_center_agent"),
            replacement.groups_id,
        )
        self.crm_member_b.invalidate_recordset(["active"])
        self.assertFalse(self.crm_member_b.active)

    def test_member_unlink_revokes_scope_but_keeps_preexisting_role_capability(self):
        agent_group = self.env.ref("contact_center_base.group_contact_center_agent")
        channel = self._create_channel(self.team_account, "Roster removal")
        self.assertIn(
            self.user_b.partner_id, channel.sudo().channel_member_ids.partner_id
        )

        self.crm_member_b.unlink()

        self.assertNotIn(self.user_b, self.team_a.agent_ids)
        self.assertIn(agent_group, self.user_b.groups_id)
        channel.invalidate_recordset(["channel_member_ids"])
        self.assertNotIn(
            self.user_b.partner_id, channel.sudo().channel_member_ids.partner_id
        )
        self.assertFalse(
            self.env["contact.center.account"]
            .with_user(self.user_b)
            .search([("id", "=", self.team_account.id)])
        )

    def test_member_unlink_revokes_role_introduced_by_bridge(self):
        crm_user = self.env.ref("sales_team.group_sale_salesman")
        agent_group = self.env.ref("contact_center_base.group_contact_center_agent")
        managed_user = self._create_user("ManagedRole", crm_user)
        membership = self.env["crm.team.member"].create(
            {"crm_team_id": self.crm_team.id, "user_id": managed_user.id}
        )
        self.assertIn(agent_group, managed_user.groups_id)

        membership.unlink()

        managed_user.invalidate_recordset(["groups_id"])
        self.assertNotIn(agent_group, managed_user.groups_id)

    def test_explicit_manual_role_is_never_revoked_by_bridge(self):
        crm_user = self.env.ref("sales_team.group_sale_salesman")
        agent_group = self.env.ref("contact_center_base.group_contact_center_agent")
        managed_user = self._create_user("ManualRole", crm_user)
        membership = self.env["crm.team.member"].create(
            {"crm_team_id": self.crm_team.id, "user_id": managed_user.id}
        )
        managed_user.write({"groups_id": [(4, agent_group.id)]})

        membership.unlink()

        managed_user.invalidate_recordset(["groups_id"])
        self.assertIn(agent_group, managed_user.groups_id)

    def test_reified_user_form_role_is_never_revoked_by_bridge(self):
        crm_user = self.env.ref("sales_team.group_sale_salesman")
        agent_group = self.env.ref("contact_center_base.group_contact_center_agent")
        managed_user = self._create_user("ReifiedManualRole", crm_user)
        membership = self.env["crm.team.member"].create(
            {"crm_team_id": self.crm_team.id, "user_id": managed_user.id}
        )

        field_names = managed_user.fields_get().keys()
        direct_field = "in_group_%s" % agent_group.id
        if direct_field in field_names:
            field_name = direct_field
            field_value = True
        else:
            field_name = next(
                name
                for name in field_names
                if name.startswith("sel_groups_")
                and str(agent_group.id) in name.split("_")[2:]
            )
            field_value = agent_group.id
        managed_user.write({field_name: field_value})

        membership.unlink()

        managed_user.invalidate_recordset(["groups_id"])
        self.assertIn(agent_group, managed_user.groups_id)

    def test_leader_change_grants_supervisor_and_revokes_previous_scope(self):
        crm_user = self.env.ref("sales_team.group_sale_salesman")
        new_leader = self._create_user("NewLeader", crm_user)
        supervisor_group = self.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )

        self.crm_team.write({"user_id": new_leader.id})

        self.assertEqual(self.team_a.supervisor_ids, new_leader)
        self.assertEqual(self.team_a.agent_ids, self.user_b)
        self.assertIn(supervisor_group, new_leader.groups_id)
        self.assertNotIn(self.user_a, self.team_a.supervisor_ids)
        self.user_a.invalidate_recordset(["groups_id"])
        self.assertNotIn(supervisor_group, self.user_a.groups_id)

    def test_manual_roster_edit_requires_binding_deactivation(self):
        with self.assertRaisesRegex(ValidationError, "managed from the CRM"):
            self.team_a.write({"agent_ids": [(6, 0, self.user_a.ids)]})

        self.team_binding_a.write({"active": False})
        self.team_a.write(
            {
                "agent_ids": [(6, 0, self.user_a.ids)],
                "supervisor_ids": [(5, 0, 0)],
            }
        )
        self.assertEqual(self.team_a.agent_ids, self.user_a)
        self.assertFalse(self.team_a.supervisor_ids)

        new_member = self._create_user(
            "InactiveBindingMember", self.env.ref("sales_team.group_sale_salesman")
        )
        self.env["crm.team.member"].create(
            {"crm_team_id": self.crm_team.id, "user_id": new_member.id}
        )
        self.assertNotIn(new_member, self.team_a.agent_ids)

    def test_binding_repoint_projects_the_new_crm_authority(self):
        new_leader = self._create_user(
            "RepointLeader", self.env.ref("sales_team.group_sale_salesman")
        )
        other_crm_team = self.env["crm.team"].create(
            {
                "name": "Repoint CRM Team %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "user_id": new_leader.id,
            }
        )

        self.team_binding_a.write({"crm_team_id": other_crm_team.id})

        self.assertEqual(self.team_a.supervisor_ids, new_leader)
        self.assertFalse(self.team_a.agent_ids)
        self.assertEqual(self.team_a.crm_team_id, other_crm_team)

    def test_binding_archive_freezes_last_roster_and_preserves_evidence(self):
        frozen_agents = self.team_a.agent_ids
        frozen_supervisors = self.team_a.supervisor_ids
        with self.assertRaises(AccessError):
            self.team_binding_a.unlink()
        self.team_binding_a.write({"active": False})
        new_member = self._create_user(
            "UnboundMember", self.env.ref("sales_team.group_sale_salesman")
        )

        self.env["crm.team.member"].create(
            {"crm_team_id": self.crm_team.id, "user_id": new_member.id}
        )

        self.assertEqual(self.team_a.agent_ids, frozen_agents)
        self.assertEqual(self.team_a.supervisor_ids, frozen_supervisors)
        self.assertFalse(self.team_a.crm_team_id)

    def test_linked_case_survives_roster_binding_freeze_and_removal(self):
        channel = self._create_channel(self.team_account, "Frozen linked roster")
        case = self._default_case(channel)
        case.with_user(self.user_a).action_transition(self.stage_a.id)
        case.with_user(self.user_a).action_create_crm_lead()
        case.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        lead = case.crm_lead_id
        frozen_agents = self.team_a.agent_ids
        frozen_supervisors = self.team_a.supervisor_ids

        self.team_binding_a.write({"active": False})
        self.crm_member_b.action_archive()

        self.assertEqual(self.team_a.agent_ids, frozen_agents)
        self.assertEqual(self.team_a.supervisor_ids, frozen_supervisors)
        lead.with_user(self.user_a).write({"stage_id": self.crm_stage_b.id})
        self.assertEqual(case.stage_id, self.stage_b)

        with self.assertRaises(AccessError):
            self.team_binding_a.unlink()
        lead.with_user(self.user_a).write({"stage_id": self.crm_stage_a.id})
        self.assertEqual(case.stage_id, self.stage_a)
        self.assertEqual(case.crm_lead_id, lead)
        self.assertFalse(self.team_a.crm_team_id)

        different_team = self.env["crm.team"].create(
            {
                "name": "Conflicting frozen roster %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "user_id": self.user_b.id,
            }
        )
        with self.assertRaisesRegex(
            ValidationError, "CRM authority"
        ), self.env.cr.savepoint():
            self.team_binding_a.write(
                {"active": True, "crm_team_id": different_team.id}
            )
        self.assertFalse(self.team_a.crm_team_id)

    def test_empty_crm_roster_cannot_orphan_shared_inbox(self):
        self.crm_member_b.action_archive()
        with self.assertRaisesRegex(
            ValidationError, "cannot become empty"
        ), self.env.cr.savepoint():
            self.crm_team.write({"user_id": False})

        self.assertEqual(self.crm_team.user_id, self.user_a)
        self.assertEqual(self.team_a.supervisor_ids, self.user_a)

    def test_empty_crm_roster_is_allowed_for_owner_backed_inbox(self):
        empty_crm_team = self.env["crm.team"].create(
            {
                "name": "Owner-backed empty roster %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "user_id": False,
            }
        )
        owner_backed_team = self.env["contact.center.team"].create(
            {
                "name": "Owner-backed CC team %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "agent_ids": [(6, 0, self.user_a.ids)],
                "pipeline_ids": [(6, 0, self.pipeline.ids)],
                "default_pipeline_id": self.pipeline.id,
            }
        )
        account = self._create_account(
            "Owner-backed empty roster",
            team=owner_backed_team,
            owner=self.user_a,
        )
        channel = self._create_channel(account, "Owner-backed roster")

        self.env["contact.center.crm.team.binding"].create(
            {
                "contact_center_team_id": owner_backed_team.id,
                "crm_team_id": empty_crm_team.id,
            }
        )

        self.assertFalse(owner_backed_team.agent_ids)
        self.assertFalse(owner_backed_team.supervisor_ids)
        channel.invalidate_recordset(["channel_member_ids"])
        self.assertIn(
            self.user_a.partner_id, channel.sudo().channel_member_ids.partner_id
        )

    def test_owner_scope_survives_removal_from_crm_roster(self):
        owned_shared = self._create_account(
            "Owner and CRM roster", team=self.team_a, owner=self.user_b
        )
        channel = self._create_channel(owned_shared, "Owner roster removal")

        self.crm_member_b.action_archive()

        self.assertNotIn(self.user_b, self.team_a.agent_ids)
        channel.invalidate_recordset(["channel_member_ids"])
        self.assertIn(
            self.user_b.partner_id, channel.sudo().channel_member_ids.partner_id
        )
        self.assertEqual(
            self.env["contact.center.account"]
            .with_user(self.user_b)
            .search([("id", "=", owned_shared.id)]),
            owned_shared,
        )

    def test_global_crm_team_does_not_grant_missing_company_access(self):
        other_company = self.env["res.company"].create(
            {"name": "Roster isolation %s" % uuid.uuid4()}
        )
        crm_user = self.env.ref("sales_team.group_sale_salesman")
        external_company_user = (
            self.env["res.users"]
            .sudo()
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Other company roster member",
                    "login": "other-company-roster-%s" % uuid.uuid4(),
                    "email": "other-company-roster@example.invalid",
                    "company_id": other_company.id,
                    "company_ids": [(6, 0, other_company.ids)],
                    "groups_id": [(6, 0, crm_user.ids)],
                }
            )
        )
        global_crm_team = self.env["crm.team"].create(
            {
                "name": "Global roster %s" % uuid.uuid4(),
                "company_id": False,
                "user_id": external_company_user.id,
            }
        )
        local_team = self.env["contact.center.team"].create(
            {
                "name": "Global roster target %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "agent_ids": [(6, 0, self.user_a.ids)],
            }
        )

        with self.assertRaisesRegex(
            ValidationError, "has no access to company"
        ), self.env.cr.savepoint():
            self.env["contact.center.crm.team.binding"].create(
                {
                    "contact_center_team_id": local_team.id,
                    "crm_team_id": global_crm_team.id,
                }
            )
        self.assertFalse(local_team.crm_team_id)
        self.assertNotIn(
            self.env.ref("contact_center_base.group_contact_center_supervisor"),
            external_company_user.groups_id,
        )

    def test_mono_membership_move_synchronizes_old_and_new_teams(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "sales_team.membership_multi", False
        )
        new_leader = self._create_user(
            "MoveLeader", self.env.ref("sales_team.group_sale_salesman")
        )
        second_crm_team = self.env["crm.team"].create(
            {
                "name": "Second roster authority %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "user_id": new_leader.id,
            }
        )
        second_cc_team = self.env["contact.center.team"].create(
            {
                "name": "Second roster target %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "agent_ids": [(6, 0, self.user_a.ids)],
            }
        )
        self.env["contact.center.crm.team.binding"].create(
            {
                "contact_center_team_id": second_cc_team.id,
                "crm_team_id": second_crm_team.id,
            }
        )

        self.env["crm.team.member"].create(
            {"crm_team_id": second_crm_team.id, "user_id": self.user_b.id}
        )

        self.crm_member_b.invalidate_recordset(["active"])
        self.assertFalse(self.crm_member_b.active)
        self.assertNotIn(self.user_b, self.team_a.agent_ids)
        self.assertIn(self.user_b, second_cc_team.agent_ids)

    def test_linked_case_can_move_to_manual_unbound_team(self):
        target_team = self.env["contact.center.team"].create(
            {
                "name": "Manual linked-case target %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "agent_ids": [(6, 0, self.user_b.ids)],
                "pipeline_ids": [(6, 0, self.pipeline.ids)],
                "default_pipeline_id": self.pipeline.id,
            }
        )
        account = self._create_account(
            "Manual linked-case team change",
            team=self.team_a,
        )
        channel = self._create_channel(account, "Manual linked-case customer")
        case = self._default_case(channel)
        case.with_user(self.user_a).action_transition(self.stage_a.id)
        case.with_user(self.user_a).action_create_crm_lead()
        case.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        lead = case.crm_lead_id

        account.write({"default_team_id": target_team.id})

        channel.invalidate_recordset(["contact_center_team_id", "channel_member_ids"])
        case.invalidate_recordset(["team_id", "crm_link_ids", "crm_lead_id"])
        self.assertEqual(account.default_team_id, target_team)
        self.assertEqual(channel.contact_center_team_id, target_team)
        self.assertEqual(case.team_id, target_team)
        self.assertEqual(case.crm_lead_id, lead)
        self.assertIn(
            self.user_b.partner_id,
            channel.sudo().channel_member_ids.partner_id,
        )
        self.assertNotIn(
            self.user_a.partner_id,
            channel.sudo().channel_member_ids.partner_id,
        )

    def test_manual_unbound_team_can_create_pipeline_bound_crm_lead(self):
        manual_team = self.env["contact.center.team"].create(
            {
                "name": "Manual CRM-capable team %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "agent_ids": [(6, 0, self.user_b.ids)],
                "pipeline_ids": [(6, 0, self.pipeline.ids)],
                "default_pipeline_id": self.pipeline.id,
            }
        )
        account = self._create_account("Manual CRM-capable inbox", team=manual_team)
        case = self._default_case(
            self._create_channel(account, "Manual CRM-capable customer")
        )
        case.with_user(self.user_b).action_transition(self.stage_a.id)

        case.with_user(self.user_b).action_create_crm_lead()

        case.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        self.assertTrue(case.crm_lead_id)
        self.assertEqual(case.crm_lead_id.team_id, self.crm_team)
        self.assertFalse(manual_team.crm_team_id)

    def test_linked_case_rejects_differently_bound_team_change(self):
        different_crm_team = self.env["crm.team"].create(
            {
                "name": "Different CRM Team %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "user_id": self.user_b.id,
            }
        )
        target_team = self.env["contact.center.team"].create(
            {
                "name": "Differently bound target %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "agent_ids": [(6, 0, self.user_b.ids)],
                "pipeline_ids": [(6, 0, self.pipeline.ids)],
                "default_pipeline_id": self.pipeline.id,
            }
        )
        self.env["contact.center.crm.team.binding"].create(
            {
                "contact_center_team_id": target_team.id,
                "crm_team_id": different_crm_team.id,
            }
        )
        account = self._create_account(
            "Rejected differently bound team change",
            team=self.team_a,
        )
        channel = self._create_channel(account, "Rejected bound-team customer")
        case = self._default_case(channel)
        case.with_user(self.user_a).action_transition(self.stage_a.id)
        case.with_user(self.user_a).action_create_crm_lead()
        case.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        link = case.crm_link_ids
        lead = case.crm_lead_id
        original_member_ids = set(channel.sudo().channel_member_ids.partner_id.ids)

        with self.assertRaisesRegex(
            ValidationError, "bound to another CRM sales team"
        ), self.env.cr.savepoint():
            account.write({"default_team_id": target_team.id})

        account.invalidate_recordset(["default_team_id"])
        channel.invalidate_recordset(["contact_center_team_id", "channel_member_ids"])
        case.invalidate_recordset(["team_id", "crm_link_ids", "crm_lead_id"])
        link.invalidate_recordset(["contact_center_team_id"])
        self.assertEqual(account.default_team_id, self.team_a)
        self.assertEqual(channel.contact_center_team_id, self.team_a)
        self.assertEqual(case.team_id, self.team_a)
        self.assertEqual(
            set(channel.sudo().channel_member_ids.partner_id.ids),
            original_member_ids,
        )
        self.assertEqual(case.crm_link_ids, link)
        self.assertEqual(case.crm_lead_id, lead)
        self.assertEqual(link.contact_center_team_id, self.team_a)

    def test_same_lead_in_two_exclusive_boxes_does_not_expand_acl(self):
        channel_a = self._create_channel(self.owner_account_a, "Owner customer A")
        channel_b = self._create_channel(self.owner_account_b, "Owner customer B")
        case_a = self._default_case(channel_a)
        case_b = self._default_case(channel_b)
        self.assertFalse(case_a.team_id)
        self.assertFalse(case_b.team_id)

        case_a.with_user(self.user_a).action_transition(self.stage_a.id)
        case_a.with_user(self.user_a).action_create_crm_lead()
        case_a.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        lead = case_a.crm_lead_id
        case_b.with_user(self.user_b).action_link_crm_lead(lead.id)

        Link = self.env["contact.center.crm.case.link"]
        links_a = Link.with_user(self.user_a).search([("lead_id", "=", lead.id)])
        links_b = Link.with_user(self.user_b).search([("lead_id", "=", lead.id)])
        self.assertEqual(links_a.mapped("case_id"), case_a)
        self.assertEqual(links_b.mapped("case_id"), case_b)
        self.assertFalse(
            self.env["contact.center.case"]
            .with_user(self.user_a)
            .search([("id", "=", case_b.id)])
        )
        with self.assertRaises(AccessError):
            case_b.with_user(self.user_a).check_access_rule("read")

        revision_a = case_a.stage_revision
        revision_b = case_b.stage_revision
        lead.with_user(self.user_a).write({"stage_id": self.crm_stage_b.id})
        self.assertEqual(case_a.stage_id, self.stage_b)
        self.assertEqual(case_b.stage_id, self.stage_b)
        self.assertEqual(case_a.stage_revision, revision_a + 1)
        self.assertEqual(case_b.stage_revision, revision_b + 1)
        self.assertFalse(
            self.env["contact.center.case"]
            .with_user(self.user_a)
            .search([("id", "=", case_b.id)])
        )

    def test_rpc_context_cannot_skip_linked_case_stage_synchronization(self):
        case = self._default_case(
            self._create_channel(self.owner_account_a, "Untrusted sync context")
        )
        case.with_user(self.user_a).action_transition(self.stage_a.id)
        case.with_user(self.user_a).action_create_crm_lead()
        case.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        lead = case.crm_lead_id
        revision = case.stage_revision

        lead.with_user(self.user_a).with_context(
            contact_center_crm_origin_case_id=case.id,
            contact_center_crm_stage_sync="forged-rpc-token",
        ).write({"stage_id": self.crm_stage_b.id})

        case.invalidate_recordset(["stage_id", "stage_revision"])
        self.assertEqual(lead.stage_id, self.crm_stage_b)
        self.assertEqual(case.stage_id, self.stage_b)
        self.assertEqual(case.stage_revision, revision + 1)

    def test_one_lead_syncs_cases_in_different_bound_pipelines(self):
        second_pipeline = self.env["contact.center.pipeline"].create(
            {
                "name": "Second CRM pipeline",
                "code": "second-crm-%s" % uuid.uuid4().hex[:8],
                "company_id": self.env.company.id,
            }
        )
        self.env["contact.center.pipeline.stage"].create(
            {
                "name": "Second local initial",
                "code": "second-local-initial",
                "pipeline_id": second_pipeline.id,
                "is_initial": True,
            }
        )
        second_binding = self.env["contact.center.crm.pipeline.binding"].create(
            {
                "pipeline_id": second_pipeline.id,
                "crm_team_id": self.crm_team.id,
            }
        )
        second_stage_a = second_binding.stage_binding_ids.filtered(
            lambda item: item.crm_stage_id == self.crm_stage_a
        ).stage_id
        second_stage_b = second_binding.stage_binding_ids.filtered(
            lambda item: item.crm_stage_id == self.crm_stage_b
        ).stage_id
        second_account = self._create_account(
            "Exclusive second-pipeline inbox",
            owner=self.user_b,
            pipeline=second_pipeline,
        )

        case_a = self._default_case(
            self._create_channel(self.owner_account_a, "First pipeline customer")
        )
        case_b = self._default_case(
            self._create_channel(second_account, "Second pipeline customer")
        )
        case_a.with_user(self.user_a).action_transition(self.stage_a.id)
        case_a.with_user(self.user_a).action_create_crm_lead()
        case_a.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        lead = case_a.crm_lead_id
        case_b.with_user(self.user_b).action_link_crm_lead(lead.id)
        case_b.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        self.assertEqual(case_b.stage_id, second_stage_a)

        lead.with_user(self.user_a).write({"stage_id": self.crm_stage_b.id})
        self.assertEqual(case_a.stage_id, self.stage_b)
        self.assertEqual(case_b.stage_id, second_stage_b)
        self.assertEqual(case_a.crm_lead_id, lead)
        self.assertEqual(case_b.crm_lead_id, lead)

    def test_bindings_and_catalog_sync_never_change_crm_members(self):
        Member = self.env["crm.team.member"]
        before = Member.search([("crm_team_id", "=", self.crm_team.id)]).read(
            ["user_id", "active"]
        )

        self.team_binding_a.action_sync_roster()
        self.pipeline_binding.action_sync_stage_catalog()

        after = Member.search([("crm_team_id", "=", self.crm_team.id)]).read(
            ["user_id", "active"]
        )
        self.assertEqual(before, after)
        self.assertEqual(
            self.env["contact.center.crm.team.binding"].search_count(
                [("crm_team_id", "=", self.crm_team.id)]
            ),
            1,
        )

    def test_contact_center_sides_of_bindings_are_immutable(self):
        other_team = self.env["contact.center.team"].create(
            {
                "name": "Immutable binding target %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "agent_ids": [(6, 0, self.user_a.ids)],
                "pipeline_ids": [(6, 0, self.pipeline.ids)],
                "default_pipeline_id": self.pipeline.id,
            }
        )
        other_pipeline = self.env["contact.center.pipeline"].create(
            {
                "name": "Immutable pipeline target",
                "code": "immutable-target-%s" % uuid.uuid4().hex[:8],
                "company_id": self.env.company.id,
            }
        )
        self.env["contact.center.pipeline.stage"].create(
            {
                "name": "Immutable target initial",
                "code": "immutable-target-initial",
                "pipeline_id": other_pipeline.id,
                "is_initial": True,
            }
        )

        with self.assertRaises(ValidationError):
            self.team_binding_a.write({"contact_center_team_id": other_team.id})
        with self.assertRaises(ValidationError):
            self.pipeline_binding.write({"pipeline_id": other_pipeline.id})
        self.assertEqual(self.team_binding_a.contact_center_team_id, self.team_a)
        self.assertEqual(self.pipeline_binding.pipeline_id, self.pipeline)

    def test_bound_pipeline_stage_catalog_is_crm_authoritative(self):
        with self.assertRaises(ValidationError):
            self.env["contact.center.pipeline.stage"].create(
                {
                    "name": "Local stage",
                    "code": "local-stage",
                    "pipeline_id": self.pipeline.id,
                }
            )
        with self.assertRaises(ValidationError):
            self.stage_a.write({"name": "Local rename"})

        self.crm_stage_a.write({"name": "CRM authoritative rename"})
        self.assertEqual(self.stage_a.name, "CRM authoritative rename")

    def test_crm_stage_delete_converges_or_blocks_when_currently_used(self):
        removable = self.env["crm.stage"].create(
            {
                "name": "Removable CRM stage %s" % uuid.uuid4(),
                "sequence": 201,
                "team_id": self.crm_team.id,
            }
        )
        removable_mapping = self.env["contact.center.crm.stage.binding"].search(
            [
                ("pipeline_binding_id", "=", self.pipeline_binding.id),
                ("crm_stage_id", "=", removable.id),
            ]
        )
        removable_core_stage = removable_mapping.stage_id
        removable.unlink()
        self.assertFalse(removable.exists())
        self.assertFalse(removable_mapping.exists())
        self.assertFalse(removable_core_stage.active)

        used = self.env["crm.stage"].create(
            {
                "name": "Used CRM stage %s" % uuid.uuid4(),
                "sequence": 202,
                "team_id": self.crm_team.id,
            }
        )
        used_mapping = self.env["contact.center.crm.stage.binding"].search(
            [
                ("pipeline_binding_id", "=", self.pipeline_binding.id),
                ("crm_stage_id", "=", used.id),
            ]
        )
        case = self._default_case(
            self._create_channel(self.team_account, "Stage deletion guard")
        )
        case.with_user(self.user_a).action_transition(used_mapping.stage_id.id)

        with self.assertRaises(ValidationError):
            used.unlink()
        self.assertTrue(used.exists())
        self.assertTrue(used_mapping.exists())
        self.assertEqual(case.stage_id, used_mapping.stage_id)

    def test_crm_stage_archive_and_restore_reuses_the_same_catalog_rows(self):
        crm_stage = self.env["crm.stage"].create(
            {
                "name": "Archive restore CRM stage %s" % uuid.uuid4(),
                "sequence": 150,
                "team_id": self.crm_team.id,
            }
        )
        mapping = self.env["contact.center.crm.stage.binding"].search(
            [
                ("pipeline_binding_id", "=", self.pipeline_binding.id),
                ("crm_stage_id", "=", crm_stage.id),
            ]
        )
        core_stage = mapping.stage_id

        crm_stage.action_archive()

        archived_mapping = (
            self.env["contact.center.crm.stage.binding"]
            .with_context(active_test=False)
            .browse(mapping.id)
        )
        archived_core_stage = (
            self.env["contact.center.pipeline.stage"]
            .with_context(active_test=False)
            .browse(core_stage.id)
        )
        self.assertFalse(archived_mapping.active)
        self.assertFalse(archived_core_stage.active)

        crm_stage.action_unarchive()

        archived_mapping.invalidate_recordset(["active"])
        archived_core_stage.invalidate_recordset(["active"])
        self.assertTrue(archived_mapping.active)
        self.assertTrue(archived_core_stage.active)
        self.assertEqual(archived_mapping.crm_stage_id, crm_stage)
        self.assertEqual(archived_mapping.stage_id, core_stage)

    def test_crm_stage_archive_is_atomic_when_a_current_case_uses_it(self):
        crm_stage = self.env["crm.stage"].create(
            {
                "name": "Used archive CRM stage %s" % uuid.uuid4(),
                "sequence": 151,
                "team_id": self.crm_team.id,
            }
        )
        mapping = self.env["contact.center.crm.stage.binding"].search(
            [
                ("pipeline_binding_id", "=", self.pipeline_binding.id),
                ("crm_stage_id", "=", crm_stage.id),
            ]
        )
        case = self._default_case(
            self._create_channel(self.team_account, "Used archived CRM stage")
        )
        case.with_user(self.user_a).action_transition(mapping.stage_id.id)

        with self.assertRaises(ValidationError):
            crm_stage.action_archive()

        crm_stage.invalidate_recordset(["active"])
        mapping.invalidate_recordset(["active"])
        mapping.stage_id.invalidate_recordset(["active"])
        self.assertTrue(crm_stage.active)
        self.assertTrue(mapping.active)
        self.assertTrue(mapping.stage_id.active)
        self.assertEqual(case.stage_id, mapping.stage_id)

    def test_active_bindings_protect_pipeline_and_crm_team_archival(self):
        with self.assertRaises(ValidationError):
            self.team_a.write({"active": False})
        with self.assertRaises(ValidationError):
            self.pipeline.write({"active": False})
        with self.assertRaises(ValidationError):
            self.crm_team.write({"active": False})
        self.assertTrue(self.pipeline.active)
        self.assertTrue(self.crm_team.active)

    def test_inactive_bindings_still_protect_crm_team_company(self):
        self.team_binding_a.write({"active": False})
        self.pipeline_binding.write({"active": False})
        other_company = self.env["res.company"].create(
            {"name": "CRM bridge other company %s" % uuid.uuid4()}
        )

        with self.assertRaises(ValidationError):
            self.crm_team.write({"company_id": other_company.id})
        self.assertEqual(self.crm_team.company_id, self.env.company)

    def test_wrong_team_stage_is_rejected_for_linked_lead(self):
        channel = self._create_channel(self.team_account, "Wrong stage")
        case = self._default_case(channel)
        case.with_user(self.user_a).action_transition(self.stage_a.id)
        case.with_user(self.user_a).action_create_crm_lead()
        case.invalidate_recordset(["crm_link_ids", "crm_lead_id"])

        other_team = self.env["crm.team"].create(
            {
                "name": "Other CRM Team %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "user_id": False,
            }
        )
        wrong_stage = self.env["crm.stage"].create(
            {
                "name": "Wrong CRM Stage %s" % uuid.uuid4(),
                "team_id": other_team.id,
            }
        )
        with self.assertRaises(ValidationError):
            case.crm_lead_id.with_user(self.user_a).write({"stage_id": wrong_stage.id})
        self.assertEqual(case.crm_lead_id.stage_id, self.crm_stage_a)
        self.assertEqual(case.stage_id, self.stage_a)

    def test_hidden_crm_link_does_not_offer_or_create_a_second_lead(self):
        groups = self.env.ref("contact_center_base.group_contact_center_agent") | (
            self.env.ref("sales_team.group_sale_salesman")
        )
        restricted_user = self._create_user("Hidden", groups)
        account = self._create_account("Hidden CRM link inbox", owner=restricted_user)
        channel = self._create_channel(account, "Hidden CRM link")
        case = self._default_case(channel)
        case.with_user(restricted_user).action_transition(self.stage_a.id)
        case.with_user(restricted_user).action_create_crm_lead()
        case.invalidate_recordset(["crm_lead_id"])
        lead = case.crm_lead_id
        lead.write({"user_id": self.user_a.id})

        restricted_case = case.with_user(restricted_user)
        restricted_case.invalidate_recordset(
            ["crm_link_exists", "crm_lead_count", "crm_lead_id"]
        )
        self.assertTrue(restricted_case.crm_link_exists)
        self.assertEqual(restricted_case.crm_lead_count, 0)
        self.assertFalse(restricted_case.crm_lead_id)
        with self.assertRaisesRegex(ValidationError, "already has a CRM lead"):
            restricted_case.action_create_crm_lead()
        with self.assertRaises(AccessError):
            restricted_case.action_open_crm_lead()

    def test_bidirectional_stage_sync_is_single_pass(self):
        channel_a = self._create_channel(self.owner_account_a, "Sync customer A")
        channel_b = self._create_channel(self.owner_account_b, "Sync customer B")
        case_a = self._default_case(channel_a)
        case_b = self._default_case(channel_b)
        case_a.with_user(self.user_a).action_transition(self.stage_a.id)
        case_a.with_user(self.user_a).action_create_crm_lead()
        case_a.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        lead = case_a.crm_lead_id
        case_b.with_user(self.user_b).action_link_crm_lead(lead.id)

        before_a = len(case_a.transition_ids)
        before_b = len(case_b.transition_ids)
        case_a.with_user(self.user_a).action_transition(self.stage_b.id)
        self.assertEqual(lead.stage_id, self.crm_stage_b)
        self.assertEqual(case_a.stage_id, self.stage_b)
        self.assertEqual(case_b.stage_id, self.stage_b)
        self.assertEqual(len(case_a.transition_ids), before_a + 1)
        self.assertEqual(len(case_b.transition_ids), before_b + 1)

        before_a = len(case_a.transition_ids)
        before_b = len(case_b.transition_ids)
        lead.with_user(self.user_a).write({"stage_id": self.crm_stage_a.id})
        self.assertEqual(case_a.stage_id, self.stage_a)
        self.assertEqual(case_b.stage_id, self.stage_a)
        self.assertEqual(len(case_a.transition_ids), before_a + 1)
        self.assertEqual(len(case_b.transition_ids), before_b + 1)

    def test_link_contract_lookups_are_batched_per_operation(self):
        case_a = self._default_case(
            self._create_channel(self.team_account, "Batch contract A")
        )
        case_b = self._default_case(
            self._create_channel(self.team_account, "Batch contract B")
        )
        case_a.with_user(self.user_a).action_transition(self.stage_a.id)
        case_b.with_user(self.user_a).action_transition(self.stage_a.id)
        case_a.with_user(self.user_a).action_create_crm_lead()
        case_a.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        lead = case_a.crm_lead_id
        case_b.with_user(self.user_a).action_link_crm_lead(lead.id)
        links = lead.sudo().contact_center_case_link_ids
        self.assertEqual(len(links), 2)

        model_names = (
            "contact.center.crm.pipeline.binding",
            "contact.center.crm.team.binding",
            "contact.center.crm.stage.binding",
        )

        def search_counts(operation):
            counts = {model_name: 0 for model_name in model_names}
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
                        counts[_model_name] += 1
                        return _original_search(recordset, *args, **kwargs)

                    stack.enter_context(
                        mock.patch.object(
                            model_class,
                            "search",
                            autospec=True,
                            side_effect=counted_search,
                        )
                    )
                operation()
            return counts

        expected = {model_name: 1 for model_name in model_names}
        self.assertEqual(
            search_counts(
                lambda: lead._contact_center_validate_linked_values(
                    {"stage_id": self.crm_stage_a.id}
                )
            ),
            expected,
        )
        self.assertEqual(
            search_counts(lambda: links._validate_link_contract()),
            expected,
        )
        self.assertEqual(
            search_counts(lambda: lead._contact_center_sync_case_stages()),
            expected,
        )

    def test_create_lead_projects_only_an_existing_direct_partner(self):
        partner = self.env["res.partner"].create({"name": "Existing CRM Contact"})
        direct = self._create_channel(
            self.team_account, "Known direct customer", partner=partner
        )
        direct_case = self._default_case(direct)
        direct_case.with_user(self.user_a).action_transition(self.stage_a.id)
        direct_case.with_user(self.user_a).action_create_crm_lead()
        direct_case.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        self.assertEqual(direct_case.crm_lead_id.partner_id, partner)

        guest = self._create_channel(self.team_account, "Guest-only customer")
        guest_case = self._default_case(guest)
        guest_case.with_user(self.user_a).action_transition(self.stage_a.id)
        guest_case.with_user(self.user_a).action_create_crm_lead()
        guest_case.invalidate_recordset(["crm_link_ids", "crm_lead_id"])
        self.assertFalse(guest_case.crm_lead_id.partner_id)

    def test_first_binding_transitions_only_revision_zero_initial_cases(self):
        pipeline = self.env["contact.center.pipeline"].create(
            {
                "name": "Local pipeline",
                "code": "local-%s" % uuid.uuid4().hex[:8],
                "company_id": self.env.company.id,
            }
        )
        local_initial = self.env["contact.center.pipeline.stage"].create(
            {
                "name": "Local new",
                "code": "local-new",
                "pipeline_id": pipeline.id,
                "is_initial": True,
            }
        )
        team = self.env["contact.center.team"].create(
            {
                "name": "Local team %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "agent_ids": [(6, 0, self.user_a.ids)],
                "pipeline_ids": [(6, 0, pipeline.ids)],
                "default_pipeline_id": pipeline.id,
            }
        )
        account = self._create_account("Local inbox", team=team, pipeline=pipeline)
        case = self._default_case(self._create_channel(account, "Local customer"))
        self.assertEqual(case.stage_id, local_initial)
        self.assertEqual(case.stage_revision, 0)

        binding = self.env["contact.center.crm.pipeline.binding"].create(
            {"pipeline_id": pipeline.id, "crm_team_id": self.crm_team.id}
        )
        expected = binding.stage_binding_ids.filtered(
            lambda item: item.stage_id.is_initial
        ).stage_id
        initial_stages = (
            self.env["contact.center.pipeline.stage"]
            .with_context(active_test=False)
            .search([("pipeline_id", "=", pipeline.id), ("is_initial", "=", True)])
        )
        self.assertEqual(initial_stages, expected)
        self.assertEqual(case.stage_id, expected)
        self.assertEqual(case.stage_revision, 1)
        self.assertFalse(local_initial.active)
        self.assertEqual(case.transition_ids.sorted("id")[-1].source, "integration")
        self.assertFalse(
            pipeline.stage_ids.filtered("active")
            - binding.stage_binding_ids.filtered("active").mapped("stage_id")
        )

    def test_first_binding_rejects_case_with_local_stage_history(self):
        pipeline = self.env["contact.center.pipeline"].create(
            {
                "name": "Historic pipeline",
                "code": "historic-%s" % uuid.uuid4().hex[:8],
                "company_id": self.env.company.id,
            }
        )
        initial = self.env["contact.center.pipeline.stage"].create(
            {
                "name": "Historic new",
                "code": "historic-new",
                "pipeline_id": pipeline.id,
                "is_initial": True,
            }
        )
        progressed = self.env["contact.center.pipeline.stage"].create(
            {
                "name": "Historic progress",
                "code": "historic-progress",
                "pipeline_id": pipeline.id,
            }
        )
        team = self.env["contact.center.team"].create(
            {
                "name": "Historic team %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "agent_ids": [(6, 0, self.user_a.ids)],
                "pipeline_ids": [(6, 0, pipeline.ids)],
                "default_pipeline_id": pipeline.id,
            }
        )
        account = self._create_account(
            "Progressed local inbox", team=team, pipeline=pipeline
        )
        case = self._default_case(
            self._create_channel(account, "Progressed local customer")
        )
        self.assertEqual(case.stage_id, initial)
        case.with_user(self.user_a).action_transition(progressed.id)

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env["contact.center.crm.pipeline.binding"].create(
                {"pipeline_id": pipeline.id, "crm_team_id": self.crm_team.id}
            )
        self.assertEqual(case.stage_id, progressed)
        self.assertEqual(case.stage_revision, 1)
        self.assertTrue(initial.active)
        self.assertTrue(progressed.active)
