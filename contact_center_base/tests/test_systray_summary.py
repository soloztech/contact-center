import datetime
import uuid
from unittest import mock

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

from ..services.tokens import CONTACT_CENTER_MEMBERSHIP_TOKEN, CONTACT_CENTER_POST_TOKEN


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
        ).with_context(contact_center_post_token=CONTACT_CENTER_POST_TOKEN)

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

    def _assert_unread_parity(self, channels):
        members = (
            self.env["mail.channel.member"]
            .with_user(self.agent)
            .search(
                [
                    ("channel_id", "in", channels.ids),
                    ("partner_id", "=", self.agent.partner_id.id),
                ]
            )
        )
        # Read EXISTS first: the exact compute must not flush pending fields for it.
        actual = set(
            self.env["mail.channel.member"]
            .with_user(self.agent)
            ._contact_center_unread_channel_ids()
        ) & set(channels.ids)
        members.invalidate_recordset(["message_unread_counter"])
        exact = set(
            members.filtered(lambda m: m.message_unread_counter > 0).channel_id.ids
        )
        self.assertEqual(actual, exact)
        return actual

    def test_exists_preserves_chronology_equal_dates_backfills_and_null_dates(self):
        channel, guest = self._conversation()
        first = self._inbound(channel, guest)
        second = self._inbound(channel, guest)
        day = datetime.datetime(2026, 1, 2, 12)
        (first | second).write({"date": day})
        self._api().mark_seen(channel.id, first.id)
        self.assertEqual(self._assert_unread_parity(channel), {channel.id})
        self._api().mark_seen(channel.id, second.id)
        self.assertEqual(self._assert_unread_parity(channel), set())
        backfill = self._inbound(channel, guest)
        backfill.write({"date": day - datetime.timedelta(days=1)})
        self.assertEqual(
            self._assert_unread_parity(channel),
            set(),
            "new ID with old date stays read",
        )
        # Legacy NULL dates use the same sentinel as the displayed exact count.
        backfill.write({"date": False})
        self.assertEqual(self._assert_unread_parity(channel), {channel.id})
        self._api().mark_seen(channel.id, backfill.id)
        self.assertEqual(self._assert_unread_parity(channel), set())
        later = self._inbound(channel, guest)
        later.write({"date": day + datetime.timedelta(days=1)})
        self.assertEqual(
            self._assert_unread_parity(channel), set(), "NULL seen sorts last"
        )
        later.write({"date": False})
        self.assertEqual(self._assert_unread_parity(channel), {channel.id})

    def test_exists_flushes_seen_and_message_fields_and_excludes_notes(self):
        channel, guest = self._conversation()
        message = self._inbound(channel, guest)
        member = channel.channel_member_ids.filtered(
            lambda m: m.partner_id == self.agent.partner_id
        )
        member.with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).seen_message_id = message
        self.assertEqual(self._assert_unread_parity(channel), set())
        self._api(self.colleague).post_internal_note(
            channel.id, "Nota interna", str(uuid.uuid4())
        )
        self.assertEqual(
            self._assert_unread_parity(channel), set(), "notes never create unread"
        )
        self.env["mail.message"].with_context(
            contact_center_post_token=CONTACT_CENTER_POST_TOKEN
        ).create(
            {
                "model": "mail.channel",
                "res_id": channel.id,
                "reply_to": False,
                "record_name": "Synthetic",
                "body": "notification",
                "message_type": "notification",
            }
        )
        self.env["mail.message"].with_context(
            contact_center_post_token=CONTACT_CENTER_POST_TOKEN
        ).create(
            {
                "model": "mail.channel",
                "res_id": channel.id,
                "reply_to": False,
                "record_name": "Synthetic",
                "body": "user notification",
                "message_type": "user_notification",
            }
        )
        self.assertEqual(self._assert_unread_parity(channel), set())
        newest = self._inbound(channel, guest)
        newest.date = fields.Datetime.now() + datetime.timedelta(days=1)
        self.assertEqual(self._assert_unread_parity(channel), {channel.id})
        newest.unlink()
        message.unlink()
        self.assertEqual(
            self._assert_unread_parity(channel),
            set(),
            "deleted seen does not count notes",
        )

    def test_summary_builds_one_unread_domain_without_exact_count_compute(self):
        channel, guest = self._conversation()
        self._inbound(channel, guest)
        api = self._api()
        cls = type(self.env["mail.channel.member"])
        original = cls._contact_center_unread_channel_ids
        with mock.patch.object(
            cls,
            "_contact_center_unread_channel_ids",
            autospec=True,
            side_effect=original,
        ) as exists, mock.patch.object(
            type(self.env["mail.channel.member"]),
            "_compute_message_unread",
            side_effect=AssertionError("summary must not compute exact counts"),
        ):
            self.assertEqual(api.systray_summary()["all_unread"], 1)
            self.assertEqual(exists.call_count, 1)

    def test_empty_and_unseen_conversations_and_membership_scope(self):
        empty, _ = self._conversation()
        unread, guest = self._conversation()
        self._inbound(unread, guest)
        self.assertEqual(self._assert_unread_parity(empty | unread), {unread.id})
        self.assertEqual(
            self.env["mail.channel.member"]
            .with_user(self.outsider)
            ._contact_center_unread_channel_ids(),
            [],
        )
        unread._contact_center_reconcile_members(
            partner_ids=self.colleague.partner_id.ids, guest_ids=guest.ids
        )
        self.assertNotIn(
            unread.id,
            self.env["mail.channel.member"]
            .with_user(self.agent)
            ._contact_center_unread_channel_ids(),
        )

    def test_unread_scope_is_recomputed_for_allowed_companies(self):
        company = self.env["res.company"].create({"name": "Unread other company"})
        self.agent.company_ids |= company
        account = self.account.copy(
            {"company_id": company.id, "access_user_ids": [(6, 0, self.agent.ids)]}
        )
        guest = self.env["mail.guest"].sudo().create({"name": "Other company guest"})
        identity = (
            self.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": "Other guest",
                    "company_id": company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        channel = (
            self.env["mail.channel"]
            .with_company(company)
            ._contact_center_create_channel(
                account=account,
                identity=identity,
                conversation_type="direct",
                partner_ids=self.agent.partner_id.ids,
                guest_ids=guest.ids,
            )
        )
        self._inbound(channel, guest)
        model = self.env["mail.channel.member"].with_user(self.agent)
        restricted = model.with_context(allowed_company_ids=self.env.company.ids)
        expanded = model.with_context(
            allowed_company_ids=(self.env.company | company).ids
        )
        self.assertNotIn(channel.id, restricted._contact_center_unread_channel_ids())
        self.assertIn(channel.id, expanded._contact_center_unread_channel_ids())
        self.assertNotIn(
            channel.id,
            restricted._contact_center_unread_channel_ids(),
            "no cross-context cache",
        )
