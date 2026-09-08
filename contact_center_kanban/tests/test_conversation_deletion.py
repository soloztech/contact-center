import uuid

from odoo.tests.common import SavepointCase


class TestKanbanConversationDeletion(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        groups = cls.env.ref(
            "contact_center_base.group_contact_center_admin"
        ) | cls.env.ref("sales_team.group_sale_manager")
        cls.admin = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Conversation deletion administrator",
                    "login": "cc-kanban-deletion-%s" % uuid.uuid4(),
                    "email": "cc-kanban-deletion@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, groups.ids)],
                }
            )
        )
        cls.pipeline = cls.env[
            "contact.center.pipeline"
        ]._contact_center_provision_default_pipeline(cls.env.company)
        cls.crm_team = cls.env["crm.team"].create(
            {
                "name": "Conversation deletion sales team",
                "company_id": cls.env.company.id,
                "user_id": cls.admin.id,
            }
        )
        cls.crm_stage = cls.env["crm.stage"].create(
            {
                "name": "Conversation deletion qualified",
                "team_id": cls.crm_team.id,
            }
        )
        cls.pipeline_binding = cls.env["contact.center.crm.pipeline.binding"].create(
            {"pipeline_id": cls.pipeline.id, "crm_team_id": cls.crm_team.id}
        )
        cls.stage = (
            cls.env["contact.center.crm.stage.binding"]
            .search(
                [
                    ("pipeline_binding_id", "=", cls.pipeline_binding.id),
                    ("crm_stage_id", "=", cls.crm_stage.id),
                    ("active", "=", True),
                ]
            )
            .stage_id
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Conversation deletion inbox",
                "company_id": cls.env.company.id,
                "platform": "telegram",
                "external_ref": "kanban-deletion-%s" % uuid.uuid4(),
                "access_user_ids": [(6, 0, cls.admin.ids)],
                "default_pipeline_id": cls.pipeline.id,
                "conversation_delete_enabled": False,
            }
        )

    def _conversation(self, name):
        guest = self.env["mail.guest"].sudo().create({"name": name})
        identity = (
            self.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": name,
                    "company_id": self.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account, identity=identity, guest_ids=guest.ids
        )
        self.env["contact.center.channel.binding"].sudo().create(
            {
                "channel_id": channel.id,
                "account_id": self.account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": str(uuid.uuid4()),
            }
        )
        return channel

    def test_deletion_removes_case_chatter_but_preserves_shared_crm_lead(self):
        channel = self._conversation("Conversation to delete")
        other_channel = self._conversation("Conversation to preserve")
        case = channel.contact_center_case_ids.filtered("is_default")
        other_case = other_channel.contact_center_case_ids.filtered("is_default")
        case.with_user(self.admin).action_transition(self.stage.id)
        case.with_user(self.admin).action_create_crm_opportunity()
        case.invalidate_recordset(["crm_lead_id", "crm_link_ids"])
        lead = case.crm_lead_id
        other_case.with_user(self.admin).action_link_crm_lead(lead.id)
        other_case.invalidate_recordset(["crm_lead_id", "crm_link_ids"])
        case_note = case.with_user(self.admin).message_post(
            body="Case-only internal history", subtype_xmlid="mail.mt_note"
        )
        other_note = other_case.with_user(self.admin).message_post(
            body="Retained case history", subtype_xmlid="mail.mt_note"
        )
        lead_note = lead.with_user(self.admin).message_post(
            body="Retained opportunity history", subtype_xmlid="mail.mt_note"
        )
        case_links = case.crm_link_ids
        other_case_links = other_case.crm_link_ids
        canonical_links = self.env["contact.center.crm.conversation.link"].search(
            [("channel_id", "=", channel.id)]
        )
        other_canonical_links = self.env["contact.center.crm.conversation.link"].search(
            [("channel_id", "=", other_channel.id)]
        )
        transitions = self.env["contact.center.case.transition"].search(
            [("case_id", "=", case.id)]
        )
        case_followers = case.message_follower_ids
        lead_snapshot = lead.read(["name", "stage_id", "team_id", "partner_id"])
        self.assertTrue(case_links)
        self.assertTrue(canonical_links)
        self.assertTrue(transitions)
        self.assertTrue(case_followers)

        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.admin)
            .delete_conversation(channel.id)
        )

        self.assertTrue(result["removed_from_conversation"])
        for removed in (
            channel,
            case,
            case_note,
            case_links,
            canonical_links,
            transitions,
            case_followers,
        ):
            self.assertFalse(removed.exists(), removed._name)
        for retained in (
            lead,
            lead_note,
            other_channel,
            other_case,
            other_note,
            other_case_links,
            other_canonical_links,
        ):
            self.assertEqual(retained.exists(), retained, retained._name)
        self.assertEqual(
            lead.read(["name", "stage_id", "team_id", "partner_id"]), lead_snapshot
        )
        self.assertEqual(other_case_links.state, "active")
        self.assertEqual(other_canonical_links.state, "active")
