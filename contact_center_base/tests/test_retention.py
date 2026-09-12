"""Retention invariants: scope, approval, races, partial purge and owned media."""

import base64
import datetime
import uuid
from unittest import mock

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.job import Job

from ..models.retention import _service_context
from ..services.adapter import ProviderAdapter, adapter_registry


@adapter_registry.register("test.retention")
class RetentionTestAdapter(ProviderAdapter):
    display_name = "Retention fixture"

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        raise AssertionError("Retention does not normalize or send messages")

    def execute_command(self, connection, command):
        raise AssertionError("Retention must not send messages")

    def get_health(self, connection):
        return {"state": "connected"}

    def get_capabilities(self, connection):
        return {}


class TestHistoryRetention(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent = cls._user("agent", "group_contact_center_agent")
        cls.supervisor = cls._user("supervisor", "group_contact_center_supervisor")
        cls.outsider = cls._user("outsider", "group_contact_center_supervisor")
        cls.members = cls.agent | cls.supervisor
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Retention test group inbox",
                "platform": "whatsapp",
                "company_id": cls.env.company.id,
                "access_user_ids": [(6, 0, cls.members.ids)],
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Retention fixture",
                "account_id": cls.account.id,
                "adapter_key": "test.retention",
                "external_ref": str(uuid.uuid4()),
                "role": "primary",
                "state": "connected",
                "inbound_active": True,
                "outbound_active": False,
            }
        )
        cls.now = datetime.datetime(2026, 9, 12, 12)
        cls.old = cls.now - datetime.timedelta(days=8)
        cls.new = cls.now - datetime.timedelta(days=1)

    @classmethod
    def _user(cls, name, group):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Retention " + name,
                    "login": "retention-%s" % uuid.uuid4(),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [
                        (6, 0, cls.env.ref("contact_center_base." + group).ids)
                    ],
                }
            )
        )

    def setUp(self):
        super().setUp()
        self.service = self.env["contact.center.retention"]
        # Base is independently installable; WuzAPI owns its routing tests. This
        # fixture substitutes only provider eligibility, not any purge behavior.
        supported = mock.patch.object(
            type(self.service),
            "_supported",
            lambda service, binding: bool(
                binding
                and binding.active
                and not binding.merged_into_id
                and binding.conversation_type == "group"
            ),
        )
        supported.start()
        self.addCleanup(supported.stop)
        self.channel, self.binding = self._group()

    def _group(self):
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            identity=self.env["contact.center.identity"],
            name="Retention group",
            conversation_type="group",
            partner_ids=self.members.partner_id.ids,
            guest_ids=[],
        )
        binding = self.env["contact.center.channel.binding"].create(
            {
                "channel_id": channel.id,
                "account_id": self.account.id,
                "conversation_type": "group",
                "conversation_ref": "%s@g.us" % uuid.uuid4().int,
            }
        )
        return channel, binding

    def _api(self, user=None):
        return self.env["contact.center.ui.api"].with_user(user or self.supervisor)

    def _enable(self):
        self.account.with_context(**_service_context()).write(
            {"retention_enabled": True}
        )

    def _message(self, date=None, attachment=None, binding=None, source=None):
        binding = binding or self.binding
        message = binding.channel_id._contact_center_post(
            origin="inbound",
            body="Retention content %s" % uuid.uuid4(),
            date=date or self.old,
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_id=self.agent.partner_id.id,
            partner_ids=[],
            attachment_ids=attachment.ids if attachment else [],
        )
        projection = self.env["contact.center.message.binding"].create(
            {
                "message_id": message.id,
                "channel_binding_id": binding.id,
                "provider_connection_id": self.connection.id,
                "direction": "inbound",
                "origin": "provider",
                "external_message_id": str(uuid.uuid4()),
                "source_inbox_event_id": source.id if source else False,
            }
        )
        self.env["contact.center.application"]._publish_message_created(
            binding.channel_id, message, direction="inbound"
        )
        return message, projection

    def _attachment(self, message, name="old.pdf"):
        attachment = (
            self.env["ir.attachment"]
            .with_context(**_service_context())
            .create(
                {
                    "name": name,
                    "datas": base64.b64encode(b"old media bytes"),
                    "res_model": "mail.message",
                    "res_id": message.id,
                    "mimetype": "application/pdf",
                }
            )
        )
        message.with_context(**_service_context()).write(
            {"attachment_ids": [(4, attachment.id)]}
        )
        return attachment

    def _purge(self, **kwargs):
        return self.service._purge_binding(self.binding, now=self.now, **kwargs)

    def _indexed_event(self, kind, external_ids, date=None, binding=None):
        binding = binding or self.binding
        route = {
            "conversation_type": "group",
            "conversation_ref": binding.conversation_ref,
            "kind": kind,
            "occurred_at": date or self.new,
            "message_id": external_ids[0] if kind == "message" else "",
            "external_ids": external_ids,
            "reply_ids": [],
        }
        with mock.patch.object(
            type(self.service), "_retention_route", return_value=route
        ):
            return (
                self.env["contact.center.inbox.event"]
                .with_context(contact_center_skip_enqueue=True)
                .create(
                    {
                        "provider_connection_id": self.connection.id,
                        "inbox_dedupe_key": str(uuid.uuid4()),
                        "provider_schema_version": "retention-test",
                        "raw_envelope_json": {"text": "Private %s body" % kind},
                        "normalized_dto_json": {"text": "Private %s normalized" % kind},
                        "state": "done",
                    }
                )
            )

    def test_default_off_and_dry_run_are_non_destructive(self):
        message, _projection = self._message()
        self.assertFalse(self.account.retention_enabled)
        self.assertEqual(self.account.retention_days, 7)
        self.assertEqual(self._purge()["message_count"], 0)
        self._enable()
        self.assertEqual(self._purge(dry_run=True)["message_count"], 1)
        self.assertTrue(message.exists())
        self.assertFalse(self.binding.retention_expired_before)

    def test_agents_see_policy_but_cannot_change_or_preview(self):
        self._enable()
        policy = self._api(self.agent).get_retention_policy(self.channel.id)["policy"]
        self.assertTrue(policy["effective"])
        self.assertFalse(policy["can_manage"])
        with self.assertRaises(AccessError):
            self._api(self.agent).set_retention_preserve(self.channel.id, True)
        with self.assertRaises(AccessError):
            self._api(self.agent).get_retention_policy(self.channel.id, preview=True)
        with self.assertRaises(AccessError):
            self._api(self.outsider).set_retention_preserve(self.channel.id, True)

    def test_direct_writes_cannot_enable_or_lower_watermark(self):
        with self.assertRaises(AccessError):
            self.account.write({"retention_enabled": True})
        with self.assertRaises(AccessError):
            self.binding.sudo().write({"retention_preserve": True})
        self.binding.with_context(**_service_context()).write(
            {"retention_expired_before": self.old}
        )
        with self.assertRaises(ValidationError):
            self.binding.with_context(**_service_context()).write(
                {"retention_expired_before": False}
            )

    def test_preserve_is_shared_and_reenable_requires_fresh_simulation(self):
        self._enable()
        message, _projection = self._message()
        self._api().set_retention_preserve(self.channel.id, True)
        self.assertTrue(
            self._api(self.agent).get_retention_policy(self.channel.id)["policy"][
                "preserve"
            ]
        )
        self.assertEqual(self._purge()["message_count"], 0)
        preview = self._api().get_retention_policy(self.channel.id, preview=True)[
            "preview"
        ]
        with self.assertRaises(ValidationError):
            self._api().set_retention_preserve(self.channel.id, False)
        self._api().set_retention_preserve(
            self.channel.id, False, preview["confirmation_token"]
        )
        self.assertTrue(
            message.exists(), "Policy changes must not purge in the user RPC"
        )

    def test_confirmation_cannot_be_reused_after_policy_revision(self):
        self._enable()
        self._api().set_retention_preserve(self.channel.id, True)
        preview = self._api().get_retention_policy(self.channel.id, preview=True)[
            "preview"
        ]
        self.account.with_context(**_service_context()).write({"retention_revision": 1})
        with self.assertRaises(ValidationError):
            self._api().set_retention_preserve(
                self.channel.id, False, preview["confirmation_token"]
            )

    def test_account_wizard_requires_preview_of_exact_target_policy(self):
        wizard = (
            self.env["contact.center.retention.wizard"]
            .with_user(self.supervisor)
            .create(
                {
                    "account_id": self.account.id,
                    "enabled": True,
                    "days": 7,
                }
            )
        )
        with self.assertRaises(ValidationError):
            wizard.action_apply()
        wizard.action_preview()
        wizard.write({"days": 1})
        with self.assertRaises(ValidationError):
            wizard.action_apply()
        wizard.action_preview()
        wizard.action_apply()
        self.assertTrue(self.account.retention_enabled)
        self.assertEqual(self.account.retention_days, 1)

    def test_original_date_cutoff_is_exclusive_and_batch_is_bounded(self):
        self._enable()
        old, old_binding = self._message()
        same_time, _same_binding = self._message()
        boundary, _boundary_binding = self._message(
            self.now - datetime.timedelta(days=7)
        )
        recent, _recent_binding = self._message(self.new)
        old_external_id = old_binding.external_message_id
        self.assertEqual(self._purge(limit=1)["message_count"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(same_time.exists())
        self.assertEqual(self.binding.retention_expired_before, self.old)
        self.assertTrue(
            self.env["contact.center.retention.receipt"]
            .sudo()
            .search_count(
                [
                    ("account_id", "=", self.account.id),
                    ("external_message_id", "=", old_external_id),
                ]
            )
        )
        self._purge()
        self.assertTrue(boundary.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(self.channel.exists())
        self.assertEqual(self.channel.contact_center_last_message_id, recent)

    def test_exclusive_media_and_consumed_upload_are_deleted(self):
        self._enable()
        message, projection = self._message()
        attachment = self._attachment(message)
        media = (
            self.env["contact.center.media.binding"]
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "message_binding_id": projection.id,
                    "kind": "document",
                    "attachment_id": attachment.id,
                    "state": "ready",
                    "remote_locator_json": {
                        "private_url": "https://invalid.example/old"
                    },
                }
            )
        )
        upload = self.env["contact.center.media.upload"].create(
            {
                "channel_binding_id": self.binding.id,
                "uploaded_by_user_id": self.agent.id,
                "attachment_id": attachment.id,
                "consumed_message_binding_id": projection.id,
                "kind": "document",
                "mime_type": "application/pdf",
                "file_name": "old.pdf",
                "size_bytes": 15,
                "sha256": "a" * 64,
                "state": "consumed",
            }
        )
        self._purge()
        self.assertFalse(media.exists())
        self.assertFalse(upload.exists())
        self.assertFalse(attachment.exists())

    def test_shared_message_attachment_is_rehomed_and_business_owned_is_preserved(self):
        self._enable()
        message, _projection = self._message()
        shared = self._attachment(message)
        recent, _recent_projection = self._message(self.new)
        recent.with_context(**_service_context()).write(
            {"attachment_ids": [(4, shared.id)]}
        )
        business = self.env["ir.attachment"].create(
            {
                "name": "Business document",
                "datas": base64.b64encode(b"keep"),
                "res_model": "res.partner",
                "res_id": self.agent.partner_id.id,
            }
        )
        message.with_context(**_service_context()).write(
            {"attachment_ids": [(4, business.id)]}
        )
        self._purge()
        self.assertTrue(shared.exists())
        self.assertEqual((shared.res_model, shared.res_id), ("mail.message", recent.id))
        self.assertTrue(business.exists())
        self.assertEqual(business.res_model, "res.partner")

    def test_read_cursors_keep_recent_unread_and_other_user_state(self):
        self._enable()
        old, _projection = self._message()
        recent, _recent_projection = self._message(self.new)
        agent_member = self.channel.with_user(
            self.agent
        )._contact_center_member_for_current_user()
        supervisor_member = self.channel.with_user(
            self.supervisor
        )._contact_center_member_for_current_user()
        agent_member.with_context(**_service_context()).write(
            {"seen_message_id": old.id, "fetched_message_id": old.id}
        )
        supervisor_member.with_context(**_service_context()).write(
            {"seen_message_id": recent.id}
        )
        self._purge()
        self.assertFalse(agent_member.seen_message_id)
        self.assertEqual(supervisor_member.seen_message_id, recent)
        self.assertEqual(agent_member.message_unread_counter, 1)
        self.assertEqual(supervisor_member.message_unread_counter, 0)

    def test_running_media_job_blocks_whole_batch_without_partial_deletion(self):
        self._enable()
        message, projection = self._message()
        media = (
            self.env["contact.center.media.binding"]
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "message_binding_id": projection.id,
                    "kind": "image",
                    "state": "downloading",
                }
            )
        )
        queued = Job(media._job_download, identity_key="retention-test:%s" % media.id)
        queued.store()
        media.write({"queue_job_uuid": queued.uuid})
        job = self.env["queue.job"].search([("uuid", "=", queued.uuid)])
        job.write({"state": "started"})
        with self.assertRaises(ValidationError):
            self._purge()
        self.assertTrue(message.exists())
        self.assertFalse(self.binding.retention_expired_before)

    def test_preserve_winner_is_rechecked_after_policy_fence(self):
        self._enable()
        message, _projection = self._message()
        binding_type = type(self.binding)
        lock = binding_type._contact_center_lock_identity_channel_binding

        def preserve_then_lock(binding):
            binding.with_context(**_service_context()).write(
                {"retention_preserve": True}
            )
            return lock(binding)

        with mock.patch.object(
            binding_type,
            "_contact_center_lock_identity_channel_binding",
            new=preserve_then_lock,
        ):
            self.assertEqual(self._purge()["message_count"], 0)
        self.assertTrue(message.exists())

    def test_dependency_failure_rolls_back_without_detaching_business(self):
        self._enable()
        message, _projection = self._message()
        with mock.patch.object(
            type(self.service),
            "_retention_prepare_dependencies",
            side_effect=ValidationError("Business dependency"),
        ):
            with self.assertRaises(ValidationError), self.env.cr.savepoint():
                self._purge()
        self.assertTrue(message.exists())
        self.assertFalse(self.binding.retention_expired_before)

    def test_disabling_retention_preserves_monotonic_expired_boundary(self):
        self._enable()
        self._message()
        self._purge()
        watermark = self.binding.retention_expired_before
        self.account.with_context(**_service_context()).write(
            {"retention_enabled": False, "retention_days": 30}
        )
        self._api().set_retention_preserve(self.channel.id, True)
        self.assertEqual(self.binding.retention_expired_before, watermark)

    def test_source_envelope_and_private_copies_become_content_free_receipt(self):
        self._enable()
        event = (
            self.env["contact.center.inbox.event"]
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": str(uuid.uuid4()),
                    "provider_schema_version": "retention-test",
                    "raw_envelope_json": {"text": "Expired source body"},
                    "normalized_dto_json": {"text": "Expired normalized body"},
                    "state": "done",
                }
            )
        )
        message, projection = self._message(source=event)
        projection.write(
            {
                "original_body": "Expired edited body",
                "protocol_snapshot_json": {"text": "Expired protocol copy"},
            }
        )
        dedupe_key = event.inbox_dedupe_key
        self._purge()
        self.assertFalse(message.exists())
        self.assertFalse(projection.exists())
        self.assertEqual(event.inbox_dedupe_key, dedupe_key)
        self.assertEqual(event.raw_envelope_json, {"content_erased": True})
        self.assertFalse(event.normalized_dto_json)
        self.assertEqual(event.state, "blocked")

    def test_old_failed_job_and_pending_retry_are_removed_by_stable_identity(self):
        self._enable()
        _message, projection = self._message()
        media = (
            self.env["contact.center.media.binding"]
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "message_binding_id": projection.id,
                    "kind": "image",
                    "state": "pending",
                }
            )
        )
        identity = "contact_center:media:%s" % media.id
        previous = Job(media._job_download, identity_key=identity)
        previous.store()
        previous_record = self.env["queue.job"].search([("uuid", "=", previous.uuid)])
        previous_record.write({"state": "failed", "exc_info": "Expired error content"})
        retry = Job(media._job_download, identity_key=identity)
        retry.store()
        media.write({"queue_job_uuid": retry.uuid})
        self._purge()
        self.assertFalse(
            self.env["queue.job"].search_count([("identity_key", "=", identity)])
        )

    def test_expired_cancelled_schedule_is_removed_but_future_intent_survives(self):
        self._enable()
        model = self.env["contact.center.scheduled.message"].with_context(
            **_service_context()
        )
        values = {
            "channel_id": self.channel.id,
            "requested_by_id": self.agent.id,
            "ui_request_id": str(uuid.uuid4()),
            "body": "Cancelled old content",
            "body_sha256": "b" * 64,
            "scheduled_at": self.old,
            "state": "cancelled",
        }
        expired = model.create(values)
        future = model.create(
            dict(
                values,
                ui_request_id=str(uuid.uuid4()),
                scheduled_at=self.now + datetime.timedelta(days=1),
                state="scheduled",
            )
        )
        self._purge()
        self.assertFalse(expired.exists())
        self.assertTrue(future.exists())

    def test_recent_mutation_expires_with_old_target_and_mixed_receipt_survives(self):
        self._enable()
        message, projection = self._message()
        recent, survivor = self._message(self.new)
        mutation = self._indexed_event("mutation", [projection.external_message_id])
        mixed_receipt = self._indexed_event(
            "receipt",
            [projection.external_message_id, survivor.external_message_id],
            self.old,
        )
        old_receipt = self._indexed_event(
            "receipt", [projection.external_message_id], self.old
        )
        _other_channel, other_group = self._group()
        unrelated = self._indexed_event(
            "mutation", [projection.external_message_id], binding=other_group
        )
        self.assertEqual(mutation.retention_target_id, projection.external_message_id)
        self._purge()
        self.assertFalse(message.exists())
        self.assertTrue(recent.exists())
        self.assertEqual(mutation.raw_envelope_json, {"content_erased": True})
        self.assertFalse(mutation.normalized_dto_json)
        self.assertEqual(old_receipt.raw_envelope_json, {"content_erased": True})
        self.assertIn("text", mixed_receipt.raw_envelope_json)
        self.assertIn("text", unrelated.raw_envelope_json)

    def test_multi_target_mutation_or_surviving_projection_blocks_expiry(self):
        self._enable()
        message, projection = self._message()
        recent, survivor = self._message(self.new)
        mixed = self._indexed_event(
            "mutation", [projection.external_message_id, survivor.external_message_id]
        )
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._purge()
        self.assertTrue(message.exists())
        self.assertTrue(recent.exists())
        self.assertIn("text", mixed.raw_envelope_json)
        self.assertFalse(self.binding.retention_expired_before)

    def test_dependency_staging_keeps_source_jobs_receipts_and_watermark(self):
        self._enable()
        message, projection = self._message()
        media = (
            self.env["contact.center.media.binding"]
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "message_binding_id": projection.id,
                    "kind": "image",
                    "state": "pending",
                }
            )
        )
        queued = Job(
            media._job_download, identity_key="contact_center:media:%s" % media.id
        )
        queued.store()
        media.write({"queue_job_uuid": queued.uuid})
        with mock.patch.object(
            type(self.service), "_retention_prepare_dependencies", return_value=False
        ):
            self.assertTrue(self._purge()["staging"])
        self.assertTrue(message.exists())
        self.assertFalse(self.binding.retention_expired_before)
        self.assertFalse(
            self.env["contact.center.retention.receipt"]
            .sudo()
            .search_count(
                [
                    ("account_id", "=", self.account.id),
                    ("external_message_id", "=", projection.external_message_id),
                ]
            )
        )
        job = self.env["queue.job"].search([("uuid", "=", queued.uuid)])
        self.assertEqual(job.state, "pending")
