import datetime
import uuid

from odoo import fields
from odoo.tests.common import SavepointCase


class TestContactCenterActivityMenu(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Activity Menu Agent",
                    "login": "cc-activity-menu-%s" % uuid.uuid4(),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [
                        (
                            6,
                            0,
                            cls.env.ref(
                                "contact_center_base.group_contact_center_agent"
                            ).ids,
                        )
                    ],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Activity Menu",
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Activity Menu",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "activity-menu-%s" % uuid.uuid4(),
                "access_team_ids": [(6, 0, cls.team.ids)],
            }
        )
        guest = cls.env["mail.guest"].create({"name": "Activity Menu Guest"})
        identity = cls.env["contact.center.identity"].create(
            {
                "name": guest.name,
                "company_id": cls.env.company.id,
                "mail_guest_id": guest.id,
            }
        )
        cls.channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=cls.account,
            identity=identity,
            conversation_type="direct",
            name="Activity Menu Conversation",
            teams=cls.team,
            guest_ids=guest.ids,
        )
        cls.env["contact.center.channel.binding"].create(
            {
                "channel_id": cls.channel.id,
                "account_id": cls.account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": "activity-menu-%s" % uuid.uuid4(),
            }
        )
        cls.native = cls.env["mail.channel"].create(
            {
                "name": "Native Discuss",
                "channel_type": "channel",
                "channel_partner_ids": [(4, cls.agent.partner_id.id)],
            }
        )

    def _schedule(self, days=0):
        return (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .schedule_followup(
                self.channel.id,
                {
                    "summary": "Contact Center reminder",
                    "user_id": self.agent.id,
                    "date_deadline": fields.Date.to_string(
                        fields.Date.today() + datetime.timedelta(days=days)
                    ),
                    "client_request_id": str(uuid.uuid4()),
                },
            )
        )

    def _native_activity(self):
        activity = (
            self.env["mail.activity"]
            .with_context(mail_activity_quick_update=True)
            .create(
                {
                    "res_model_id": self.env["ir.model"]._get_id("res.partner"),
                    "res_id": self.agent.partner_id.id,
                    "activity_type_id": self.env.ref("mail.mail_activity_data_todo").id,
                    "user_id": self.agent.id,
                    "summary": "Native reminder",
                    "date_deadline": fields.Date.today(),
                }
            )
        )
        # Native channels reject adding followers during activity creation.
        # Reassign an existing activity to exercise the systray's mixed input.
        activity.write(
            {
                "res_model_id": self.env["ir.model"]._get_id("mail.channel"),
                "res_id": self.native.id,
            }
        )
        return activity

    def test_contact_center_and_discuss_keep_distinct_groups_and_counts(self):
        self._schedule()
        self._schedule(2)
        self._native_activity()
        groups = self.env["res.users"].with_user(self.agent).systray_get_activities()
        contact = next(group for group in groups if group.get("contact_center"))
        native = next(group for group in groups if group["model"] == "mail.channel")
        self.assertNotEqual(contact["id"], native["id"])
        self.assertEqual(contact["model"], "contact.center.followup.request")
        self.assertEqual(contact["name"], "Contact Center")
        self.assertEqual(
            (contact["total_count"], contact["today_count"], contact["planned_count"]),
            (1, 1, 1),
        )
        self.assertEqual(
            (native["total_count"], native["today_count"], native["planned_count"]),
            (1, 1, 0),
        )
        self.assertNotIn("contact_center", native)
        native_records = (
            self.env["mail.channel"].with_user(self.agent).search(native["domain"])
        )
        self.assertIn(self.native, native_records)
        self.assertNotIn(self.channel, native_records)

    def test_losing_inbox_membership_hides_only_contact_center_group(self):
        scheduled = self._schedule()
        self._native_activity()
        replacement = self.agent.with_context(no_reset_password=True).copy(
            {
                "name": "Activity Menu Replacement",
                "login": "cc-menu-replacement-%s" % uuid.uuid4(),
            }
        )
        self.team.write({"agent_ids": [(4, replacement.id), (3, self.agent.id)]})
        activity = self.env["mail.activity"].browse(scheduled["activity"]["id"])
        self.assertEqual(activity.user_id, replacement)
        groups = self.env["res.users"].with_user(self.agent).systray_get_activities()
        self.assertFalse(any(group.get("contact_center") for group in groups))
        native = next(group for group in groups if group["model"] == "mail.channel")
        self.assertEqual(native["today_count"], 1)
