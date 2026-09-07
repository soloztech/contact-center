import datetime
import hashlib
import threading
import uuid
from unittest import mock

import pytz
from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import SavepointCase, TransactionCase
from odoo.tools import html2plaintext

from odoo.addons.contact_center_base.models.productivity import (
    _PRODUCTIVITY_SERVICE_TOKEN,
)
from odoo.addons.contact_center_base.services.tokens import (
    CONTACT_CENTER_MEMBERSHIP_TOKEN,
    CONTACT_CENTER_POST_TOKEN,
)

# Register the fixture adapter even when only Kanban is upgraded/tested.
from odoo.addons.contact_center_base.tests.test_contact_center import (  # noqa: F401
    FakeAdapter,
)
from odoo.addons.queue_job.tests.common import trap_jobs


class TestContactCenterProductivity(SavepointCase):
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
                "default_team_id": cls.team.id,
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
            team=cls.team,
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

    def _future_local_string(self, minutes=5):
        utc_value = fields.Datetime.to_datetime(
            fields.Datetime.now()
        ) + datetime.timedelta(minutes=minutes)
        localized = pytz.UTC.localize(utc_value).astimezone(
            pytz.timezone(self.agent.tz)
        )
        return localized.replace(tzinfo=None).isoformat(timespec="seconds")

    def _today(self, user=None):
        return fields.Date.context_today(self.channel.with_user(user or self.agent))

    def test_quick_replies_reuse_native_shortcode(self):
        marker = uuid.uuid4().hex[:10]
        reply = self.env["mail.shortcode"].create(
            {
                "source": "hello-%s" % marker,
                "substitution": "Hello from the Contact Center",
                "description": "Productivity fixture %s" % marker,
            }
        )
        binding = self.env["contact.center.quick.reply.binding"].create(
            {
                "shortcode_id": reply.id,
                "company_id": self.env.company.id,
                "scope": "company",
            }
        )

        result = self._api().search_quick_replies(self.channel.id, marker, limit=5)

        self.assertEqual(
            binding.scope_key,
            "%s:company:%s:%s" % (self.env.company.id, self.env.company.id, reply.id),
        )
        self.assertEqual(
            result["items"],
            [
                {
                    "id": reply.id,
                    "binding_id": binding.id,
                    "scope": "company",
                    "shortcut": reply.source,
                    "body": reply.substitution,
                    "description": reply.description,
                }
            ],
        )

    def test_quick_replies_require_an_authorized_channel_and_scoped_binding(self):
        marker = uuid.uuid4().hex[:10]
        company_reply = self.env["mail.shortcode"].create(
            {
                "source": "company-%s" % marker,
                "substitution": "Company scope",
                "description": marker,
            }
        )
        account_reply = self.env["mail.shortcode"].create(
            {
                "source": "account-%s" % marker,
                "substitution": "Other inbox only",
                "description": marker,
            }
        )
        other_account = self.env["contact.center.account"].create(
            {
                "name": "Other quick reply inbox",
                "company_id": self.env.company.id,
                "platform": "whatsapp",
                "external_ref": "quick-reply-other-%s" % uuid.uuid4(),
                "default_team_id": self.team.id,
            }
        )
        company_binding = self.env["contact.center.quick.reply.binding"].create(
            {
                "shortcode_id": company_reply.id,
                "company_id": self.env.company.id,
                "scope": "company",
            }
        )
        self.env["contact.center.quick.reply.binding"].create(
            {
                "shortcode_id": account_reply.id,
                "company_id": self.env.company.id,
                "scope": "account",
                "account_id": other_account.id,
            }
        )

        result = self._api().search_quick_replies(self.channel.id, marker, limit=10)

        self.assertEqual(
            [item["binding_id"] for item in result["items"]], [company_binding.id]
        )
        with self.assertRaises(AccessError):
            self.env["contact.center.quick.reply.binding"].with_user(self.agent).create(
                {
                    "shortcode_id": company_reply.id,
                    "company_id": self.env.company.id,
                    "scope": "account",
                    "account_id": self.account.id,
                }
            )
        with self.assertRaises(AccessError):
            self.env["contact.center.quick.reply.binding"].with_user(
                self.outsider
            ).search([("id", "=", company_binding.id)])
        with self.assertRaises(AccessError):
            self._api(self.outsider).search_quick_replies(
                self.channel.id, marker, limit=10
            )

    def test_quick_reply_most_specific_binding_wins(self):
        marker = uuid.uuid4().hex[:10]
        reply = self.env["mail.shortcode"].create(
            {
                "source": "specific-%s" % marker,
                "substitution": "Specific reply",
                "description": marker,
            }
        )
        self.env["contact.center.quick.reply.binding"].create(
            {
                "shortcode_id": reply.id,
                "company_id": self.env.company.id,
                "scope": "company",
                "sequence": 1,
            }
        )
        channel_binding = self.env["contact.center.quick.reply.binding"].create(
            {
                "shortcode_id": reply.id,
                "company_id": self.env.company.id,
                "scope": "channel",
                "channel_id": self.channel.id,
                "sequence": 99,
            }
        )

        result = self._api().search_quick_replies(self.channel.id, marker, limit=10)

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["binding_id"], channel_binding.id)
        self.assertEqual(result["items"][0]["scope"], "channel")

    def test_internal_note_is_escaped_idempotent_and_has_no_outbox(self):
        request_id = str(uuid.uuid4())
        body = (
            "First  line\n\n"
            "URL: https://example.com/path?q=one&next=two\n"
            "<script>alert('private')</script>\n"
            "Last line"
        )
        baseline_message = self.channel.sudo()._contact_center_post(
            origin="inbound",
            body="Operational message before the note",
            author_guest_id=self.identity.mail_guest_id.id,
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
        )
        self.env["contact.center.application"]._publish_message_created(
            self.channel, baseline_message, direction="inbound"
        )
        self._api().mark_seen(self.channel.id, baseline_message.id)
        self.channel.invalidate_recordset(
            ["contact_center_last_message_id", "contact_center_last_message_at"]
        )
        pointer_before = (
            self.channel.contact_center_last_message_id.id,
            self.channel.contact_center_last_message_at,
        )
        members = self.channel.sudo().channel_member_ids.filtered("partner_id")
        members.invalidate_recordset(["message_unread_counter"])
        unread_before = {member.id: member.message_unread_counter for member in members}
        outbox_count = self.env["contact.center.outbox.command"].sudo().search_count([])

        first = self._api().post_internal_note(self.channel.id, body, request_id)
        replay = self._api().post_internal_note(self.channel.id, body, request_id)

        self.assertEqual(
            first["message"]["message_id"], replay["message"]["message_id"]
        )
        message = self.env["mail.message"].browse(first["message"]["message_id"])
        self.channel.invalidate_recordset(
            ["contact_center_last_message_id", "contact_center_last_message_at"]
        )
        self.assertEqual(
            (
                self.channel.contact_center_last_message_id.id,
                self.channel.contact_center_last_message_at,
            ),
            pointer_before,
        )
        members.invalidate_recordset(["message_unread_counter"])
        self.assertEqual(
            {member.id: member.message_unread_counter for member in members},
            unread_before,
        )
        timeline = self._api().get_timeline(self.channel.id)
        timeline_note = next(
            item for item in timeline["items"] if item["message_id"] == message.id
        )
        self.assertEqual(timeline_note["direction"], "internal")
        self.assertEqual(timeline_note["content_type"], "note")
        self.assertEqual(message.subtype_id, self.env.ref("mail.mt_note"))
        plain_body = html2plaintext(message.body).strip()
        self.assertIn("First", plain_body)
        self.assertIn("example.com", plain_body)
        self.assertNotIn("<script>", str(message.body))
        self.assertFalse(
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", message.id)])
        )
        self.assertEqual(
            self.env["contact.center.outbox.command"].sudo().search_count([]),
            outbox_count,
        )
        ledger = (
            self.env["contact.center.internal.note.request"]
            .sudo()
            .search(
                [
                    ("channel_id", "=", self.channel.id),
                    ("ui_request_id", "=", request_id),
                ],
                limit=1,
            )
        )
        self.assertTrue(ledger)
        self.assertEqual(
            ledger.body_sha256,
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            ledger.message_body_sha256,
            hashlib.sha256(str(message.body or "").encode("utf-8")).hexdigest(),
        )
        with self.assertRaises(AccessError):
            message.sudo().write({"body": "Tampered through the ORM"})
        with self.assertRaises(AccessError):
            message.sudo().unlink()

        unrelated = (
            self.env["mail.message"]
            .sudo()
            .create(
                {
                    "body": "Unrelated native note",
                    "message_type": "comment",
                    "model": "contact.center.case",
                    "res_id": self.channel.contact_center_case_ids[:1].id,
                    "subtype_id": self.env.ref("mail.mt_note").id,
                }
            )
        )
        unrelated.write({"body": "Still editable"})
        unrelated.unlink()

        with self.assertRaises(ValidationError):
            self._api().post_internal_note(
                self.channel.id, "Different note", request_id
            )

        # The immutable ORM contract and the restrictive FK protect normal
        # callers.  Replay validation also detects out-of-band SQL damage.
        self.env.cr.execute(
            "UPDATE mail_message SET body = %s WHERE id = %s",
            ["Tampered out of band", message.id],
        )
        message.invalidate_recordset(["body"])
        with self.assertRaises(ValidationError):
            self._api().post_internal_note(self.channel.id, body, request_id)

    def test_internal_note_never_becomes_inbox_last_message_projection(self):
        channel, _binding, _identity = self._create_conversation("direct")
        self.assertFalse(channel.contact_center_last_message_id)

        note = self._api().post_internal_note(
            channel.id,
            "Internal context before the customer writes",
            str(uuid.uuid4()),
        )

        conversation = self._api().get_conversation(channel.id)["item"]
        timeline = self._api().get_timeline(channel.id)["items"]
        self.assertFalse(conversation["last_message"])
        self.assertIn(
            note["message"]["message_id"], {item["message_id"] for item in timeline}
        )

    def test_followups_support_default_and_secondary_cases_and_completion(self):
        default_case = self.channel.contact_center_case_ids.filtered("is_default")
        pipeline = default_case.pipeline_id
        secondary = (
            self.env["contact.center.case"]
            .with_user(self.agent)
            .create(
                {
                    "name": "Second commercial topic",
                    "channel_id": self.channel.id,
                    "pipeline_id": pipeline.id,
                    "stage_id": pipeline._contact_center_initial_stage().id,
                }
            )
        )
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
                "case_id": secondary.id,
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

        self.assertEqual(first["activity"]["case_id"], default_case.id)
        self.assertEqual(second["activity"]["case_id"], secondary.id)
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
            {item["case_id"] for item in productivity["activities"]},
            {default_case.id, secondary.id},
        )
        chatter_before = (
            self.env["mail.message"]
            .sudo()
            .search_count(
                [
                    ("model", "=", "contact.center.case"),
                    ("res_id", "=", default_case.id),
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
                    ("model", "=", "contact.center.case"),
                    ("res_id", "=", default_case.id),
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
        case = self.channel.contact_center_case_ids.filtered("is_default")[:1]
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
        portal_user = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Productivity Portal Follower",
                    "login": "cc-productivity-portal-%s" % uuid.uuid4(),
                    "email": "cc-productivity-portal@example.invalid",
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [(6, 0, self.env.ref("base.group_portal").ids)],
                }
            )
        )
        case.message_subscribe(partner_ids=self.supervisor.partner_id.ids)
        case.message_subscribe(partner_ids=portal_user.partner_id.ids)
        self.assertIn(self.supervisor.partner_id, case.message_partner_ids)
        self.assertIn(portal_user.partner_id, case.message_partner_ids)

        self.team.write({"supervisor_ids": [(5, 0, 0)]})

        activity = self.env["mail.activity"].browse(result["activity"]["id"])
        activity.invalidate_recordset(["user_id"])
        case.invalidate_recordset(["message_partner_ids"])
        self.assertEqual(activity.user_id, self.agent)
        self.assertNotIn(self.supervisor.partner_id, case.message_partner_ids)
        self.assertIn(portal_user.partner_id, case.message_partner_ids)
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
        case = self.channel.contact_center_case_ids.filtered("is_default")[:1]
        activity = case.activity_schedule(
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
            .search_count(
                [("model", "=", "contact.center.case"), ("res_id", "=", case.id)]
            )
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
                [("model", "=", "contact.center.case"), ("res_id", "=", case.id)]
            ),
            chatter_before + 1,
        )

    def test_native_activity_changes_publish_one_productivity_invalidation(self):
        case = self.channel.contact_center_case_ids.filtered("is_default")[:1]
        application = self.env["contact.center.application"]
        activity_type = self.env.ref("mail.mail_activity_data_todo")
        values = {
            "activity_type_id": activity_type.id,
            "date_deadline": self._today(),
            "res_id": case.id,
            "res_model_id": self.env["ir.model"]._get_id("contact.center.case"),
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
        case = self.channel.contact_center_case_ids.filtered("is_default")[:1]
        values = {
            "activity_type_id": self.env.ref("mail.mail_activity_data_todo").id,
            "date_deadline": self._today(),
            "res_id": case.id,
            "res_model_id": self.env["ir.model"]._get_id("contact.center.case"),
            "summary": "Must remain in the inbox",
            "user_id": self.outsider.id,
        }

        with self.assertRaises(ValidationError):
            self.env["mail.activity"].with_user(self.agent).create(values)

        values["user_id"] = self.agent.id
        activity = self.env["mail.activity"].with_user(self.agent).create(values)
        with self.assertRaises(ValidationError):
            activity.with_user(self.agent).write({"user_id": False})

    def test_receipted_activity_cannot_be_retargeted_and_snapshot_stays_current(self):
        default_case = self.channel.contact_center_case_ids.filtered("is_default")[:1]
        pipeline = default_case.pipeline_id
        secondary = (
            self.env["contact.center.case"]
            .with_user(self.agent)
            .create(
                {
                    "name": "Retarget destination",
                    "channel_id": self.channel.id,
                    "pipeline_id": pipeline.id,
                    "stage_id": pipeline._contact_center_initial_stage().id,
                }
            )
        )
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
        self.assertEqual(activity.res_id, default_case.id)
        self.assertEqual(receipt.case_id, default_case)

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
        with mock.patch.object(
            type(application), "_notify_ui", autospec=True
        ) as notify_ui:
            result = self._api().schedule_followup(
                self.channel.id,
                {
                    "client_request_id": str(uuid.uuid4()),
                    "date_deadline": fields.Date.to_string(self._today()),
                    "summary": "One invalidation only",
                    "user_id": self.agent.id,
                },
            )

        productivity_calls = [
            call
            for call in notify_ui.call_args_list
            if call.args[2] == "productivity_updated"
        ]
        self.assertEqual(len(productivity_calls), 1)
        self.assertEqual(
            productivity_calls[0].args[3],
            {"activity_id": result["activity"]["id"]},
        )

    def test_schedule_followup_locks_case_topology_before_channel_fence(self):
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

    def test_productivity_never_truncates_actionable_scheduled_messages(self):
        now = fields.Datetime.to_datetime(fields.Datetime.now())
        values = []
        active_count = 55
        history_count = 60
        for index in range(active_count + history_count):
            body = "Scheduled visibility %s" % index
            values.append(
                {
                    "body": body,
                    "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                    "channel_id": self.channel.id,
                    "requested_by_id": self.agent.id,
                    "scheduled_at": now + datetime.timedelta(minutes=index + 1),
                    "state": "scheduled" if index < active_count else "released",
                    "ui_request_id": str(uuid.uuid4()),
                }
            )
        self.env["contact.center.scheduled.message"].with_context(
            contact_center_productivity_service_token=_PRODUCTIVITY_SERVICE_TOKEN
        ).create(values)

        result = self._api().get_productivity(self.channel.id)["scheduled_messages"]

        self.assertEqual(
            len([item for item in result if item["state"] == "scheduled"]),
            active_count,
        )
        self.assertEqual(
            len([item for item in result if item["state"] == "released"]),
            50,
        )

    def test_scheduled_message_is_idempotent_cancelled_and_job_is_noop(self):
        request_id = str(uuid.uuid4())
        scheduled_at = self._future_local_string()
        with trap_jobs() as trap:
            first = self._api().schedule_message(
                self.channel.id, "Scheduled hello", scheduled_at, request_id
            )
            replay = self._api().schedule_message(
                self.channel.id, "Scheduled hello", scheduled_at, request_id
            )

        self.assertEqual(
            first["scheduled_message"]["id"], replay["scheduled_message"]["id"]
        )
        self.assertEqual(len(trap.enqueued_jobs), 1)
        scheduled = self.env["contact.center.scheduled.message"].browse(
            first["scheduled_message"]["id"]
        )
        cancelled = self._api().cancel_scheduled_message(self.channel.id, scheduled.id)
        self.assertEqual(cancelled["scheduled_message"]["state"], "cancelled")
        self.assertFalse(
            scheduled.with_context(job_uuid=scheduled.queue_job_uuid)._job_release()
        )
        self.assertFalse(scheduled.message_id)
        with self.assertRaises(ValidationError):
            self._api().schedule_message(
                self.channel.id, "Changed body", scheduled_at, request_id
            )

    def test_scheduled_cancellation_rechecks_membership_after_row_lock(self):
        with trap_jobs():
            result = self._api().schedule_message(
                self.channel.id,
                "Do not cancel after access revocation",
                self._future_local_string(),
                str(uuid.uuid4()),
            )
        scheduled = self.env["contact.center.scheduled.message"].browse(
            result["scheduled_message"]["id"]
        )
        scheduled_type = type(scheduled)
        original_lock = scheduled_type._lock_for_transition

        def revoke_then_lock(record):
            self.team.write({"agent_ids": [(5, 0, 0)]})
            return original_lock(record)

        with mock.patch.object(
            scheduled_type,
            "_lock_for_transition",
            autospec=True,
            side_effect=revoke_then_lock,
        ), self.assertRaises(AccessError):
            self._api().cancel_scheduled_message(self.channel.id, scheduled.id)

        scheduled.invalidate_recordset(["state"])
        self.assertEqual(scheduled.state, "scheduled")

    def test_scheduled_message_replay_after_delivery_time_returns_receipt(self):
        request_id = str(uuid.uuid4())
        scheduled_at = self._future_local_string()
        with trap_jobs() as first_trap:
            first = self._api().schedule_message(
                self.channel.id, "Replay after delivery time", scheduled_at, request_id
            )
            first_trap.assert_jobs_count(1)

        later = fields.Datetime.to_datetime(fields.Datetime.now()) + datetime.timedelta(
            days=1
        )
        with mock.patch.object(
            fields.Datetime, "now", return_value=later
        ), trap_jobs() as trap:
            replay = self._api().schedule_message(
                self.channel.id, "Replay after delivery time", scheduled_at, request_id
            )

        self.assertEqual(replay, first)
        trap.assert_jobs_count(0)

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

    def test_due_scheduled_message_uses_canonical_outbox_flow(self):
        request_id = str(uuid.uuid4())
        with trap_jobs():
            result = self._api().schedule_message(
                self.channel.id,
                "Release through the application service",
                self._future_local_string(),
                request_id,
            )
        scheduled = self.env["contact.center.scheduled.message"].browse(
            result["scheduled_message"]["id"]
        )
        self.env.cr.execute(
            "UPDATE contact_center_scheduled_message SET scheduled_at = %s "
            "WHERE id = %s",
            [
                fields.Datetime.now() - datetime.timedelta(seconds=1),
                scheduled.id,
            ],
        )
        scheduled.invalidate_recordset(["scheduled_at"])

        with trap_jobs():
            released = scheduled.with_context(
                job_uuid=scheduled.queue_job_uuid
            )._job_release()

        scheduled.invalidate_recordset()
        self.assertTrue(released)
        self.assertEqual(scheduled.state, "released")
        self.assertTrue(scheduled.message_id)
        self.assertTrue(scheduled.outbox_command_id)
        self.assertEqual(
            scheduled.outbox_command_id.ui_request_id,
            scheduled.outbound_request_id,
        )
        self.assertNotEqual(scheduled.outbound_request_id, request_id)
        self.assertEqual(scheduled.outbox_command_id.command_type, "send_message")
        self.assertEqual(
            scheduled.outbox_command_id.message_binding_id.message_id,
            scheduled.message_id,
        )

    def test_scheduled_release_has_an_independent_outbound_idempotency_namespace(self):
        request_id = str(uuid.uuid4())
        immediate = (
            self._api()
            .with_context(contact_center_skip_enqueue=True)
            .send_message(
                self.channel.id,
                "Same payload in another intent namespace",
                client_request_id=request_id,
                media_refs=[],
            )
        )
        with trap_jobs():
            result = self._api().schedule_message(
                self.channel.id,
                "Same payload in another intent namespace",
                self._future_local_string(),
                request_id,
            )
        scheduled = self.env["contact.center.scheduled.message"].browse(
            result["scheduled_message"]["id"]
        )
        self.env.cr.execute(
            "UPDATE contact_center_scheduled_message SET scheduled_at = %s "
            "WHERE id = %s",
            [fields.Datetime.now() - datetime.timedelta(seconds=1), scheduled.id],
        )
        scheduled.invalidate_recordset(["scheduled_at"])

        with trap_jobs():
            self.assertTrue(
                scheduled.with_context(job_uuid=scheduled.queue_job_uuid)._job_release()
            )

        scheduled.invalidate_recordset(
            ["message_id", "outbox_command_id", "outbound_request_id", "state"]
        )
        self.assertEqual(scheduled.state, "released")
        self.assertNotEqual(scheduled.outbound_request_id, request_id)
        self.assertNotEqual(scheduled.message_id.id, immediate["message_id"])
        self.assertNotEqual(
            scheduled.outbox_command_id.id, immediate["outbox_command_id"]
        )
        self.assertEqual(
            scheduled.outbox_command_id.ui_request_id,
            scheduled.outbound_request_id,
        )

    def test_scheduled_release_fails_terminally_when_body_digest_diverges(self):
        with trap_jobs():
            result = self._api().schedule_message(
                self.channel.id,
                "Immutable scheduled body",
                self._future_local_string(),
                str(uuid.uuid4()),
            )
        scheduled = self.env["contact.center.scheduled.message"].browse(
            result["scheduled_message"]["id"]
        )
        self.env.cr.execute(
            "UPDATE contact_center_scheduled_message "
            "SET body = %s, scheduled_at = %s WHERE id = %s",
            [
                "Tampered out of band",
                fields.Datetime.now() - datetime.timedelta(seconds=1),
                scheduled.id,
            ],
        )
        scheduled.invalidate_recordset(["body", "scheduled_at"])
        outbox_before = (
            self.env["contact.center.outbox.command"].sudo().search_count([])
        )

        released = scheduled.with_context(
            job_uuid=scheduled.queue_job_uuid
        )._job_release()

        scheduled.invalidate_recordset(
            ["state", "last_error_class", "message_id", "outbox_command_id"]
        )
        self.assertFalse(released)
        self.assertEqual(scheduled.state, "failed")
        self.assertEqual(scheduled.last_error_class, "ScheduledBodyIntegrityError")
        self.assertFalse(scheduled.message_id)
        self.assertFalse(scheduled.outbox_command_id)
        self.assertEqual(
            self.env["contact.center.outbox.command"].sudo().search_count([]),
            outbox_before,
        )

    def test_scheduled_release_rejects_job_when_canonical_uuid_is_missing(self):
        with trap_jobs():
            result = self._api().schedule_message(
                self.channel.id,
                "Release only from the canonical queue job",
                self._future_local_string(),
                str(uuid.uuid4()),
            )
        scheduled = self.env["contact.center.scheduled.message"].browse(
            result["scheduled_message"]["id"]
        )
        job_uuid = scheduled.queue_job_uuid
        scheduled._service_write({"queue_job_uuid": False})

        self.assertFalse(scheduled.with_context(job_uuid=job_uuid)._job_release())
        scheduled.invalidate_recordset(["state", "message_id", "outbox_command_id"])
        self.assertEqual(scheduled.state, "scheduled")
        self.assertFalse(scheduled.message_id)
        self.assertFalse(scheduled.outbox_command_id)

    def test_recovery_recreates_an_orphan_with_original_eta(self):
        future = fields.Datetime.to_datetime(
            fields.Datetime.now()
        ) + datetime.timedelta(minutes=10)
        scheduled = (
            self.env["contact.center.scheduled.message"]
            .sudo()
            .with_context(
                contact_center_productivity_service_token=_PRODUCTIVITY_SERVICE_TOKEN
            )
            .create(
                {
                    "channel_id": self.channel.id,
                    "requested_by_id": self.agent.id,
                    "ui_request_id": str(uuid.uuid4()),
                    "body": "Recover me",
                    "body_sha256": "fixture",
                    "scheduled_at": future,
                }
            )
        )

        with trap_jobs() as trap:
            recovered = self.env[
                "contact.center.scheduled.message"
            ]._cron_recover_scheduled_messages(limit=10, grace_seconds=0)

        self.assertEqual(recovered, 1)
        self.assertEqual(len(trap.enqueued_jobs), 1)
        scheduled.invalidate_recordset(["queue_job_uuid"])
        self.assertTrue(scheduled.queue_job_uuid)
        self.assertEqual(fields.Datetime.to_datetime(trap.enqueued_jobs[0].eta), future)

    def test_recovery_replaces_queue_job_without_canonical_identity(self):
        future = fields.Datetime.to_datetime(
            fields.Datetime.now()
        ) + datetime.timedelta(minutes=10)
        scheduled = (
            self.env["contact.center.scheduled.message"]
            .sudo()
            .with_context(
                contact_center_productivity_service_token=_PRODUCTIVITY_SERVICE_TOKEN
            )
            .create(
                {
                    "channel_id": self.channel.id,
                    "requested_by_id": self.agent.id,
                    "ui_request_id": str(uuid.uuid4()),
                    "body": "Replace non-canonical queue pointer",
                    "body_sha256": "fixture",
                    "scheduled_at": future,
                }
            )
        )
        scheduled._enqueue()
        stale_job = (
            self.env["queue.job"]
            .sudo()
            .search([("uuid", "=", scheduled.queue_job_uuid)], limit=1)
        )
        self.assertTrue(stale_job)
        stale_job.write({"identity_key": False})
        stale_at = fields.Datetime.now() - datetime.timedelta(minutes=10)
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE contact_center_scheduled_message SET write_date = %s WHERE id = %s",
            [stale_at, scheduled.id],
        )
        self.env.invalidate_all(flush=False)

        with trap_jobs() as trap:
            recovered = self.env[
                "contact.center.scheduled.message"
            ]._cron_recover_scheduled_messages(limit=10, grace_seconds=0)

        scheduled.invalidate_recordset(["queue_job_uuid"])
        self.assertEqual(recovered, 1)
        self.assertEqual(len(trap.enqueued_jobs), 1)
        self.assertNotEqual(scheduled.queue_job_uuid, stale_job.uuid)
        self.assertEqual(
            trap.enqueued_jobs[0].identity_key,
            "contact_center:scheduled:%s" % scheduled.id,
        )

    def test_recovery_keeps_exhausted_queue_failure_terminal(self):
        future = fields.Datetime.to_datetime(
            fields.Datetime.now()
        ) + datetime.timedelta(minutes=10)
        scheduled = (
            self.env["contact.center.scheduled.message"]
            .sudo()
            .with_context(
                contact_center_productivity_service_token=_PRODUCTIVITY_SERVICE_TOKEN
            )
            .create(
                {
                    "channel_id": self.channel.id,
                    "requested_by_id": self.agent.id,
                    "ui_request_id": str(uuid.uuid4()),
                    "body": "Do not loop",
                    "body_sha256": "fixture",
                    "scheduled_at": future,
                }
            )
        )
        scheduled._enqueue()
        failed_job = (
            self.env["queue.job"]
            .sudo()
            .search([("uuid", "=", scheduled.queue_job_uuid)], limit=1)
        )
        self.assertTrue(failed_job)
        failed_job.write({"state": "failed", "retry": 5})
        stale_at = fields.Datetime.now() - datetime.timedelta(minutes=10)
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE contact_center_scheduled_message SET write_date = %s WHERE id = %s",
            [stale_at, scheduled.id],
        )
        self.env.invalidate_all(flush=False)

        with trap_jobs() as trap:
            recovered = self.env[
                "contact.center.scheduled.message"
            ]._cron_recover_scheduled_messages(limit=10, grace_seconds=0)

        scheduled.invalidate_recordset(["state", "last_error_class", "queue_job_uuid"])
        self.assertEqual(recovered, 1)
        self.assertFalse(trap.enqueued_jobs)
        self.assertEqual(scheduled.state, "failed")
        self.assertEqual(scheduled.last_error_class, "ScheduledQueueJobFailed")
        self.assertEqual(scheduled.queue_job_uuid, failed_job.uuid)

    def test_filters_scope_type_tag_activity_and_member_unread(self):
        group_channel, _group_binding, group_identity = self._create_conversation(
            "group"
        )
        tag = self.env["contact.center.tag"].create(
            {
                "name": "Priority %s" % uuid.uuid4().hex[:8],
                "company_id": self.env.company.id,
            }
        )
        self.channel.sudo().with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).write({"contact_center_tag_ids": [(4, tag.id)]})

        direct_items = self._api().list_conversations(
            filters={"conversation_type": "direct"}
        )["items"]
        group_items = self._api().list_conversations(
            filters={"conversation_type": "group"}
        )["items"]
        tagged_items = self._api().list_conversations(filters={"tag_id": tag.id})[
            "items"
        ]
        self.assertIn(self.channel.id, {item["channel_id"] for item in direct_items})
        self.assertNotIn(
            group_channel.id, {item["channel_id"] for item in direct_items}
        )
        self.assertIn(group_channel.id, {item["channel_id"] for item in group_items})
        self.assertEqual(
            {item["channel_id"] for item in tagged_items}, {self.channel.id}
        )

        message = group_channel.sudo()._contact_center_post(
            origin="inbound",
            body="Unread only for the other member",
            author_guest_id=group_identity.mail_guest_id.id,
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
        )
        self.env["contact.center.application"]._publish_message_created(
            group_channel, message, direction="inbound"
        )
        self._api().mark_seen(group_channel.id, message.id)
        current_member = group_channel.sudo().channel_member_ids.filtered(
            lambda member: member.partner_id == self.agent.partner_id
        )
        other_member = group_channel.sudo().channel_member_ids.filtered(
            lambda member: member.partner_id == self.supervisor.partner_id
        )
        current_member.invalidate_recordset(["message_unread_counter"])
        other_member.invalidate_recordset(["message_unread_counter"])
        self.assertEqual(current_member.message_unread_counter, 0)
        self.assertGreater(other_member.message_unread_counter, 0)
        unread_items = self._api().list_conversations(filters={"unread_only": True})[
            "items"
        ]
        self.assertNotIn(
            group_channel.id, {item["channel_id"] for item in unread_items}
        )

        self._api().schedule_followup(
            self.channel.id,
            {
                "client_request_id": str(uuid.uuid4()),
                "date_deadline": fields.Date.to_string(self._today()),
                "summary": "Due today",
            },
        )
        today_items = self._api().list_conversations(
            filters={"activity_timing": "today"}
        )["items"]
        self.assertIn(self.channel.id, {item["channel_id"] for item in today_items})

    def test_outsider_cannot_use_productivity_endpoints(self):
        request_id = str(uuid.uuid4())
        with self.assertRaises(AccessError):
            self._api(self.outsider).post_internal_note(
                self.channel.id, "Private note", request_id
            )
        with self.assertRaises(AccessError):
            self._api(self.outsider).get_productivity(self.channel.id)


