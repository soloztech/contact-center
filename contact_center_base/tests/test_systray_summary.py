import uuid

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase


class TestSystraySummary(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent = cls._user("Agent", agent_group)
        cls.colleague = cls._user("Colleague", agent_group)
        cls.outsider = cls._user("Outsider", agent_group)
        cls.employee = cls._user("Employee", cls.env.ref("base.group_user"))
        cls.members = cls.agent | cls.colleague
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Systray summary",
                "company_id": cls.env.company.id,
                "platform": "telegram",
                "external_ref": "systray-summary-%s" % uuid.uuid4(),
                "access_user_ids": [(6, 0, cls.members.ids)],
            }
        )

    @classmethod
    def _user(cls, name, group):
        token = str(uuid.uuid4())
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Systray %s" % name,
                    "login": "systray-%s" % token,
                    "email": "systray-%s@example.invalid" % token,
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, group.ids)],
                }
            )
        )

    def _conversation(self):
        guest = self.env["mail.guest"].sudo().create({"name": "Systray guest"})
        identity = (
            self.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": "Systray guest",
                    "company_id": self.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            identity=identity,
            conversation_type="direct",
            partner_ids=self.members.partner_id.ids,
            guest_ids=guest.ids,
        )
        self.env["contact.center.channel.binding"].sudo().create(
            {
                "channel_id": channel.id,
                "account_id": self.account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": "systray-%s" % uuid.uuid4(),
            }
        )
        return channel, guest

    def _inbound(self, channel, guest, body="Olá"):
        return channel._contact_center_post(
            origin="inbound",
            body=body,
            date=fields.Datetime.now(),
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_guest_id=guest.id,
            partner_ids=[],
        )

    def _api(self, user=None):
        return self.env["contact.center.ui.api"].with_user(user or self.agent)

    def _listed(self, **filters):
        result = self._api().list_conversations(
            limit=100, filters=dict(filters, unread_only=True, exclude_muted=True)
        )
        return result["total"], {item["channel_id"] for item in result["items"]}

    def test_counts_unread_conversations_like_the_preset_lists(self):
        mine, mine_guest = self._conversation()
        # Claiming posts the agent's own note, which counts as read by them.
        self._api().claim_conversation(mine.id)
        self._inbound(mine, mine_guest)
        self._inbound(mine, mine_guest, "Segunda mensagem")
        waiting, waiting_guest = self._conversation()
        self._inbound(waiting, waiting_guest)
        muted, muted_guest = self._conversation()
        self._inbound(muted, muted_guest)
        self._api().set_conversation_preference(muted.id, {"muted": True})
        read, read_guest = self._conversation()
        seen = self._inbound(read, read_guest)
        self._api().mark_seen(read.id, seen.id)

        summary = self._api().systray_summary()
        self.assertEqual(
            (summary["enabled"], summary["mine_unread"], summary["all_unread"]),
            (True, 1, 2),
            "conversations, not messages; muted and read ones stay out",
        )
        self.assertEqual(
            self._listed(responsibility="mine"), (1, {mine.id}), "Mine opens them"
        )
        self.assertEqual(self._listed(), (2, {mine.id, waiting.id}))
        # Muting and reading are personal: the colleague still counts the muted
        # conversation and the one only the agent read.
        colleague = self._api(self.colleague).systray_summary()
        self.assertEqual(colleague["all_unread"], 4)
        self.assertEqual(colleague["mine_unread"], 0)
        # Only conversations where the user is a member are counted.
        self.assertEqual(self._api(self.outsider).systray_summary()["all_unread"], 0)

        # Another agent answering a read conversation makes it unread again.
        read.with_user(self.colleague)._contact_center_post(
            origin="outbound",
            body="Resposta do colega",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_id=self.colleague.partner_id.id,
            partner_ids=[],
        )
        self.assertEqual(self._api().systray_summary()["all_unread"], 3)
        self.assertEqual(self._listed()[0], 3)

        # Unmuting brings the conversation back without any new message.
        self._api().set_conversation_preference(muted.id, {"muted": False})
        self.assertEqual(self._api().systray_summary()["all_unread"], 4)

    def test_a_user_without_the_agent_role_sees_nothing(self):
        self.assertEqual(
            self._api(self.employee).systray_summary(),
            {"schema_version": 1, "enabled": False},
        )

    def test_the_muted_filter_must_be_boolean(self):
        with self.assertRaises(ValidationError):
            self._api().list_conversations(filters={"exclude_muted": "yes"})
