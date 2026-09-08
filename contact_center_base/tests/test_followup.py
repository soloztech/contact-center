import datetime
import threading
import uuid
from unittest import mock

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import html2plaintext

from ..models.productivity import _PRODUCTIVITY_SERVICE_TOKEN
from ..services.tokens import CONTACT_CENTER_MEMBERSHIP_TOKEN, CONTACT_CENTER_POST_TOKEN
from .test_contact_center import FakeAdapter  # noqa: F401


class TestContactCenterFollowup(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        supervisor_group = cls.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Productivity Agent",
                    "login": "cc-productivity-agent-%s" % uuid.uuid4(),
                    "email": "cc-productivity-agent@example.invalid",
                    "tz": "America/Sao_Paulo",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, agent_group.ids)],
                }
            )
        )
        cls.supervisor = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Productivity Supervisor",
                    "login": "cc-productivity-supervisor-%s" % uuid.uuid4(),
                    "email": "cc-productivity-supervisor@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, supervisor_group.ids)],
                }
            )
        )
        cls.outsider = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Productivity Outsider",
                    "login": "cc-productivity-outsider-%s" % uuid.uuid4(),
                    "email": "cc-productivity-outsider@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, cls.env.ref("base.group_user").ids)],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Productivity Team %s" % uuid.uuid4(),
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
                "supervisor_ids": [(6, 0, cls.supervisor.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Productivity Inbox",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "productivity-account-%s" % uuid.uuid4(),
                "access_team_ids": [(6, 0, [cls.team.id])],
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Productivity Provider",
                "account_id": cls.account.id,
                "adapter_key": "test.fake",
                "external_ref": "productivity-provider-%s" % uuid.uuid4(),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {
                    "send_message": True,
                    "sender_signature": True,
                },
                "last_state_observed_at": fields.Datetime.now(),
                "last_health_at": fields.Datetime.now(),
            }
        )
        cls.channel, cls.binding, cls.identity = cls._create_conversation("direct")

    @classmethod
    def _create_conversation(cls, conversation_type):
        token = uuid.uuid4().hex
        guest = (
            cls.env["mail.guest"]
            .sudo()
            .create({"name": "Productivity Guest %s" % token[:8]})
        )
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
        channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=cls.account,
            identity=identity if conversation_type == "direct" else None,
            conversation_type=conversation_type,
            name="Productivity %s %s" % (conversation_type, token[:8]),
            teams=cls.team,
            guest_ids=guest.ids,
        )
        binding = (
            cls.env["contact.center.channel.binding"]
            .sudo()
            .create(
                {
                    "channel_id": channel.id,
                    "account_id": cls.account.id,
                    "identity_id": (
                        identity.id if conversation_type == "direct" else False
                    ),
                    "conversation_type": conversation_type,
                    "conversation_ref": "productivity-%s-%s"
                    % (conversation_type, token),
                }
            )
        )
        cls.env["contact.center.channel.alias"].sudo().create(
            {
                "channel_binding_id": binding.id,
                "account_id": cls.account.id,
                "namespace": (
                    "whatsapp.group" if conversation_type == "group" else "whatsapp.pn"
                ),
                "value_raw": (
                    "%s@g.us" if conversation_type == "group" else "55%s@s.whatsapp.net"
                )
                % token,
                "value_normalized": (
                    "%s@g.us" if conversation_type == "group" else "55%s@s.whatsapp.net"
                )
                % token,
                "role": "primary",
            }
        )
        return channel, binding, identity

    def _api(self, user=None):
        return self.env["contact.center.ui.api"].with_user(user or self.agent)

    def _today(self, user=None):
        return fields.Date.context_today(self.channel.with_user(user or self.agent))

    def test_followups_belong_to_conversation_and_complete_idempotently(self):
        deadline = fields.Date.to_string(self._today())
        first_request_id = str(uuid.uuid4())
        second_request_id = str(uuid.uuid4())

        first = self._api().schedule_followup(
            self.channel.id,
            {
                "client_request_id": first_request_id,
                "date_deadline": deadline,
                "summary": "Call customer",
                "note": "Confirm the first proposal",
                "user_id": self.agent.id,
            },
        )
        second = self._api().schedule_followup(
            self.channel.id,
            {
                "client_request_id": second_request_id,
                "date_deadline": deadline,
                "summary": "Review second proposal",
                "user_id": self.supervisor.id,
            },
        )
        first_replay = self._api().schedule_followup(
            self.channel.id,
            {
                "client_request_id": first_request_id,
                "date_deadline": deadline,
                "summary": "Call customer",
                "note": "Confirm the first proposal",
                "user_id": self.agent.id,
            },
        )

        self.assertEqual(first["activity"]["channel_id"], self.channel.id)
        self.assertEqual(second["activity"]["channel_id"], self.channel.id)
        self.assertEqual(first_replay["activity"]["id"], first["activity"]["id"])
        with self.assertRaises(ValidationError):
            self._api().schedule_followup(
                self.channel.id,
                {
                    "client_request_id": first_request_id,
                    "date_deadline": deadline,
                    "summary": "A different activity",
                    "user_id": self.agent.id,
                },
            )
        productivity = self._api().get_productivity(self.channel.id)
        self.assertEqual(
            {item["channel_id"] for item in productivity["activities"]},
            {self.channel.id},
        )
        chatter_before = (
            self.env["mail.message"]
            .sudo()
            .search_count(
                [
                    ("model", "=", "mail.channel"),
                    ("res_id", "=", self.channel.id),
                ]
            )
        )
        with self.assertRaisesRegex(ValidationError, "UUID"):
            self._api(self.supervisor).complete_followup(
                self.channel.id,
                first["activity"]["id"],
                "Missing idempotency key",
            )
        completion_request_id = str(uuid.uuid4())
        completed = self._api(self.supervisor).complete_followup(
            self.channel.id,
            first["activity"]["id"],
            "Customer contacted",
            completion_request_id,
        )
        completed_replay = self._api(self.supervisor).complete_followup(
            self.channel.id,
            first["activity"]["id"],
            "Customer contacted",
            completion_request_id,
        )
        self.assertTrue(completed["activity"]["completed"])
        self.assertEqual(completed["activity"]["state"], "completed")
        self.assertEqual(completed_replay, completed)
        self.assertFalse(
            self.env["mail.activity"].browse(first["activity"]["id"]).exists()
        )
        self.assertEqual(
            self.env["mail.message"]
            .sudo()
            .search_count(
                [
                    ("model", "=", "mail.channel"),
                    ("res_id", "=", self.channel.id),
                ]
            ),
            chatter_before + 1,
        )
        with self.assertRaises(ValidationError):
            self._api().complete_followup(
                self.channel.id,
                first["activity"]["id"],
                "Different feedback",
                completion_request_id,
            )
        with self.assertRaises(AccessError):
            self._api(self.agent).complete_followup(
                self.channel.id,
                second["activity"]["id"],
                client_request_id=str(uuid.uuid4()),
            )

    def test_membership_revocation_reassigns_followup_and_removes_internal_follower(
        self,
    ):
        request_id = str(uuid.uuid4())
        result = self._api().schedule_followup(
            self.channel.id,
            {
                "client_request_id": request_id,
                "date_deadline": fields.Date.to_string(self._today()),
                "summary": "Reassign after revocation",
                "user_id": self.supervisor.id,
            },
        )
        self.team.write({"supervisor_ids": [(5, 0, 0)]})

        activity = self.env["mail.activity"].browse(result["activity"]["id"])
        activity.invalidate_recordset(["user_id"])
        self.assertEqual(activity.user_id, self.agent)
        self.assertNotIn(
            self.supervisor.partner_id, self.channel.channel_member_ids.partner_id
        )
        receipt = (
            self.env["contact.center.followup.request"]
            .sudo()
            .search(
                [
                    ("channel_id", "=", self.channel.id),
                    ("ui_request_id", "=", request_id),
                ]
            )
        )
        self.assertEqual(receipt.state, "active")
        self.assertEqual(receipt.activity_id, activity)

    def test_native_activity_completion_creates_a_replayable_tombstone(self):
        channel = self.channel
        activity = channel.activity_schedule(
            activity_type_id=self.env.ref("mail.mail_activity_data_todo").id,
            date_deadline=self._today(),
            summary="Native activity",
            user_id=self.agent.id,
        )
        activity_id = activity.id
        request_id = str(uuid.uuid4())
        chatter_before = (
            self.env["mail.message"]
            .sudo()
            .search_count([("model", "=", "mail.channel"), ("res_id", "=", channel.id)])
        )

        completed = self._api().complete_followup(
            self.channel.id,
            activity_id,
            "Native activity completed",
            request_id,
        )
        replay = self._api().complete_followup(
            self.channel.id,
            activity_id,
            "Native activity completed",
            request_id,
        )

        self.assertEqual(replay, completed)
        receipt = (
            self.env["contact.center.followup.request"]
            .sudo()
            .search([("activity_record_id", "=", activity_id)])
        )
        self.assertEqual(len(receipt), 1)
        self.assertEqual(receipt.state, "completed")
        self.assertFalse(receipt.ui_request_id)
        self.assertEqual(receipt.completion_request_id, request_id)
        self.assertEqual(
            self.env["mail.message"]
            .sudo()
            .search_count(
                [("model", "=", "mail.channel"), ("res_id", "=", channel.id)]
            ),
            chatter_before + 1,
        )

    def test_native_activity_changes_publish_one_productivity_invalidation(self):
        channel = self.channel
        application = self.env["contact.center.application"]
        activity_type = self.env.ref("mail.mail_activity_data_todo")
        values = {
            "activity_type_id": activity_type.id,
            "date_deadline": self._today(),
            "res_id": channel.id,
            "res_model_id": self.env["ir.model"]._get_id("mail.channel"),
            "summary": "Native productivity invalidation",
            "user_id": self.agent.id,
        }

        with mock.patch.object(
            type(application), "_notify_ui", autospec=True
        ) as notify_ui:
            activities = self.env["mail.activity"].create([values, dict(values)])

        self.assertEqual(notify_ui.call_count, 1)
        self.assertEqual(notify_ui.call_args.args[2], "productivity_updated")
        self.assertEqual(notify_ui.call_args.args[1].id, self.channel.id)
        self.assertEqual(
            notify_ui.call_args.args[3], {"reason": "mail_activity_changed"}
        )

        with mock.patch.object(
            type(application), "_notify_ui", autospec=True
        ) as notify_ui:
            activities.write({"summary": "Native productivity changed"})
        self.assertEqual(notify_ui.call_count, 1)

        with mock.patch.object(
            type(application), "_notify_ui", autospec=True
        ) as notify_ui:
            activities.unlink()
        self.assertEqual(notify_ui.call_count, 1)

    def test_native_activity_rejects_assignee_outside_inbox_scope(self):
        channel = self.channel
        values = {
            "activity_type_id": self.env.ref("mail.mail_activity_data_todo").id,
            "date_deadline": self._today(),
            "res_id": channel.id,
            "res_model_id": self.env["ir.model"]._get_id("mail.channel"),
            "summary": "Must remain in the inbox",
            "user_id": self.outsider.id,
        }

        with self.assertRaises(ValidationError):
            self.env["mail.activity"].with_user(self.agent).create(values)

        values["user_id"] = self.agent.id
        activity = self.env["mail.activity"].with_user(self.agent).create(values)
        with self.assertRaises(ValidationError):
            activity.with_user(self.agent).write({"user_id": False})

    def test_native_context_defaults_are_fenced_and_validate_assignee(self):
        values = {
            "activity_type_id": self.env.ref("mail.mail_activity_data_todo").id,
            "summary": "Validate effective defaults",
        }
        model = self.env["mail.activity"].with_context(
            default_res_model_id=self.env["ir.model"]._get_id("mail.channel"),
            default_res_id=self.channel.id,
            default_user_id=self.agent.id,
        )
        before = self.env["mail.activity"].search_count([])
        with self.assertRaises(AccessError):
            model.with_user(self.outsider).create(values)
        with self.assertRaises(ValidationError):
            model.with_user(self.agent).with_context(
                default_user_id=self.outsider.id
            ).create(values)
        self.assertEqual(self.env["mail.activity"].search_count([]), before)
        activity = model.with_user(self.agent).create(values)
        self.assertEqual(activity.res_model, "mail.channel")
        self.assertEqual(activity.res_id, self.channel.id)
        self.assertEqual(activity.user_id, self.agent)

    def test_readonly_model_name_cannot_hide_effective_conversation_target(self):
        model = (
            self.env["mail.activity"]
            .with_user(self.agent)
            .with_context(
                default_res_model_id=self.env["ir.model"]._get_id("mail.channel"),
                default_res_id=self.channel.id,
                default_user_id=self.agent.id,
            )
        )
        values = {
            "activity_type_id": self.env.ref("mail.mail_activity_data_todo").id,
            "summary": "Do not trust the related model name",
        }
        with self.assertRaisesRegex(ValidationError, "do not match"):
            model.create(dict(values, res_model="res.partner"))
        activity = model.create(values)
        with self.assertRaisesRegex(ValidationError, "do not match"):
            activity.write({"res_model": "res.partner"})
        self.assertEqual(activity.res_model, "mail.channel")
        self.assertEqual(activity.res_id, self.channel.id)

    def test_receipted_activity_cannot_be_retargeted_and_snapshot_stays_current(self):
        secondary, _binding, _identity = self._create_conversation("direct")
        scheduled = self._api().schedule_followup(
            self.channel.id,
            {
                "client_request_id": str(uuid.uuid4()),
                "date_deadline": fields.Date.to_string(self._today()),
                "summary": "Original summary",
                "user_id": self.agent.id,
            },
        )
        activity = self.env["mail.activity"].browse(scheduled["activity"]["id"])
        receipt = (
            self.env["contact.center.followup.request"]
            .sudo()
            .search([("activity_record_id", "=", activity.id)], limit=1)
        )

        activity.with_user(self.agent).write({"summary": "Updated natively"})
        receipt.invalidate_recordset(["activity_snapshot_json"])
        self.assertEqual(receipt.activity_snapshot_json["summary"], "Updated natively")
        with self.assertRaises(AccessError):
            activity.with_user(self.agent).write({"res_id": secondary.id})
        activity.invalidate_recordset(["res_id"])
        self.assertEqual(activity.res_id, self.channel.id)
        self.assertEqual(receipt.channel_id, self.channel)

    def test_native_unlink_closes_active_receipt_with_last_snapshot(self):
        scheduled = self._api().schedule_followup(
            self.channel.id,
            {
                "client_request_id": str(uuid.uuid4()),
                "date_deadline": fields.Date.to_string(self._today()),
                "summary": "Close outside Contact Center",
                "user_id": self.agent.id,
            },
        )
        activity = self.env["mail.activity"].browse(scheduled["activity"]["id"])
        receipt = (
            self.env["contact.center.followup.request"]
            .sudo()
            .search([("activity_record_id", "=", activity.id)], limit=1)
        )

        activity.with_user(self.agent).unlink()

        receipt.invalidate_recordset(
            ["state", "closed_at", "activity_id", "activity_snapshot_json"]
        )
        self.assertEqual(receipt.state, "closed")
        self.assertTrue(receipt.closed_at)
        self.assertFalse(receipt.activity_id)
        self.assertEqual(
            receipt.activity_snapshot_json["summary"],
            "Close outside Contact Center",
        )

    def test_contact_center_followup_suppresses_duplicate_activity_notification(self):
        application = self.env["contact.center.application"]
        messages_before = self.env["mail.message"].search_count([])
        emails_before = self.env["mail.mail"].search_count([])
        with mock.patch.object(
            type(application), "_notify_ui", autospec=True
        ) as notify_ui:
            result = self._api().schedule_followup(
                self.channel.id,
                {
                    "client_request_id": str(uuid.uuid4()),
                    "date_deadline": fields.Date.to_string(self._today()),
                    "summary": "One invalidation only",
                    "user_id": self.supervisor.id,
                },
            )

        productivity_calls = [
            call
            for call in notify_ui.call_args_list
            if call.args[2] == "productivity_updated"
        ]
        self.assertEqual(len(productivity_calls), 1)
        self.assertEqual(self.env["mail.message"].search_count([]), messages_before)
        self.assertEqual(self.env["mail.mail"].search_count([]), emails_before)
        self.assertEqual(
            productivity_calls[0].args[3],
            {"activity_id": result["activity"]["id"]},
        )

    def test_followup_keeps_assignee_until_last_overlapping_grant_is_removed(self):
        second_team = self.env["contact.center.team"].create(
            {
                "name": "Follow-up second team %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "agent_ids": [(6, 0, self.agent.ids)],
                "supervisor_ids": [(6, 0, self.supervisor.ids)],
            }
        )
        self.account.write(
            {
                "access_user_ids": [(6, 0, self.agent.ids)],
                "access_team_ids": [(4, second_team.id)],
            }
        )
        scheduled = self._api().schedule_followup(
            self.channel.id,
            {
                "client_request_id": str(uuid.uuid4()),
                "date_deadline": fields.Date.to_string(self._today()),
                "summary": "Overlapping access follow-up",
                "user_id": self.agent.id,
            },
        )
        activity = self.env["mail.activity"].browse(scheduled["activity"]["id"])
        self.account.write({"access_team_ids": [(3, self.team.id)]})
        second_team.write({"agent_ids": [(3, self.agent.id)]})
        self.assertEqual(activity.user_id, self.agent)
        self.account.write({"access_user_ids": [(3, self.agent.id)]})
        self.assertEqual(activity.user_id, self.supervisor)
        self.assertEqual(activity.res_model, "mail.channel")
        self.assertEqual(activity.res_id, self.channel.id)

    def test_schedule_followup_locks_access_topology_before_channel_fence(self):
        events = []
        activity_type = type(self.env["mail.activity"])
        api_type = type(self._api())
        original_scope = activity_type._contact_center_lock_activity_scope
        original_fence = api_type._productivity_fence_channel

        def record_scope(record, *args, **kwargs):
            events.append("topology")
            return original_scope(record, *args, **kwargs)

        def record_fence(record, *args, **kwargs):
            events.append("channel")
            return original_fence(record, *args, **kwargs)

        with mock.patch.object(
            activity_type,
            "_contact_center_lock_activity_scope",
            autospec=True,
            side_effect=record_scope,
        ), mock.patch.object(
            api_type,
            "_productivity_fence_channel",
            autospec=True,
            side_effect=record_fence,
        ):
            self._api().schedule_followup(
                self.channel.id,
                {
                    "client_request_id": str(uuid.uuid4()),
                    "date_deadline": fields.Date.to_string(self._today()),
                    "summary": "Canonical lock order",
                },
            )

        self.assertIn("topology", events)
        self.assertIn("channel", events)
        self.assertLess(events.index("topology"), events.index("channel"))

    def test_complete_followup_locks_topology_channel_activity_receipt_in_order(self):
        scheduled = self._api().schedule_followup(
            self.channel.id,
            {
                "client_request_id": str(uuid.uuid4()),
                "date_deadline": fields.Date.to_string(self._today()),
                "summary": "Completion lock order",
            },
        )
        events = []
        activity_type = type(self.env["mail.activity"])
        api_type = type(self._api())
        original_scope = activity_type._contact_center_lock_activity_scope
        original_fence = api_type._productivity_fence_channel
        original_activity_receipt = (
            activity_type._contact_center_lock_activities_and_receipts
        )

        def record_scope(record, *args, **kwargs):
            events.append("topology")
            return original_scope(record, *args, **kwargs)

        def record_fence(record, *args, **kwargs):
            events.append("channel")
            return original_fence(record, *args, **kwargs)

        def record_activity_receipt(record, *args, **kwargs):
            events.append("activity_receipt")
            return original_activity_receipt(record, *args, **kwargs)

        with mock.patch.object(
            activity_type,
            "_contact_center_lock_activity_scope",
            autospec=True,
            side_effect=record_scope,
        ), mock.patch.object(
            api_type,
            "_productivity_fence_channel",
            autospec=True,
            side_effect=record_fence,
        ), mock.patch.object(
            activity_type,
            "_contact_center_lock_activities_and_receipts",
            autospec=True,
            side_effect=record_activity_receipt,
        ):
            self._api().complete_followup(
                self.channel.id,
                scheduled["activity"]["id"],
                "Done once",
                str(uuid.uuid4()),
            )

        self.assertLess(events.index("topology"), events.index("channel"))
        self.assertLess(events.index("channel"), events.index("activity_receipt"))

    def test_followup_replay_ignores_later_deadline_and_roster_changes(self):
        request_id = str(uuid.uuid4())
        values = {
            "client_request_id": request_id,
            "date_deadline": fields.Date.to_string(self._today()),
            "summary": "Replay after policy changes",
            "user_id": self.supervisor.id,
        }
        first = self._api().schedule_followup(self.channel.id, values)

        self.team.write({"supervisor_ids": [(5, 0, 0)]})
        tomorrow = self._today() + datetime.timedelta(days=1)
        with mock.patch.object(fields.Date, "context_today", return_value=tomorrow):
            replay = self._api().schedule_followup(self.channel.id, values)

        self.assertEqual(replay["activity"]["id"], first["activity"]["id"])
        self.assertEqual(replay["client_request_id"], request_id)
        self.assertEqual(replay["activity"]["assigned_to"]["id"], self.agent.id)

    def test_native_completion_uses_internal_note_ledger_without_unread_or_outbox(self):
        activity = self.channel.with_user(self.agent).activity_schedule(
            activity_type_id=self.env.ref("mail.mail_activity_data_todo").id,
            date_deadline=self._today(),
            summary="Native completion",
            user_id=self.agent.id,
        )
        member = self.channel.channel_member_ids.filtered(
            lambda item: item.partner_id == self.supervisor.partner_id
        )
        before_unread = member.message_unread_counter
        before_outbox = self.env["contact.center.outbox.command"].search_count([])
        before_last = self.channel.contact_center_last_message_id
        message_id = activity.with_user(self.agent).action_feedback(
            "Confirmed internally"
        )
        message = self.env["mail.message"].browse(message_id)
        ledger = (
            self.env["contact.center.internal.note.request"]
            .sudo()
            .search(
                [
                    ("channel_id", "=", self.channel.id),
                    ("message_id", "=", message.id),
                ]
            )
        )
        self.assertEqual(len(ledger), 1)
        self.assertEqual(message.subtype_id, self.env.ref("mail.mt_note"))
        self.assertEqual(message.message_type, "comment")
        self.assertIn("Confirmed internally", html2plaintext(message.body))
        member.invalidate_recordset(["message_unread_counter"])
        self.assertEqual(member.message_unread_counter, before_unread)
        self.assertEqual(self.channel.contact_center_last_message_id, before_last)
        self.assertEqual(
            self.env["contact.center.outbox.command"].search_count([]), before_outbox
        )
        self.assertFalse(activity.exists())

    def test_followup_subscription_never_adds_members_or_external_followers(self):
        members = self.channel.channel_member_ids
        values = {
            "res_model_id": self.env["ir.model"]._get_id("mail.channel"),
            "res_id": self.channel.id,
            "activity_type_id": self.env.ref("mail.mail_activity_data_todo").id,
            "summary": "Same authorized assignee",
            "user_id": self.agent.id,
            "date_deadline": self._today(),
        }
        activities = (
            self.env["mail.activity"]
            .with_user(self.agent)
            .create([values, dict(values)])
        )
        self.assertEqual(len(activities), 2)
        self.assertEqual(self.channel.channel_member_ids, members)
        self.assertFalse(self.channel.message_follower_ids)
        with self.assertRaises(UserError):
            self.channel.with_user(self.agent).message_subscribe(
                partner_ids=self.agent.partner_id.ids
            )
        with self.assertRaises(ValidationError):
            activities.with_user(self.agent).write({"user_id": self.outsider.id})
        self.assertEqual(self.channel.channel_member_ids, members)

    def test_completion_rejects_attachments_without_discarding_them(self):
        activity = self.channel.with_user(self.agent).activity_schedule(
            activity_type_id=self.env.ref("mail.mail_activity_data_todo").id,
            date_deadline=self._today(),
            summary="Keep attachment",
            user_id=self.agent.id,
        )
        attachment = self.env["ir.attachment"].create(
            {
                "name": "Follow-up draft.txt",
                "raw": b"Private follow-up file",
                "res_model": "mail.activity",
                "res_id": activity.id,
            }
        )
        with self.assertRaisesRegex(ValidationError, "without attachments"):
            activity.with_user(self.agent).action_feedback("No discard")
        self.assertTrue(activity.exists())
        self.assertEqual(attachment.res_model, "mail.activity")
        self.assertEqual(attachment.res_id, activity.id)

    def test_followup_filters_all_due_and_future_are_conversation_scoped(self):
        other, _binding, _identity = self._create_conversation("direct")
        today = self._today()
        for channel, deadline in (
            (self.channel, today),
            (other, today + datetime.timedelta(days=1)),
        ):
            self._api().schedule_followup(
                channel.id,
                {
                    "client_request_id": str(uuid.uuid4()),
                    "date_deadline": fields.Date.to_string(deadline),
                    "summary": "Conversation reminder",
                },
            )
        all_items = self._api().list_conversations(filters={"activity_timing": "all"})[
            "items"
        ]
        due_items = self._api().list_conversations(filters={"activity_timing": "due"})[
            "items"
        ]
        planned = self._api().list_conversations(
            filters={"activity_timing": "planned"}
        )["items"]
        self.assertEqual(
            {row["channel_id"] for row in all_items}, {self.channel.id, other.id}
        )
        self.assertEqual({row["channel_id"] for row in due_items}, {self.channel.id})
        self.assertEqual({row["channel_id"] for row in planned}, {other.id})
        with self.assertRaises(AccessError):
            self._api(self.outsider).schedule_followup(
                self.channel.id,
                {
                    "client_request_id": str(uuid.uuid4()),
                    "date_deadline": fields.Date.to_string(today),
                    "summary": "Unauthorized",
                },
            )