@tagged("-at_install", "post_install")
class TestProductivityCompletionConcurrency(TransactionCase):
    """Exercise a lost-response/double-click completion with real transactions."""

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
                    "default_team_id": team.id,
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
                team=team,
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
            case_id = scheduled["activity"]["case_id"]
            chatter_before = (
                env["mail.message"]
                .sudo()
                .search_count(
                    [("model", "=", "contact.center.case"), ("res_id", "=", case_id)]
                )
            )
            cr.commit()  # pylint: disable=invalid-commit
            return {
                "account_id": account.id,
                "activity_id": scheduled["activity"]["id"],
                "agent_id": agent.id,
                "binding_id": binding.id,
                "case_id": case_id,
                "channel_id": channel.id,
                "chatter_before": chatter_before,
                "connection_id": connection.id,
                "guest_id": guest.id,
                "identity_id": identity.id,
                "team_id": team.id,
            }

    def _create_due_scheduled_fixture(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            body = "Release or cancel exactly once"
            job_uuid = str(uuid.uuid4())
            scheduled = (
                env["contact.center.scheduled.message"]
                .with_context(
                    contact_center_productivity_service_token=(
                        _PRODUCTIVITY_SERVICE_TOKEN
                    )
                )
                .create(
                    {
                        "channel_id": fixture["channel_id"],
                        "requested_by_id": fixture["agent_id"],
                        "ui_request_id": str(uuid.uuid4()),
                        "body": body,
                        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                        "scheduled_at": fields.Datetime.now()
                        - datetime.timedelta(seconds=1),
                        "queue_job_uuid": job_uuid,
                    }
                )
            )
            cr.commit()  # pylint: disable=invalid-commit
            return {"id": scheduled.id, "job_uuid": job_uuid}

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

    def _scheduled_transition_transaction(
        self, fixture, scheduled, operation, retry_count=0
    ):
        with self.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = '5s'")
            cr.execute("SET LOCAL statement_timeout = '10s'")
            env = api.Environment(cr, SUPERUSER_ID, {})
            try:
                if operation == "release":
                    result = (
                        env["contact.center.scheduled.message"]
                        .browse(scheduled["id"])
                        .with_context(job_uuid=scheduled["job_uuid"])
                        ._job_release()
                    )
                    outcome = "released" if result else "noop"
                else:
                    try:
                        env["contact.center.ui.api"].with_user(
                            fixture["agent_id"]
                        ).cancel_scheduled_message(
                            fixture["channel_id"], scheduled["id"]
                        )
                        outcome = "cancelled"
                    except ValidationError:
                        outcome = "too_late"
                cr.commit()  # pylint: disable=invalid-commit
                return outcome
            except SerializationFailure:
                cr.rollback()
                if retry_count >= 2:
                    raise
                return self._scheduled_transition_transaction(
                    fixture, scheduled, operation, retry_count=retry_count + 1
                )

    def _scheduled_transition_worker(
        self, fixture, scheduled, operation, barrier, results
    ):
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT id FROM contact_center_scheduled_message WHERE id = %s",
                [scheduled["id"]],
            )
            barrier.wait(timeout=self.WORKER_TIMEOUT_SECONDS)
            env = api.Environment(cr, SUPERUSER_ID, {})
            try:
                if operation == "release":
                    result = (
                        env["contact.center.scheduled.message"]
                        .browse(scheduled["id"])
                        .with_context(job_uuid=scheduled["job_uuid"])
                        ._job_release()
                    )
                    outcome = "released" if result else "noop"
                else:
                    try:
                        env["contact.center.ui.api"].with_user(
                            fixture["agent_id"]
                        ).cancel_scheduled_message(
                            fixture["channel_id"], scheduled["id"]
                        )
                        outcome = "cancelled"
                    except ValidationError:
                        outcome = "too_late"
                cr.commit()  # pylint: disable=invalid-commit
                results[operation] = outcome
            except SerializationFailure:
                cr.rollback()
                results[operation] = self._scheduled_transition_transaction(
                    fixture, scheduled, operation
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
            cases = (
                env["contact.center.case"]
                .sudo()
                .with_context(active_test=False)
                .search([("channel_id", "=", fixture["channel_id"])])
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
                [("res_model", "=", "contact.center.case"), ("res_id", "in", cases.ids)]
            ).unlink()
            env["mail.message"].sudo().search(
                [
                    "|",
                    "&",
                    ("model", "=", "mail.channel"),
                    ("res_id", "=", fixture["channel_id"]),
                    "&",
                    ("model", "=", "contact.center.case"),
                    ("res_id", "in", cases.ids),
                ]
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
                            ("model", "=", "contact.center.case"),
                            ("res_id", "=", fixture["case_id"]),
                        ]
                    ),
                    fixture["chatter_before"] + 1,
                )
        finally:
            self._cleanup_committed_fixture(fixture)

    def test_concurrent_scheduled_release_and_cancel_have_no_lock_inversion(self):
        fixture = self._setup_committed_fixture(uuid.uuid4().hex)
        scheduled = self._create_due_scheduled_fixture(fixture)
        barrier = threading.Barrier(2)
        results = {}
        workers = [
            threading.Thread(
                target=self._scheduled_transition_worker,
                args=(fixture, scheduled, operation, barrier, results),
            )
            for operation in ("release", "cancel")
        ]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(self.WORKER_TIMEOUT_SECONDS)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(set(results), {"release", "cancel"})
            self.assertIn(results["release"], {"released", "noop"})
            self.assertIn(results["cancel"], {"cancelled", "too_late"})
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                record = env["contact.center.scheduled.message"].browse(scheduled["id"])
                self.assertIn(
                    record.state,
                    {"released", "cancelled"},
                    "scheduled transition ended in %s (%s: %s); workers=%r"
                    % (
                        record.state,
                        record.last_error_class or "no error class",
                        record.last_error_message or "no error message",
                        results,
                    ),
                )
                self.assertEqual(bool(record.message_id), record.state == "released")
        finally:
            self._cleanup_committed_fixture(fixture)
