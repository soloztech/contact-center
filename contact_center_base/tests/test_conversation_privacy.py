import datetime
import hashlib
import uuid
from unittest import mock

from psycopg2.errors import SerializationFailure

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.job import Job

from ..models.productivity import _PRODUCTIVITY_SERVICE_TOKEN
from ..services.adapter import ProviderAdapter, adapter_registry
from ..services.tokens import CONTACT_CENTER_POST_TOKEN


@adapter_registry.register("test.conversation.privacy")
class ConversationPrivacyAdapter(ProviderAdapter):
    display_name = "Conversation Privacy Fixture"

    def authenticate_webhook(self, connection, headers, body):
        return True

    def execute_command(self, connection, command):
        raise AssertionError("Privacy operations must never send provider commands")

    def prepare_request_snapshot(self, connection, command):
        raise AssertionError("Privacy operations must never send provider commands")

    def get_health(self, connection):
        return {"state": "connected"}

    def conversation_route(self, connection, envelope):
        return envelope.get("route")

    def normalize_event(self, connection, envelope):
        raise AssertionError("Discarded content must never reach normalization")

    def get_capabilities(self, connection):
        return {}


class TestConversationPrivacy(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent = cls._user("Agent", "contact_center_base.group_contact_center_agent")
        cls.admin = cls._user("Admin", "contact_center_base.group_contact_center_admin")
        cls.outsider = cls._user(
            "Outsider", "contact_center_base.group_contact_center_agent"
        )
        cls.members = cls.agent | cls.admin
        cls.account = cls._account()
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Privacy provider",
                "account_id": cls.account.id,
                "adapter_key": "test.conversation.privacy",
                "external_ref": str(uuid.uuid4()),
                "state": "connected",
                "role": "primary",
                "active": True,
                "inbound_active": True,
                "outbound_active": False,
            }
        )

    @classmethod
    def _user(cls, name, group):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Privacy %s" % name,
                    "login": "privacy-%s" % uuid.uuid4(),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, cls.env.ref(group).ids)],
                }
            )
        )

    @classmethod
    def _account(cls):
        return cls.env["contact.center.account"].create(
            {
                "name": "Privacy inbox %s" % uuid.uuid4(),
                "platform": "whatsapp",
                "company_id": cls.env.company.id,
                "access_user_ids": [(6, 0, cls.members.ids)],
                "conversation_delete_enabled": True,
                "conversation_ignore_enabled": True,
            }
        )

    def _conversation(self, account=None, identity=None, group=False):
        account = account or self.account
        if not identity and not group:
            guest = self.env["mail.guest"].create({"name": "Privacy guest"})
            identity = self.env["contact.center.identity"].create(
                {
                    "name": guest.name,
                    "mail_guest_id": guest.id,
                    "company_id": account.company_id.id,
                }
            )
        identity = identity or self.env["contact.center.identity"]
        reference = "%s@%s" % (uuid.uuid4().int, "g.us" if group else "s.whatsapp.net")
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=account,
            identity=identity,
            name="Privacy conversation",
            conversation_type="group" if group else "direct",
            partner_ids=self.members.partner_id.ids,
            guest_ids=identity.mail_guest_id.ids,
        )
        binding = self.env["contact.center.channel.binding"].create(
            {
                "channel_id": channel.id,
                "account_id": account.id,
                "identity_id": identity.id,
                "conversation_type": "group" if group else "direct",
                "conversation_ref": reference,
            }
        )
        if identity:
            self.env["contact.center.identity.alias"].create(
                {
                    "identity_id": identity.id,
                    "account_id": account.id,
                    "namespace": "whatsapp.pn",
                    "value_raw": reference,
                    "value_normalized": reference,
                }
            )
        return channel, binding

    def _api(self, user=None):
        return self.env["contact.center.ui.api"].with_user(user or self.agent)

    def _inbox(self, binding, text="Private raw content"):
        route = {
            "conversation_type": binding.conversation_type,
            "conversation_ref": binding.conversation_ref,
            "addresses": [["whatsapp.pn", binding.conversation_ref]],
        }
        return (
            self.env["contact.center.inbox.event"]
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": str(uuid.uuid4()),
                    "provider_schema_version": "privacy-v1",
                    "raw_envelope_json": {
                        "route": route,
                        "unsupported": {"text": text},
                    },
                }
            )
        )

    def _message(self, channel, binding, inbox=None):
        message = channel._contact_center_post(
            origin="inbound",
            body="Private conversation content",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_guest_id=binding.identity_id.mail_guest_id.id,
            partner_ids=[],
        )
        projected = self.env["contact.center.message.binding"].create(
            {
                "message_id": message.id,
                "channel_binding_id": binding.id,
                "provider_connection_id": self.connection.id,
                "source_inbox_event_id": inbox.id if inbox else False,
                "direction": "inbound",
                "origin": "provider",
                "external_message_id": str(uuid.uuid4()),
            }
        )
        return message, projected

    def test_ignore_preserves_history_and_survives_delete_with_agent_reversal(self):
        channel, binding = self._conversation()
        message, _projection = self._message(channel, binding)
        item = self._api().set_conversation_ignored(channel.id, True)["item"]
        self.assertTrue(item["ignored"])
        self.assertTrue(message.exists())
        rule = self.env["contact.center.conversation.ignore"]._for_binding(binding)
        self.assertEqual(len(rule), 1)
        route = self._inbox(binding).raw_envelope_json["route"]
        self._api().delete_conversation(channel.id)
        self.assertTrue(rule.exists())
        self.assertTrue(rule._matches_route(self.account, route))
        rule.with_user(self.agent).action_stop_ignoring()
        self.assertFalse(rule._matches_route(self.account, route))
        self.assertFalse(channel.exists())
        self.assertFalse(message.exists())
        menu = self.env.ref(
            "contact_center_base.menu_contact_center_conversation_ignore"
        )
        self.assertEqual(
            menu.parent_id, self.env.ref("contact_center_base.menu_contact_center_root")
        )

    def test_ignore_follows_identity_aliases_but_never_another_inbox_or_group(self):
        channel, binding = self._conversation()
        self._api().set_conversation_ignored(channel.id, True)
        self.env["contact.center.identity.alias"].create(
            {
                "identity_id": binding.identity_id.id,
                "account_id": self.account.id,
                "namespace": "whatsapp.lid",
                "value_raw": "12345@lid",
                "value_normalized": "12345@lid",
            }
        )
        route = {
            "conversation_type": "direct",
            "conversation_ref": "12345@lid",
            "addresses": [["whatsapp.lid", "12345@lid"]],
        }
        policy = self.env["contact.center.conversation.ignore"]
        self.assertTrue(policy._matches_route(self.account, route))
        second = self._account()
        self.assertFalse(policy._matches_route(second, route))
        group_channel, group_binding = self._conversation(group=True)
        group_route = {
            "conversation_type": "group",
            "conversation_ref": group_binding.conversation_ref,
            "addresses": route["addresses"],
        }
        self.assertFalse(policy._matches_route(self.account, group_route))
        self._api().set_conversation_ignored(group_channel.id, True)
        self.assertTrue(policy._matches_route(self.account, group_route))
        self.assertFalse(policy._matches_route(second, group_route))
        self._api().set_conversation_ignored(channel.id, False)
        self.assertFalse(policy._matches_route(self.account, route))
        self.assertTrue(policy._matches_route(self.account, group_route))

    def test_reversal_notifies_live_conversations_and_rejects_revoked_members(self):
        channel, binding = self._conversation()
        self._api().set_conversation_ignored(channel.id, True)
        rule = self.env["contact.center.conversation.ignore"]._for_binding(binding)
        with mock.patch.object(
            type(self.env["contact.center.application"]), "_notify_ui"
        ) as notify:
            rule.with_user(self.agent).action_stop_ignoring()
        self.assertEqual(notify.call_args.args[0], channel)
        self.assertEqual(notify.call_args.args[1], "conversation_updated")
        self._api().set_conversation_ignored(channel.id, True)
        self.account.write({"access_user_ids": [(3, self.agent.id)]})
        with self.assertRaises(AccessError):
            rule.with_user(self.agent).action_stop_ignoring()
        self.assertTrue(binding._contact_center_is_ignored())

    def test_flags_membership_and_forged_context_are_enforced(self):
        channel, binding = self._conversation()
        self.account.write(
            {"conversation_delete_enabled": False, "conversation_ignore_enabled": False}
        )
        for action, args in (
            ("delete_conversation", (channel.id,)),
            ("set_conversation_ignored", (channel.id, True)),
        ):
            with self.assertRaises(AccessError):
                getattr(self._api(), action)(*args)
            with self.assertRaises(AccessError):
                getattr(self._api(self.outsider), action)(*args)
        self.assertTrue(
            self._api(self.admin).set_conversation_ignored(channel.id, True)["item"][
                "ignored"
            ]
        )
        rule = self.env["contact.center.conversation.ignore"]._for_binding(binding)
        for token in (True, "CONTACT_CENTER_DELETION_TOKEN"):
            with self.assertRaises(AccessError):
                rule.with_context(contact_center_conversation_policy_token=token).write(
                    {"active": False}
                )
            with self.assertRaises(AccessError):
                self.account.with_context(
                    contact_center_conversation_policy_token=token
                ).write({"conversation_policy_revision": 999})
        self._api(self.admin).delete_conversation(channel.id)
        self.assertFalse(channel.exists())

    def test_admin_bypass_still_rejects_inactive_company_scope(self):
        channel, _binding = self._conversation()
        other_company = self.env["res.company"].create(
            {"name": "Privacy other company"}
        )
        self.admin.write({"company_ids": [(4, other_company.id)]})
        api = self._api(self.admin).with_context(allowed_company_ids=other_company.ids)
        with self.assertRaises(AccessError):
            api.delete_conversation(channel.id)
        with self.assertRaises(AccessError):
            api.set_conversation_ignored(channel.id, True)
        self.assertTrue(channel.exists())

    def test_delete_purges_native_messages_notes_schedules_and_private_attachments(
        self,
    ):
        channel, binding = self._conversation()
        other_channel, other_binding = self._conversation()
        inbox = self._inbox(binding)
        pending = self._inbox(binding, "Unprojected unsupported payload")
        unrelated = self._inbox(other_binding, "Keep this other customer's content")
        original_key = inbox.inbox_dedupe_key
        message, projection = self._message(channel, binding, inbox)
        other_message, _other_projection = self._message(other_channel, other_binding)
        partner = self.env["res.partner"].create({"name": "Keep business owner"})
        business_attachment = self.env["ir.attachment"].create(
            {
                "name": "Business.pdf",
                "type": "binary",
                "datas": "cGRm",
                "res_model": "res.partner",
                "res_id": partner.id,
            }
        )
        private_attachment = self.env["ir.attachment"].create(
            {
                "name": "Private.txt",
                "type": "binary",
                "datas": "cHJpdmF0ZQ==",
                "res_model": "mail.message",
                "res_id": message.id,
            }
        )
        message.with_context(contact_center_post_token=CONTACT_CENTER_POST_TOKEN).write(
            {
                "attachment_ids": [
                    (4, business_attachment.id),
                    (4, private_attachment.id),
                ]
            }
        )
        self._api().post_internal_note(channel.id, "Private note", str(uuid.uuid4()))
        notes = self.env["contact.center.internal.note.request"].search(
            [("channel_id", "=", channel.id)]
        )
        schedule = (
            self.env["contact.center.scheduled.message"]
            .with_context(
                contact_center_productivity_service_token=_PRODUCTIVITY_SERVICE_TOKEN
            )
            .create(
                {
                    "channel_id": channel.id,
                    "requested_by_id": self.agent.id,
                    "ui_request_id": str(uuid.uuid4()),
                    "body": "Private scheduled text",
                    "body_sha256": hashlib.sha256(
                        b"Private scheduled text"
                    ).hexdigest(),
                    "scheduled_at": fields.Datetime.now() + datetime.timedelta(hours=1),
                }
            )
        )
        self._api().schedule_followup(
            channel.id,
            {
                "client_request_id": str(uuid.uuid4()),
                "date_deadline": fields.Date.to_string(fields.Date.today()),
                "summary": "Private follow-up",
                "note": "Private follow-up details",
                "user_id": self.agent.id,
            },
        )
        followups = self.env["contact.center.followup.request"].search(
            [("channel_id", "=", channel.id)]
        )
        activities = followups.activity_id
        self.assertTrue(activities)
        with mock.patch.object(
            type(self.env["contact.center.application"]), "_notify_ui"
        ) as notify:
            response = self._api().delete_conversation(channel.id)
        self.assertEqual(
            response,
            {
                "schema_version": 1,
                "channel_id": channel.id,
                "removed_from_conversation": True,
            },
        )
        self.assertEqual(notify.call_args.args[1], "conversation_deleted")
        for deleted in (
            channel,
            binding,
            message,
            projection,
            notes,
            schedule,
            followups,
            activities,
            private_attachment,
        ):
            self.assertFalse(deleted.exists(), deleted._name)
        self.assertTrue(partner.exists())
        self.assertTrue(business_attachment.exists())
        self.assertTrue(other_channel.exists())
        self.assertTrue(other_message.exists())
        self.assertEqual(inbox.inbox_dedupe_key, original_key)
        for erased in (inbox, pending):
            erased.flush_recordset(
                ["raw_envelope_json", "normalized_dto_json", "metadata_json"]
            )
            erased.invalidate_recordset(
                ["raw_envelope_json", "normalized_dto_json", "metadata_json"]
            )
            self.assertEqual(erased.raw_envelope_json, {"content_erased": True})
            self.assertFalse(erased.normalized_dto_json)
            self.assertTrue(erased.metadata_json["content_erased"])
            self.assertEqual(erased.state, "blocked")
            with self.assertRaises(ValidationError):
                erased.with_user(self.admin).action_requeue()
            with self.assertRaises(ValidationError):
                erased._normalize_one()
        self.assertIn("Keep this", str(unrelated.raw_envelope_json))

    def test_queued_ignored_content_is_discarded_inside_retry_boundary(self):
        channel, binding = self._conversation()
        event = self._inbox(binding)
        self._api().set_conversation_ignored(channel.id, True)
        job_uuid = str(uuid.uuid4())
        event.write({"queue_job_uuid": job_uuid})
        self.assertFalse(event.with_context(job_uuid=job_uuid)._job_process())
        event.flush_recordset(
            ["raw_envelope_json", "normalized_dto_json", "metadata_json"]
        )
        event.invalidate_recordset(
            ["raw_envelope_json", "normalized_dto_json", "metadata_json"]
        )
        self.assertEqual(event.raw_envelope_json, {"content_erased": True})
        self.assertEqual(event.state, "blocked")
        fresh = self._inbox(binding)
        fresh.write({"queue_job_uuid": job_uuid})
        application = type(self.env["contact.center.application"])
        with mock.patch.object(
            application,
            "_lock_inbound_account_scope",
            side_effect=SerializationFailure("concurrent ignore"),
        ):
            with self.assertRaises(RetryableJobError) as caught:
                fresh.with_context(job_uuid=job_uuid)._job_process()
        self.assertTrue(caught.exception.ignore_retry)
        self.assertEqual(fresh.state, "pending")
        for token in (True, "CONTACT_CENTER_DELETION_TOKEN"):
            with self.assertRaises(AccessError):
                fresh.with_context(
                    contact_center_deletion_token=token
                )._erase_conversation_content("conversation_deleted")

    def test_archived_merge_redirect_is_detached_without_deleting_its_history(self):
        channel, binding = self._conversation()
        retired_channel, retired_binding = self._conversation()
        retired_message, _retired_projection = self._message(
            retired_channel, retired_binding
        )
        retired_binding.write({"active": False, "merged_into_id": binding.id})
        self._api().delete_conversation(channel.id)
        self.assertFalse(channel.exists())
        self.assertTrue(retired_channel.exists())
        self.assertTrue(retired_message.exists())
        self.assertFalse(retired_binding.active)
        self.assertFalse(retired_binding.merged_into_id)

    def _outbox(self, channel, binding, state="pending"):
        message = channel._contact_center_post(
            origin="outbound",
            body="Private queued send",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_id=self.agent.partner_id.id,
            partner_ids=[],
        )
        projected = self.env["contact.center.message.binding"].create(
            {
                "message_id": message.id,
                "channel_binding_id": binding.id,
                "provider_connection_id": self.connection.id,
                "direction": "outbound",
                "origin": "agent",
                "client_message_id": str(uuid.uuid4()),
            }
        )
        return (
            self.env["contact.center.outbox.command"]
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "account_id": self.account.id,
                    "provider_connection_id": self.connection.id,
                    "channel_binding_id": binding.id,
                    "message_binding_id": projected.id,
                    "outbox_idempotency_key": str(uuid.uuid4()),
                    "command_type": "send_message",
                    "command_json": {"text": "Private queued send"},
                    "state": state,
                }
            )
        )

    def test_delete_cancels_pending_outbox_job_before_purging_command(self):
        channel, binding = self._conversation()
        command = self._outbox(channel, binding)
        message = command.message_binding_id.message_id
        queued = Job(
            command._job_process, identity_key="contact_center:outbox:%s" % command.id
        )
        queued.store()
        command.write({"queue_job_uuid": queued.uuid})
        job = self.env["queue.job"].search([("uuid", "=", queued.uuid)])
        self.assertEqual(job.state, "pending")
        queue_class = type(job)
        cancel = queue_class.button_cancelled
        cancelled_states = []

        def cancel_and_observe(records):
            result = cancel(records)
            cancelled_states.extend(records.mapped("state"))
            return result

        with mock.patch.object(queue_class, "button_cancelled", new=cancel_and_observe):
            self._api().delete_conversation(channel.id)
        self.assertEqual(cancelled_states, ["cancelled"])
        self.assertFalse(job.exists())
        self.assertFalse(command.exists())
        self.assertFalse(message.exists())
        self.assertFalse(channel.exists())

    def test_delete_refuses_started_or_uncertain_sends_atomically(self):
        for state in ("processing", "uncertain"):
            with self.subTest(state=state):
                channel, binding = self._conversation()
                event = self._inbox(binding)
                command = self._outbox(channel, binding, state=state)
                command.write(
                    {
                        "dispatch_started_at": fields.Datetime.now(),
                        "dispatch_job_uuid": str(uuid.uuid4()),
                    }
                )
                message = command.message_binding_id.message_id
                revision = self.account.conversation_policy_revision
                with self.assertRaises(ValidationError), self.env.cr.savepoint():
                    self._api().delete_conversation(channel.id)
                self.account.invalidate_recordset(["conversation_policy_revision"])
                self.assertEqual(self.account.conversation_policy_revision, revision)
                self.assertTrue(channel.exists())
                self.assertTrue(command.exists())
                self.assertTrue(message.exists())
                self.assertEqual(command.state, state)
                self.assertIn("Private raw content", str(event.raw_envelope_json))