@tagged("-at_install", "post_install")
class TestFollowupCompletionConcurrency(TransactionCase):
    WORKER_TIMEOUT_SECONDS = 12

    def _setup_committed_fixture(self, token):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            company = env.company
            agent_group = env.ref("contact_center_base.group_contact_center_agent")
            agent = (
                env["res.users"]
                .with_context(no_reset_password=True)
                .create(
                    {
                        "name": "Productivity concurrency %s" % token,
                        "login": "cc-productivity-concurrency-%s" % token,
                        "email": "cc-productivity-concurrency-%s@example.invalid"
                        % token,
                        "company_id": company.id,
                        "company_ids": [(6, 0, company.ids)],
                        "groups_id": [(6, 0, agent_group.ids)],
                    }
                )
            )
            team = env["contact.center.team"].create(
                {
                    "name": "Productivity concurrency %s" % token,
                    "company_id": company.id,
                    "agent_ids": [(6, 0, agent.ids)],
                }
            )
            account = env["contact.center.account"].create(
                {
                    "name": "Productivity concurrency %s" % token,
                    "company_id": company.id,
                    "platform": "whatsapp",
                    "external_ref": "productivity-concurrency-%s" % token,
                    "access_team_ids": [(6, 0, [team.id])],
                }
            )
            connection = env["contact.center.provider.connection"].create(
                {
                    "name": "Productivity concurrency %s" % token,
                    "account_id": account.id,
                    "adapter_key": "test.fake",
                    "external_ref": "productivity-concurrency-provider-%s" % token,
                    "provider_schema_version": "fixture-v1",
                    "state": "connected",
                    "active": True,
                    "role": "primary",
                    "inbound_active": True,
                    "outbound_active": True,
                    "capabilities_json": {"send_message": True},
                    "last_state_observed_at": fields.Datetime.now(),
                    "last_health_at": fields.Datetime.now(),
                }
            )
            guest = (
                env["mail.guest"]
                .sudo()
                .create({"name": "Productivity concurrency guest %s" % token})
            )
            identity = (
                env["contact.center.identity"]
                .sudo()
                .create(
                    {
                        "name": guest.name,
                        "company_id": company.id,
                        "mail_guest_id": guest.id,
                    }
                )
            )
            channel = env["mail.channel"]._contact_center_create_channel(
                account=account,
                identity=identity,
                conversation_type="direct",
                name="Productivity concurrency %s" % token,
                teams=team,
                guest_ids=guest.ids,
            )
            binding = (
                env["contact.center.channel.binding"]
                .sudo()
                .create(
                    {
                        "channel_id": channel.id,
                        "account_id": account.id,
                        "identity_id": identity.id,
                        "conversation_type": "direct",
                        "conversation_ref": "productivity-concurrency-%s" % token,
                    }
                )
            )
            env["contact.center.channel.alias"].sudo().create(
                {
                    "channel_binding_id": binding.id,
                    "account_id": account.id,
                    "namespace": "whatsapp.pn",
                    "value_raw": "55%s@s.whatsapp.net" % token,
                    "value_normalized": "55%s@s.whatsapp.net" % token,
                    "role": "primary",
                }
            )
            schedule_request_id = str(uuid.uuid4())
            scheduled = (
                env["contact.center.ui.api"]
                .with_user(agent)
                .schedule_followup(
                    channel.id,
                    {
                        "client_request_id": schedule_request_id,
                        "date_deadline": fields.Date.to_string(
                            fields.Date.context_today(agent)
                        ),
                        "summary": "Concurrency completion",
                        "user_id": agent.id,
                    },
                )
            )
            conversation_id = channel.id
            chatter_before = (
                env["mail.message"]
                .sudo()
                .search_count(
                    [("model", "=", "mail.channel"), ("res_id", "=", conversation_id)]
                )
            )
            cr.commit()  # pylint: disable=invalid-commit
            return {
                "account_id": account.id,
                "activity_id": scheduled["activity"]["id"],
                "agent_id": agent.id,
                "binding_id": binding.id,
                "conversation_id": conversation_id,
                "channel_id": channel.id,
                "chatter_before": chatter_before,
                "connection_id": connection.id,
                "guest_id": guest.id,
                "identity_id": identity.id,
                "team_id": team.id,
            }

    def _complete_transaction(self, fixture, completion_request_id):
        with self.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = '5s'")
            cr.execute("SET LOCAL statement_timeout = '10s'")
            env = api.Environment(cr, SUPERUSER_ID, {})
            try:
                result = (
                    env["contact.center.ui.api"]
                    .with_user(fixture["agent_id"])
                    .complete_followup(
                        fixture["channel_id"],
                        fixture["activity_id"],
                        "Completed once",
                        completion_request_id,
                    )
                )
                cr.commit()  # pylint: disable=invalid-commit
                return {"outcome": "done", "result": result}
            except SerializationFailure:
                cr.rollback()
                return {"outcome": "serialization", "result": False}

    def _complete_worker(
        self, fixture, completion_request_id, barrier, results, worker_name
    ):
        with self.registry.cursor() as cr:
            # Pin both workers to the same REPEATABLE READ snapshot before the
            # channel/activity cutover locks are acquired.
            cr.execute(
                "SELECT id FROM mail_channel WHERE id = %s",
                [fixture["channel_id"]],
            )
            barrier.wait(timeout=self.WORKER_TIMEOUT_SECONDS)
            env = api.Environment(cr, SUPERUSER_ID, {})
            try:
                result = (
                    env["contact.center.ui.api"]
                    .with_user(fixture["agent_id"])
                    .complete_followup(
                        fixture["channel_id"],
                        fixture["activity_id"],
                        "Completed once",
                        completion_request_id,
                    )
                )
                cr.commit()  # pylint: disable=invalid-commit
                results[worker_name] = {"outcome": "done", "result": result}
            except SerializationFailure:
                cr.rollback()
                results[worker_name] = self._complete_transaction(
                    fixture, completion_request_id
                )

    def _cleanup_committed_fixture(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            channel = (
                env["mail.channel"]
                .sudo()
                .with_context(active_test=False)
                .browse(fixture["channel_id"])
                .exists()
            )
            receipts = (
                env["contact.center.followup.request"]
                .sudo()
                .search([("channel_id", "=", fixture["channel_id"])])
            )
            receipts.with_context(
                contact_center_productivity_service_token=(_PRODUCTIVITY_SERVICE_TOKEN)
            ).unlink()
            outboxes = (
                env["contact.center.outbox.command"]
                .sudo()
                .search([("channel_binding_id", "=", fixture["binding_id"])])
            )
            if outboxes:
                env["queue.job"].sudo().search(
                    [
                        (
                            "identity_key",
                            "in",
                            [
                                "contact_center:outbox:%s" % outbox.id
                                for outbox in outboxes
                            ],
                        )
                    ]
                ).unlink()
            env["mail.activity"].sudo().search(
                [
                    ("res_model", "=", "mail.channel"),
                    ("res_id", "=", fixture["channel_id"]),
                ]
            ).unlink()
            env["contact.center.internal.note.request"].sudo().search(
                [("channel_id", "=", fixture["channel_id"])]
            ).with_context(
                contact_center_productivity_service_token=_PRODUCTIVITY_SERVICE_TOKEN
            ).unlink()
            env["mail.message"].sudo().search(
                [("model", "=", "mail.channel"), ("res_id", "=", fixture["channel_id"])]
            ).with_context(contact_center_post_token=CONTACT_CENTER_POST_TOKEN).unlink()
            env["contact.center.channel.binding"].sudo().with_context(
                active_test=False
            ).browse(fixture["binding_id"]).unlink()
            channel.with_context(
                contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN,
                contact_center_post_token=CONTACT_CENTER_POST_TOKEN,
            ).unlink()
            env["contact.center.identity"].sudo().browse(
                fixture["identity_id"]
            ).unlink()
            env["mail.guest"].sudo().browse(fixture["guest_id"]).with_context(
                contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
            ).unlink()
            env["contact.center.provider.connection"].sudo().with_context(
                active_test=False
            ).browse(fixture["connection_id"]).unlink()
            env["contact.center.account"].sudo().with_context(active_test=False).browse(
                fixture["account_id"]
            ).unlink()
            env["contact.center.team"].sudo().with_context(active_test=False).browse(
                fixture["team_id"]
            ).unlink()
            user = (
                env["res.users"]
                .sudo()
                .with_context(active_test=False)
                .browse(fixture["agent_id"])
            )
            partner = user.partner_id
            user.unlink()
            partner.exists().unlink()
            cr.commit()  # pylint: disable=invalid-commit

    def test_concurrent_completion_converges_to_one_feedback_message(self):
        fixture = self._setup_committed_fixture(uuid.uuid4().hex)
        completion_request_id = str(uuid.uuid4())
        barrier = threading.Barrier(2)
        results = {}
        workers = [
            threading.Thread(
                target=self._complete_worker,
                args=(
                    fixture,
                    completion_request_id,
                    barrier,
                    results,
                    "worker-%s" % index,
                ),
            )
            for index in range(2)
        ]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(self.WORKER_TIMEOUT_SECONDS)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(len(results), 2)
            self.assertEqual(
                {result["outcome"] for result in results.values()}, {"done"}
            )
            returned = [result["result"] for result in results.values()]
            self.assertEqual(returned[0], returned[1])
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                self.assertFalse(
                    env["mail.activity"].browse(fixture["activity_id"]).exists()
                )
                receipt = (
                    env["contact.center.followup.request"]
                    .sudo()
                    .search([("activity_record_id", "=", fixture["activity_id"])])
                )
                self.assertEqual(len(receipt), 1)
                self.assertEqual(receipt.state, "completed")
                self.assertEqual(receipt.completion_request_id, completion_request_id)
                self.assertEqual(
                    env["mail.message"]
                    .sudo()
                    .search_count(
                        [
                            ("model", "=", "mail.channel"),
                            ("res_id", "=", fixture["conversation_id"]),
                        ]
                    ),
                    fixture["chatter_before"] + 1,
                )
        finally:
            self._cleanup_committed_fixture(fixture)
