import uuid
from unittest import mock

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from ..services.adapter import AdapterResult, ProviderAdapter, adapter_registry


@adapter_registry.register("test.conversation.preference")
class ConversationPreferenceTestAdapter(ProviderAdapter):
    display_name = "Conversation Preference Test"

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        raise NotImplementedError

    def execute_command(self, connection, command):
        return AdapterResult.success(provider_response={"accepted": True})

    def get_capabilities(self, connection):
        return {"send_message": True}

    def get_health(self, connection):
        return {"state": "connected"}


class TestConversationLifecycleAndPreference(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent_a = cls._create_user("A", agent_group)
        cls.agent_b = cls._create_user("B", agent_group)
        cls.outsider = cls._create_user("Outsider", agent_group)
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Preference Team %s" % uuid.uuid4(),
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, (cls.agent_a | cls.agent_b).ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Preference Inbox %s" % uuid.uuid4(),
                "company_id": cls.env.company.id,
                "platform": "telegram",
                "external_ref": "preference-account-%s" % uuid.uuid4(),
                "default_team_id": cls.team.id,
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Preference Provider %s" % uuid.uuid4(),
                "account_id": cls.account.id,
                "adapter_key": "test.conversation.preference",
                "external_ref": "preference-provider-%s" % uuid.uuid4(),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {"send_message": True},
            }
        )

    @classmethod
    def _create_user(cls, label, group):
        token = uuid.uuid4().hex
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Preference Agent %s" % label,
                    "login": "cc-preference-%s-%s" % (label.lower(), token),
                    "email": "cc-preference-%s@example.invalid" % token,
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, group.ids)],
                }
            )
        )

    def _conversation(self, label):
        guest = self.env["mail.guest"].sudo().create({"name": "Guest %s" % label})
        identity = (
            self.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": "Guest %s" % label,
                    "company_id": self.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            identity=identity,
            conversation_type="direct",
            team=self.team,
            partner_ids=(self.agent_a | self.agent_b).partner_id.ids,
            guest_ids=guest.ids,
        )
        binding = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .create(
                {
                    "channel_id": channel.id,
                    "account_id": self.account.id,
                    "identity_id": identity.id,
                    "conversation_type": "direct",
                    "conversation_ref": "preference-%s-%s" % (label, uuid.uuid4()),
                }
            )
        )
        return channel, binding, guest

    def _inbound(self, channel, guest, body, date=None):
        values = {
            "origin": "inbound",
            "body": body,
            "message_type": "comment",
            "subtype_xmlid": "mail.mt_comment",
            "author_guest_id": guest.id,
            "partner_ids": [],
        }
        if date:
            values["date"] = date
        message = channel._contact_center_post(
            **values,
        )
        self.env["contact.center.application"]._publish_message_created(
            channel, message, direction="inbound"
        )
        return message

    def test_states_include_archive_and_preference_is_personal(self):
        channel, _binding, _guest = self._conversation("personal")
        api_a = self.env["contact.center.ui.api"].with_user(self.agent_a)
        api_b = self.env["contact.center.ui.api"].with_user(self.agent_b)

        self.assertEqual(
            [item["key"] for item in api_a.bootstrap()["states"]["conversation"]],
            ["open", "resolved", "archived"],
        )
        archived = api_a.update_conversation(channel.id, {"state": "archived"})
        self.assertEqual(archived["item"]["state"], "archived")
        self.assertFalse(archived["item"]["preference"]["pinned"])

        pinned = api_a.set_conversation_preference(
            channel.id, {"pinned": True, "muted": True}
        )["item"]
        self.assertTrue(pinned["preference"]["pinned"])
        self.assertTrue(pinned["preference"]["pinned_at"])
        self.assertTrue(pinned["preference"]["muted"])
        pinned_again = api_a.set_conversation_preference(channel.id, {"pinned": True})[
            "item"
        ]
        self.assertEqual(
            pinned_again["preference"]["pinned_at"],
            pinned["preference"]["pinned_at"],
        )
        self.assertEqual(
            api_b.get_conversation(channel.id)["item"]["preference"],
            {"pinned": False, "pinned_at": False, "muted": False},
        )

        preferences = self.env["contact.center.conversation.preference"].with_user(
            self.agent_b
        )
        self.assertFalse(preferences.search([]))
        with self.assertRaises(AccessError):
            preferences.create(
                {
                    "channel_id": channel.id,
                    "user_id": self.agent_a.id,
                    "muted": True,
                }
            )
        with self.assertRaises(AccessError):
            self.env["contact.center.ui.api"].with_user(
                self.outsider
            ).set_conversation_preference(channel.id, {"pinned": True})

    def test_false_preferences_remove_sparse_row(self):
        channel, _binding, _guest = self._conversation("sparse")
        api = self.env["contact.center.ui.api"].with_user(self.agent_a)
        api.set_conversation_preference(channel.id, {"pinned": True, "muted": True})
        muted_only = api.set_conversation_preference(channel.id, {"pinned": False})[
            "item"
        ]["preference"]
        self.assertFalse(muted_only["pinned"])
        self.assertTrue(muted_only["muted"])
        api.set_conversation_preference(channel.id, {"pinned": False, "muted": False})
        self.assertFalse(
            self.env["contact.center.conversation.preference"]
            .sudo()
            .search(
                [
                    ("channel_id", "=", channel.id),
                    ("user_id", "=", self.agent_a.id),
                ]
            )
        )

    def test_pinned_conversations_precede_activity_and_cursor_is_stable(self):
        api = self.env["contact.center.ui.api"].with_user(self.agent_a)
        channels = []
        for label, date in (
            ("old", "2026-09-04 10:00:00"),
            ("middle", "2026-09-04 11:00:00"),
            ("new", "2026-09-04 12:00:00"),
        ):
            channel, _binding, guest = self._conversation(label)
            self._inbound(channel, guest, label, fields.Datetime.to_datetime(date))
            channels.append(channel)
        with mock.patch.object(
            fields.Datetime,
            "now",
            side_effect=(
                fields.Datetime.to_datetime("2026-09-04 13:00:00"),
                fields.Datetime.to_datetime("2026-09-04 14:00:00"),
            ),
        ):
            api.set_conversation_preference(channels[0].id, {"pinned": True})
            api.set_conversation_preference(channels[1].id, {"pinned": True})

        first = api.list_conversations(limit=1)
        self.assertEqual(
            [item["channel_id"] for item in first["items"]],
            [channels[1].id],
        )
        self.assertEqual(first["next_cursor"]["segment"], "pinned")
        second = api.list_conversations(limit=1, cursor=first["next_cursor"])
        self.assertEqual(
            [item["channel_id"] for item in second["items"]], [channels[0].id]
        )
        self.assertEqual(second["next_cursor"]["segment"], "pinned")
        third = api.list_conversations(limit=1, cursor=second["next_cursor"])
        self.assertEqual(
            [item["channel_id"] for item in third["items"]], [channels[2].id]
        )
        self.assertFalse(third["has_more"])

        with self.assertRaisesRegex(ValidationError, "cursor segment"):
            api.list_conversations(
                limit=1,
                cursor={
                    "last_activity_at": "2026-09-04 12:00:00",
                    "channel_id": channels[2].id,
                },
            )

    def test_anchor_page_starts_at_first_unread_without_advancing_seen(self):
        channel, _binding, guest = self._conversation("unread")
        api = self.env["contact.center.ui.api"].with_user(self.agent_a)
        messages = [self._inbound(channel, guest, "message 0")]
        api.mark_seen(channel.id, messages[0].id)
        note = api.post_internal_note(
            channel.id, "Timeline-only note", str(uuid.uuid4())
        )["message"]
        self.assertGreater(note["message_id"], messages[0].id)
        messages.extend(
            [
                self._inbound(channel, guest, "message %s" % index)
                for index in range(1, 6)
            ]
        )
        member = channel.with_user(
            self.agent_a
        )._contact_center_member_for_current_user()
        member.invalidate_recordset(["seen_message_id"])
        seen_before = member.seen_message_id

        conversation = api.get_conversation(channel.id)["item"]
        # Internal notes deliberately do not increase the operational unread
        # counter, so they cannot become the first-unread scroll anchor either.
        self.assertEqual(conversation["first_unread_message_id"], messages[1].id)
        page = api.get_timeline(
            channel.id,
            anchor_message_id=conversation["first_unread_message_id"],
            limit=2,
        )
        self.assertEqual(
            [item["message_id"] for item in page["items"]],
            [messages[1].id, messages[2].id],
        )
        self.assertEqual(page["anchor_message_id"], messages[1].id)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_before_message_id"], messages[1].id)
        self.assertTrue(page["has_more_forward"])
        self.assertEqual(page["next_after_message_id"], messages[2].id)
        member.invalidate_recordset(["seen_message_id"])
        self.assertEqual(member.seen_message_id, seen_before)

        with self.assertRaisesRegex(ValidationError, "mutually exclusive"):
            api.get_timeline(
                channel.id,
                before_message_id=messages[1].id,
                anchor_message_id=messages[2].id,
            )

    def test_muting_suppresses_attention_but_not_realtime_delivery(self):
        channel, _binding, _guest = self._conversation("mute")
        self.env["contact.center.ui.api"].with_user(
            self.agent_a
        ).set_conversation_preference(channel.id, {"muted": True})
        sent = []

        def capture(_bus, notifications):
            sent.extend(notifications)
            return True

        with mock.patch.object(
            type(self.env["bus.bus"]),
            "_sendmany",
            autospec=True,
            side_effect=capture,
        ):
            self.env["contact.center.application"]._notify_ui(
                channel,
                "message_created",
                {"message_id": 999, "direction": "inbound"},
            )

        by_partner = {partner.id: payload for partner, _topic, payload in sent}
        self.assertIn(self.agent_a.partner_id.id, by_partner)
        self.assertIn(self.agent_b.partner_id.id, by_partner)
        self.assertFalse(by_partner[self.agent_a.partner_id.id]["personal_attention"])
        self.assertTrue(by_partner[self.agent_b.partner_id.id]["personal_attention"])
        self.assertEqual(
            by_partner[self.agent_a.partner_id.id]["event_type"], "message_created"
        )
