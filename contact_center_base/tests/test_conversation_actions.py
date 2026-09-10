import uuid
from unittest import mock

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase
from odoo.tools import html2plaintext


class TestConversationUnreadAndActivityNotes(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        supervisor_group = cls.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        cls.agent = cls._user("Agent", agent_group)
        cls.colleague = cls._user("Colleague", agent_group)
        cls.supervisor = cls._user("Supervisor", supervisor_group)
        cls.outsider = cls._user("Outsider", agent_group)
        cls.members = cls.agent | cls.colleague | cls.supervisor
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Conversation actions",
                "company_id": cls.env.company.id,
                "platform": "telegram",
                "external_ref": "conversation-actions-%s" % uuid.uuid4(),
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
                    "name": "Conversation %s" % name,
                    "login": "conversation-action-%s" % token,
                    "email": "conversation-action-%s@example.invalid" % token,
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, group.ids)],
                }
            )
        )

    def _conversation(self, conversation_type="direct"):
        guest = self.env["mail.guest"].sudo().create({"name": "Actions guest"})
        identity = (
            self.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": "Actions guest",
                    "company_id": self.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            identity=identity,
            conversation_type=conversation_type,
            partner_ids=self.members.partner_id.ids,
            guest_ids=guest.ids,
        )
        self.env["contact.center.channel.binding"].sudo().create(
            {
                "channel_id": channel.id,
                "account_id": self.account.id,
                "identity_id": identity.id,
                "conversation_type": conversation_type,
                "conversation_ref": "conversation-action-%s" % uuid.uuid4(),
            }
        )
        return channel, guest

    def _api(self, user=None):
        return self.env["contact.center.ui.api"].with_user(user or self.agent)

    def _message(self, channel, guest, body, date):
        message = channel._contact_center_post(
            origin="inbound",
            body=body,
            date=date,
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_guest_id=guest.id,
            partner_ids=[],
        )
        self.env["contact.center.application"]._publish_message_created(
            channel, message, direction="inbound"
        )
        return message

    def _member(self, channel, user=None):
        return channel.with_user(
            user or self.agent
        )._contact_center_member_for_current_user()

    def _notes(self, channel):
        return (
            self.env["contact.center.internal.note.request"]
            .sudo()
            .search([("channel_id", "=", channel.id)], order="id")
        )

    def test_admin_capabilities_ignore_disabled_inbox_flags(self):
        admin = self._user(
            "Administrator",
            self.env.ref("contact_center_base.group_contact_center_admin"),
        )
        self.account.write(
            {
                "access_user_ids": [(4, admin.id)],
                "attribution_ui_enabled": False,
                "conversation_delete_enabled": False,
                "conversation_ignore_enabled": False,
            }
        )
        channel, _guest = self._conversation()
        admin_item = self._api(admin).get_conversation(channel.id)["item"]
        agent_item = self._api().get_conversation(channel.id)["item"]
        for capability in (
            "view_attribution",
            "delete_conversation",
            "ignore_conversation",
        ):
            self.assertTrue(admin_item["capabilities"][capability])
            self.assertFalse(agent_item["capabilities"][capability])

    def test_ignore_capability_is_not_advertised_for_other_conversation_types(self):
        self.account.write(
            {
                "conversation_delete_enabled": True,
                "conversation_ignore_enabled": True,
            }
        )
        channel, _guest = self._conversation(conversation_type="other")
        item = self._api().get_conversation(channel.id)["item"]
        self.assertTrue(item["capabilities"]["delete_conversation"])
        self.assertFalse(item["capabilities"]["ignore_conversation"])

    def test_unread_rewinds_only_current_member_and_never_enqueues_receipt(self):
        channel, guest = self._conversation()
        first = self._message(channel, guest, "First", "2026-09-08 10:00:00")
        last = self._message(channel, guest, "Last", "2026-09-08 11:00:00")
        api = self._api()
        api.mark_seen(channel.id, last.id)
        self._api(self.colleague).mark_seen(channel.id, last.id)
        member = self._member(channel)
        seen_at = member.last_seen_dt
        outbox_ids = self.env["contact.center.outbox.command"].sudo().search([]).ids
        events = []

        def notify(_application, target, event_type, payload, partner_ids=None):
            events.append((target.id, event_type, payload, partner_ids))

        application_class = type(self.env["contact.center.application"])
        with mock.patch.object(
            application_class, "_queue_direct_mark_read"
        ) as enqueue, mock.patch.object(application_class, "_notify_ui", new=notify):
            result = api.mark_conversation_unread(channel.id)["item"]
        enqueue.assert_not_called()
        member.invalidate_recordset()
        self.assertEqual(member.seen_message_id, first)
        self.assertEqual(member.fetched_message_id, last)
        self.assertEqual(member.last_seen_dt, seen_at)
        self.assertEqual(self._member(channel, self.colleague).seen_message_id, last)
        self.assertEqual(result["unread_count"], 1)
        self.assertEqual(result["first_unread_message_id"], last.id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][3], self.agent.partner_id.ids)
        self.assertEqual(
            self.env["contact.center.outbox.command"].sudo().search([]).ids,
            outbox_ids,
        )
        # Reopening the conversation advances the same native pointer again.
        api.mark_seen(channel.id, last.id)
        reopened = api.get_conversation(channel.id)["item"]
        self.assertEqual(reopened["unread_count"], 0)
        self.assertFalse(reopened["first_unread_message_id"])

    def test_unread_uses_message_dates_and_preserves_existing_boundary(self):
        channel, guest = self._conversation()
        latest = self._message(channel, guest, "Latest", "2026-09-08 12:00:00")
        earliest = self._message(channel, guest, "Backfill", "2026-09-08 10:00:00")
        middle = self._message(channel, guest, "Middle", "2026-09-08 11:00:00")
        self.assertGreater(middle.id, latest.id)
        api = self._api()
        api.mark_seen(channel.id, latest.id)
        result = api.mark_conversation_unread(channel.id)["item"]
        self.assertEqual(self._member(channel).seen_message_id, middle)
        self.assertEqual(result["first_unread_message_id"], latest.id)
        self.assertEqual(result["unread_count"], 1)
        repeated = api.mark_conversation_unread(channel.id)["item"]
        self.assertEqual(repeated["first_unread_message_id"], latest.id)
        # A colleague's naturally unread history keeps its original anchor.
        original = self._api(self.colleague).mark_conversation_unread(channel.id)[
            "item"
        ]
        self.assertEqual(original["first_unread_message_id"], earliest.id)
        self.assertEqual(original["unread_count"], 3)

    def test_unread_handles_single_message_and_excludes_later_notes(self):
        channel, guest = self._conversation()
        message = self._message(channel, guest, "Only", "2026-09-08 10:00:00")
        api = self._api()
        note = api.post_internal_note(channel.id, "Internal", str(uuid.uuid4()))
        api.mark_seen(channel.id, note["message"]["message_id"])
        result = api.mark_conversation_unread(channel.id)["item"]
        self.assertFalse(self._member(channel).seen_message_id)
        self.assertEqual(result["unread_count"], 1)
        self.assertEqual(result["first_unread_message_id"], message.id)

    def test_empty_and_note_only_conversations_stay_without_unread_work(self):
        channel, _guest = self._conversation()
        api = self._api()
        self.assertEqual(
            api.mark_conversation_unread(channel.id)["item"]["unread_count"], 0
        )
        api.post_internal_note(channel.id, "Internal only", str(uuid.uuid4()))
        result = api.mark_conversation_unread(channel.id)["item"]
        self.assertEqual(result["unread_count"], 0)
        self.assertFalse(result["first_unread_message_id"])

    def test_unread_requires_membership_and_canonical_id(self):
        channel, _guest = self._conversation()
        with self.assertRaises(AccessError):
            self._api(self.outsider).mark_conversation_unread(channel.id)
        with self.assertRaises(ValidationError):
            self._api().mark_conversation_unread(True)

    def test_unread_locks_channel_before_member(self):
        channel, guest = self._conversation()
        message = self._message(channel, guest, "Only", "2026-09-08 10:00:00")
        self._api().mark_seen(channel.id, message.id)
        statements = []
        cursor_class = type(self.env.cr)
        execute = cursor_class.execute

        def trace(cursor, query, *args, **kwargs):
            statement = str(query)
            if "FOR UPDATE" in statement and (
                "FROM mail_channel WHERE" in statement
                or "FROM mail_channel_member WHERE" in statement
            ):
                statements.append(statement)
            return execute(cursor, query, *args, **kwargs)

        with mock.patch.object(cursor_class, "execute", new=trace):
            self._api().mark_conversation_unread(channel.id)
        self.assertIn("FROM mail_channel WHERE", statements[0])
        self.assertIn("FROM mail_channel_member WHERE", statements[1])

    def test_resolve_reopen_notes_are_internal_and_only_record_real_changes(self):
        channel, guest = self._conversation()
        last = self._message(channel, guest, "Last", "2026-09-08 10:00:00")
        api = self._api()
        api.mark_seen(channel.id, last.id)
        self._api(self.colleague).mark_seen(channel.id, last.id)
        outbox_ids = self.env["contact.center.outbox.command"].sudo().search([]).ids
        email_ids = self.env["mail.mail"].sudo().search([]).ids
        cursor = channel.contact_center_last_message_id
        api.update_conversation(channel.id, {"state": "resolved"})
        api.update_conversation(channel.id, {"state": "resolved"})
        notes = self._notes(channel)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes.requested_by_id, self.agent)
        self.assertEqual(notes.message_id.author_id, self.agent.partner_id)
        self.assertEqual(notes.message_id.subtype_id, self.env.ref("mail.mt_note"))
        self.assertFalse(notes.message_id.partner_ids)
        self.assertIn(self.agent.display_name, html2plaintext(notes.message_id.body))
        self.assertIn("resolved", html2plaintext(notes.message_id.body))
        api.update_conversation(channel.id, {"state": "open"})
        api.update_conversation(channel.id, {"state": "open"})
        self.assertEqual(len(self._notes(channel)), 2)
        self.assertIn(
            "reopened", html2plaintext(self._notes(channel)[-1].message_id.body)
        )
        self.assertEqual(channel.contact_center_last_message_id, cursor)
        for user in (self.agent, self.colleague):
            self.assertEqual(
                self._api(user).get_conversation(channel.id)["item"]["unread_count"],
                0,
            )
        self.assertEqual(
            self.env["contact.center.outbox.command"].sudo().search([]).ids,
            outbox_ids,
        )
        self.assertEqual(self.env["mail.mail"].sudo().search([]).ids, email_ids)

    def test_claim_transfer_and_unassignment_record_the_actual_actor(self):
        channel, _guest = self._conversation()
        self._api().claim_conversation(channel.id)
        self._api().claim_conversation(channel.id)
        notes = self._notes(channel)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes.requested_by_id, self.agent)
        self.assertIn("claimed", html2plaintext(notes.message_id.body))
        supervisor = self._api(self.supervisor)
        supervisor.update_conversation(
            channel.id, {"responsible_id": self.colleague.id}
        )
        supervisor.update_conversation(
            channel.id, {"responsible_id": self.colleague.id}
        )
        notes = self._notes(channel)
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[-1].requested_by_id, self.supervisor)
        self.assertIn(
            self.supervisor.display_name, html2plaintext(notes[-1].message_id.body)
        )
        self.assertIn(
            self.colleague.display_name, html2plaintext(notes[-1].message_id.body)
        )
        supervisor.update_conversation(channel.id, {"responsible_id": False})
        self.assertEqual(len(self._notes(channel)), 3)
        self.assertFalse(channel.contact_center_responsible_id)

    def test_combined_transition_creates_one_note_and_failure_rolls_back_action(self):
        channel, _guest = self._conversation()
        api = self._api(self.supervisor)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            with mock.patch.object(
                type(api),
                "_persist_internal_note",
                side_effect=ValidationError("Injected note failure"),
            ):
                api.update_conversation(channel.id, {"state": "resolved"})
        channel.invalidate_recordset()
        self.assertEqual(channel.contact_center_state, "open")
        self.assertFalse(self._notes(channel))
        api.update_conversation(
            channel.id,
            {"state": "resolved", "responsible_id": self.colleague.id},
        )
        notes = self._notes(channel)
        self.assertEqual(len(notes), 1)
        body = html2plaintext(notes.message_id.body)
        self.assertIn("resolved", body)
        self.assertIn(self.colleague.display_name, body)

    def test_denied_assignment_never_creates_an_activity_note(self):
        channel, _guest = self._conversation()
        with self.assertRaises(AccessError):
            self._api().update_conversation(
                channel.id, {"responsible_id": self.colleague.id}
            )
        self.assertFalse(self._notes(channel))
        self.assertFalse(channel.contact_center_responsible_id)

    def test_conversation_list_loads_ignore_rules_once_for_the_page(self):
        self.account.conversation_ignore_enabled = True
        channels = self.env["mail.channel"]
        for _index in range(3):
            channel, _guest = self._conversation()
            channels |= channel
        api = self._api()
        rule_class = type(self.env["contact.center.conversation.ignore"])
        native_search = rule_class.search
        searches = []

        def tracked_search(model, *args, **kwargs):
            searches.append(args)
            return native_search(model, *args, **kwargs)

        for ignored in (False, True, False):
            api.set_conversation_ignored(channels[0].id, ignored)
            searches.clear()
            with mock.patch.object(rule_class, "search", new=tracked_search):
                page = api.list_conversations(filters={"account_id": self.account.id})
            self.assertEqual(len(searches), 1)
            self.assertEqual(
                {item["channel_id"]: item["ignored"] for item in page["items"]},
                {
                    channel.id: bool(ignored and channel == channels[0])
                    for channel in channels
                },
            )
