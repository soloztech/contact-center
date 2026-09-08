import datetime
import hashlib
import uuid

from odoo import fields
from odoo.tests.common import SavepointCase

from ..models.productivity import _PRODUCTIVITY_SERVICE_TOKEN


class TestContactCenterBaseProductivity(SavepointCase):
    """Keep provider-neutral productivity independently installable."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Base Productivity Agent",
                    "login": "cc-base-productivity-%s" % uuid.uuid4(),
                    "email": "cc-base-productivity@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, agent_group.ids)],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Base Productivity %s" % uuid.uuid4(),
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Base Productivity Inbox",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "base-productivity-%s" % uuid.uuid4(),
                "access_team_ids": [(6, 0, cls.team.ids)],
            }
        )
        guest = cls.env["mail.guest"].sudo().create({"name": "Base Guest"})
        identity = (
            cls.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": guest.name,
                    "company_id": cls.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        cls.channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=cls.account,
            identity=identity,
            conversation_type="direct",
            name="Base Productivity Conversation",
            teams=cls.team,
            guest_ids=guest.ids,
        )
        cls.env["contact.center.channel.binding"].sudo().create(
            {
                "channel_id": cls.channel.id,
                "account_id": cls.account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": "base-productivity-%s" % uuid.uuid4(),
            }
        )

    def _api(self):
        return self.env["contact.center.ui.api"].with_user(self.agent)

    def test_quick_replies_include_every_shared_access_team(self):
        other_team = self.env["contact.center.team"].create(
            {
                "name": "Shared reply team %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
            }
        )
        self.account.write({"access_team_ids": [(4, other_team.id)]})
        marker = uuid.uuid4().hex[:10]
        shortcode = self.env["mail.shortcode"].create(
            {
                "source": "other-team-%s" % marker,
                "substitution": "Reply from another authorized team",
                "description": marker,
            }
        )
        self.env["contact.center.quick.reply.binding"].create(
            {
                "shortcode_id": shortcode.id,
                "company_id": self.env.company.id,
                "scope": "team",
                "team_id": other_team.id,
            }
        )
        replies = self._api().search_quick_replies(self.channel.id, marker)
        self.assertEqual(len(replies["items"]), 1)
        self.assertEqual(replies["items"][0]["body"], shortcode.substitution)
        self.account.write({"access_team_ids": [(3, other_team.id)]})
        self.assertFalse(
            self._api().search_quick_replies(self.channel.id, marker)["items"]
        )

    def test_quick_reply_internal_note_and_schedule_are_base_features(self):
        marker = uuid.uuid4().hex[:10]
        shortcode = self.env["mail.shortcode"].create(
            {
                "source": "base-%s" % marker,
                "substitution": "Provider-neutral reply",
                "description": marker,
            }
        )
        self.env["contact.center.quick.reply.binding"].create(
            {
                "shortcode_id": shortcode.id,
                "company_id": self.env.company.id,
                "scope": "company",
            }
        )
        replies = self._api().search_quick_replies(self.channel.id, marker)
        self.assertEqual(replies["items"][0]["body"], "Provider-neutral reply")

        note = self._api().post_internal_note(
            self.channel.id, "Private operational context", str(uuid.uuid4())
        )
        self.assertEqual(note["message"]["content_type"], "note")

        body = "Scheduled from the base addon"
        scheduled = (
            self.env["contact.center.scheduled.message"]
            .with_context(
                contact_center_productivity_service_token=_PRODUCTIVITY_SERVICE_TOKEN
            )
            .create(
                {
                    "channel_id": self.channel.id,
                    "requested_by_id": self.agent.id,
                    "ui_request_id": str(uuid.uuid4()),
                    "body": body,
                    "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                    "scheduled_at": fields.Datetime.now()
                    + datetime.timedelta(minutes=10),
                }
            )
        )
        payload = self._api().get_productivity(self.channel.id)
        self.assertIn(
            scheduled.id, [item["id"] for item in payload["scheduled_messages"]]
        )
        self.assertNotIn("cases", payload)
        self.assertEqual(payload["activities"], [])
