"""Queue, scope, provider snapshots, and deletion invariants for transcription."""

import uuid
from unittest import mock

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.job import Job

from ..services.adapter import ProviderAdapter, adapter_registry
from ..services.tokens import CONTACT_CENTER_POST_TOKEN
from ..services.transcription import TranscriptionError, TranscriptionResult


@adapter_registry.register("test.transcription")
class TranscriptionTransportFixture(ProviderAdapter):
    display_name = "Transcription transport fixture"

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        raise AssertionError(
            "Transcription tests must not contact a messaging provider"
        )

    def execute_command(self, connection, command):
        raise AssertionError("Transcription tests must not send a message")

    def get_capabilities(self, connection):
        return {}

    def get_health(self, connection):
        return {"state": "connected"}


@tagged("post_install", "-at_install", "contact_center_transcription")
class TestAudioTranscription(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent = cls._user("agent", "group_contact_center_agent")
        cls.supervisor = cls._user("supervisor", "group_contact_center_supervisor")
        cls.outsider = cls._user("outsider", "group_contact_center_agent")
        cls.admin = cls._user("admin", "group_contact_center_admin")
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Transcription fixture inbox",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "access_user_ids": [(6, 0, (cls.agent | cls.supervisor).ids)],
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Transcription transport",
                "account_id": cls.account.id,
                "adapter_key": "test.transcription",
                "external_ref": str(uuid.uuid4()),
                "role": "primary",
                "state": "connected",
                "active": True,
                "inbound_active": True,
                "outbound_active": False,
            }
        )
        guest = cls.env["mail.guest"].create({"name": "Transcription fixture caller"})
        identity = cls.env["contact.center.identity"].create(
            {
                "name": "Transcription fixture caller",
                "company_id": cls.env.company.id,
                "mail_guest_id": guest.id,
            }
        )
        cls.channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=cls.account,
            identity=identity,
            partner_ids=(cls.agent | cls.supervisor).partner_id.ids,
            guest_ids=guest.ids,
        )
        cls.binding = cls.env["contact.center.channel.binding"].create(
            {
                "channel_id": cls.channel.id,
                "account_id": cls.account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": "transcription-" + str(uuid.uuid4()),
            }
        )
        cls.provider = cls.env["contact.center.transcription.provider"].create(
            {
                "name": "Synthetic speech provider",
                "backend": "openai",
                "model": "gpt-transcribe",
                "api_key": "fixture-key",
                "language": "pt",
                "prompt": "Motores de 220 volts.",
            }
        )

    @classmethod
    def _user(cls, name, group):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Transcription " + name,
                    "login": "transcription-" + str(uuid.uuid4()),
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
        provider_patch = mock.patch.object(
            type(self.provider),
            "_transcribe",
            return_value=TranscriptionResult("Pedido de um motor de 220 volts.", "pt"),
        )
        self.transcribe = provider_patch.start()
        self.addCleanup(provider_patch.stop)
        network_patch = mock.patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Real HTTP is forbidden in transcription tests"),
        )
        network_patch.start()
        self.addCleanup(network_patch.stop)

    def _enable(self, mode="manual", provider=None):
        self.account.write(
            {
                "transcription_mode": mode,
                "transcription_provider_id": (provider or self.provider).id,
            }
        )

    def _media(self, *, kind="audio", direction="inbound", state="ready", duration=12):
        message = self.channel._contact_center_post(
            origin="inbound",
            body="Synthetic audio fixture",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_id=self.agent.partner_id.id,
            partner_ids=[],
        )
        binding = self.env["contact.center.message.binding"].create(
            {
                "message_id": message.id,
                "channel_binding_id": self.binding.id,
                "provider_connection_id": self.connection.id,
                "direction": direction,
                "origin": "provider" if direction == "inbound" else "agent",
                "external_message_id": str(uuid.uuid4()),
            }
        )
        attachment = (
            self.env["ir.attachment"]
            .with_context(contact_center_post_token=CONTACT_CENTER_POST_TOKEN)
            .create(
                {
                    "name": "fixture.ogg",
                    "raw": b"OggS-synthetic-voice-fixture",
                    "res_model": "mail.message",
                    "res_id": message.id,
                    "mimetype": "audio/ogg",
                }
            )
        )
        return (
            self.env["contact.center.media.binding"]
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "message_binding_id": binding.id,
                    "kind": kind,
                    "attachment_id": attachment.id,
                    "file_name": "fixture.ogg",
                    "mime_type": "audio/ogg",
                    "size_bytes": len(attachment.raw),
                    "duration_seconds": duration,
                    "state": state,
                }
            )
        )

    def _api(self, user=None):
        return self.env["contact.center.ui.api"].with_user(user or self.agent)

    def _request(self, media):
        return self._api().request_transcription(media.id)

    def _job(self, media):
        return (
            self.env["queue.job"]
            .sudo()
            .search([("uuid", "=", media.transcription_queue_job_uuid)], limit=1)
        )

    def _perform(self, media):
        return media.with_context(
            job_uuid=media.transcription_queue_job_uuid
        )._job_transcribe()

    def _redact(self, media):
        mutation = self.env["contact.center.message.mutation"].create(
            {
                "target_message_binding_id": media.message_binding_id.id,
                "provider_connection_id": self.connection.id,
                "mutation_type": "delete",
                "direction": "inbound",
                "external_event_id": str(uuid.uuid4()),
            }
        )
        mutation._apply_delete()

    def test_default_disabled_prevents_automatic_and_manual_upload(self):
        self.assertEqual(self.account.transcription_mode, "disabled")
        media = self._media()
        self.assertEqual(media.transcription_state, "idle")
        self.assertFalse(media.transcription_queue_job_uuid)
        with self.assertRaises(ValidationError):
            self._request(media)
        self.transcribe.assert_not_called()

    def test_manual_request_queues_without_io_and_is_idempotent(self):
        self._enable()
        media = self._media()
        self.assertEqual(media.transcription_state, "idle")
        result = self._request(media)
        self.assertEqual(result["state"], "pending")
        self.assertEqual(media.transcription_requested_by_id, self.agent)
        job = self._job(media)
        self.assertTrue(job)
        self.assertEqual(job.identity_key, media._transcription_identity())
        self.assertEqual(job.max_retries, 3)
        self._request(media)
        self.assertEqual(self._job(media), job)
        self.assertEqual(
            self.env["queue.job"].search_count(
                [("identity_key", "=", job.identity_key)]
            ),
            1,
        )
        self.transcribe.assert_not_called()

    def test_interrupted_pending_job_exposes_manual_recovery(self):
        self._enable()
        media = self._media()
        self._request(media)
        original_uuid = media.transcription_queue_job_uuid
        interrupted = Job.load(self.env, original_uuid)
        interrupted.set_done()
        interrupted.store()
        descriptor = media.with_user(self.agent)._transcription_descriptor()
        self.assertEqual(descriptor["state"], "failed")
        self.assertTrue(descriptor["can_request"])
        self.assertFalse(descriptor["text"])
        self._request(media)
        self.assertNotEqual(media.transcription_queue_job_uuid, original_uuid)
        self.assertEqual(media.transcription_state, "pending")
        self.transcribe.assert_not_called()

    def test_automatic_only_inbound_ready_audio_and_on_download_transition(self):
        self._enable("automatic")
        ready = self._media()
        self.assertEqual(ready.transcription_state, "pending")
        for media in [
            self._media(kind="image"),
            self._media(direction="outbound"),
            self._media(state="pending"),
        ]:
            self.assertEqual(media.transcription_state, "idle")
            self.assertFalse(media.transcription_queue_job_uuid)
        pending = self._media(state="pending")
        pending.write({"state": "ready"})
        self.assertEqual(pending.transcription_state, "pending")
        self.transcribe.assert_not_called()

    def test_completion_is_plain_text_and_does_not_change_message_body(self):
        self._enable()
        media = self._media()
        original_body = media.message_binding_id.message_id.body
        literal = '<script>alert("text")</script> & 220 volts'
        self.transcribe.return_value = TranscriptionResult(literal, "pt")
        self._request(media)
        self.assertTrue(self._perform(media))
        self.assertEqual(media.transcription_state, "done")
        self.assertEqual(media.transcription_text, literal)
        self.assertEqual(media.message_binding_id.message_id.body, original_body)
        projection = self._api()._serialize_message(
            media.message_binding_id.message_id, media.message_binding_id
        )
        self.assertEqual(projection["transcriptions"][0]["text"], literal)
        self.assertFalse(projection["transcriptions"][0]["can_request"])
        self._request(media)
        self.assertTrue(self._perform(media))
        self.transcribe.assert_called_once()

    def test_queued_provider_snapshot_survives_model_and_language_edits(self):
        self._enable()
        media = self._media()
        self._request(media)
        self.provider.write(
            {
                "model": "gpt-4o-mini-transcribe",
                "language": "en",
                "prompt": "Changed context",
            }
        )
        self._perform(media)
        snapshot, request = self.transcribe.call_args.args
        self.assertEqual(snapshot["backend"], "openai")
        self.assertEqual(snapshot["model"], "gpt-transcribe")
        self.assertEqual(snapshot["base_url"], "")
        self.assertEqual(request.language, "pt")
        self.assertEqual(request.prompt, "Motores de 220 volts.")
        self.assertNotIn("api_key", snapshot)
        self.assertEqual(media.transcription_model, "gpt-transcribe")

    def test_changing_endpoint_on_same_provider_cancels_queued_upload(self):
        self._enable()
        media = self._media()
        self._request(media)
        self.provider.write(
            {
                "model": "large-v3",
                "backend": "openai_compatible",
                "base_url": "http://192.0.2.55:8000/v1",
            }
        )
        self.assertFalse(self._perform(media))
        self.assertEqual(media.transcription_state, "skipped")
        self.assertEqual(media.transcription_error_code, "routing_changed")
        self.transcribe.assert_not_called()

    def test_changing_credentials_on_same_provider_cancels_queued_upload(self):
        self._enable()
        media = self._media()
        self._request(media)
        self.provider.write({"api_key": "replacement-fixture-key"})
        self.assertFalse(self._perform(media))
        self.assertEqual(media.transcription_state, "skipped")
        self.assertEqual(media.transcription_error_code, "routing_changed")
        self.transcribe.assert_not_called()

    def test_swapping_inbox_provider_before_job_skips_upload(self):
        self._enable()
        media = self._media()
        self._request(media)
        replacement = self.provider.copy(
            {"name": "Other synthetic provider", "api_key": "fixture-key"}
        )
        self.account.transcription_provider_id = replacement
        self.assertFalse(self._perform(media))
        self.assertEqual(media.transcription_state, "skipped")
        self.transcribe.assert_not_called()

    def test_stale_or_missing_job_uuid_cannot_transcribe(self):
        self._enable()
        media = self._media()
        self._request(media)
        self.assertFalse(media._job_transcribe())
        self.assertFalse(
            media.with_context(job_uuid=str(uuid.uuid4()))._job_transcribe()
        )
        self.transcribe.assert_not_called()

    def test_disabled_or_archived_provider_before_job_skips_upload(self):
        self._enable()
        media = self._media()
        self._request(media)
        self.provider.active = False
        self.assertFalse(self._perform(media))
        self.assertEqual(media.transcription_state, "skipped")
        self.transcribe.assert_not_called()

    def test_duration_limit_skips_upload(self):
        self._enable()
        media = self._media(duration=self.provider.max_duration_seconds + 1)
        self._request(media)
        self.assertFalse(self._perform(media))
        self.assertEqual(media.transcription_error_code, "duration_limit")
        self.transcribe.assert_not_called()

    def test_permanent_failure_is_safe_and_manual_retry_gets_new_job(self):
        self._enable()
        media = self._media()
        self._request(media)
        original_uuid = media.transcription_queue_job_uuid
        self.transcribe.side_effect = TranscriptionError("invalid_credentials")
        self.assertFalse(self._perform(media))
        self.assertEqual(media.transcription_state, "failed")
        self.assertEqual(media.transcription_error_code, "invalid_credentials")
        self.assertFalse(media.transcription_text)
        job = Job.load(self.env, original_uuid)
        job.set_done()
        job.store()
        self._request(media)
        self.assertNotEqual(media.transcription_queue_job_uuid, original_uuid)
        self.assertEqual(media.transcription_state, "pending")

    def test_transient_error_retries_then_stops_at_attempt_ceiling(self):
        self._enable()
        media = self._media()
        self._request(media)
        self.transcribe.side_effect = TranscriptionError(
            "rate_limited", retryable=True, retry_after_seconds=17
        )
        self._job(media).retry = 1
        with self.assertRaises(RetryableJobError) as caught:
            self._perform(media)
        self.assertEqual(caught.exception.seconds, 17)
        self.assertEqual(media.transcription_state, "pending")
        self._job(media).retry = 3
        self.assertFalse(self._perform(media))
        self.assertEqual(media.transcription_state, "failed")
        self.assertEqual(media.transcription_error_code, "rate_limited")

    def test_unexpected_provider_exception_does_not_persist_private_details(self):
        self._enable()
        media = self._media()
        self._request(media)
        self.transcribe.side_effect = RuntimeError(
            "private-transcript fixture-key http://private.example"
        )
        self.assertFalse(self._perform(media))
        self.assertEqual(media.transcription_error_code, "internal_error")
        self.assertFalse(media.transcription_text)

    def test_empty_result_is_failure_not_success(self):
        self._enable()
        media = self._media()
        self._request(media)
        self.transcribe.return_value = TranscriptionResult("  ")
        self.assertFalse(self._perform(media))
        self.assertEqual(media.transcription_state, "failed")

    def test_outsider_cannot_request_or_read_transcription(self):
        self._enable()
        media = self._media()
        self.assertNotIn(
            self.outsider.partner_id, self.channel.channel_member_ids.partner_id
        )
        with self.assertRaises(AccessError):
            self._api(self.outsider).request_transcription(media.id)
        with self.assertRaises(AccessError):
            media.with_user(self.outsider).read(["transcription_text"])
        self.transcribe.assert_not_called()

    def test_supervisor_cannot_configure_or_read_provider_secrets(self):
        self._enable()
        with self.assertRaises(AccessError):
            self.account.with_user(self.supervisor).write(
                {"transcription_mode": "automatic"}
            )
        with self.assertRaises(AccessError):
            self.provider.with_user(self.supervisor).read(["api_key"])
        with self.assertRaises(AccessError):
            self.provider.with_user(self.supervisor).write({"model": "large-v3"})
        self.account.with_user(self.admin).write({"transcription_mode": "automatic"})
        self.assertEqual(self.account.transcription_mode, "automatic")

    def test_cross_company_provider_is_rejected(self):
        other_company = self.env["res.company"].create(
            {"name": "Other transcription company"}
        )
        other_provider = self.provider.copy(
            {"company_id": other_company.id, "api_key": "fixture-key"}
        )
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self._enable(provider=other_provider)
        with self.assertRaises(AccessError):
            other_provider.with_user(self.admin).read(["model"])

    def test_provider_in_use_cannot_move_to_another_company(self):
        self._enable()
        other_company = self.env["res.company"].create(
            {"name": "Another speech provider company"}
        )
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.provider.write({"company_id": other_company.id})
        self.assertEqual(self.provider.company_id, self.account.company_id)

    def test_provider_used_by_archived_account_cannot_change_company(self):
        self._enable()
        self.account.write({"active": False})
        other_company = self.env["res.company"].create(
            {"name": "Archived inbox provider company"}
        )
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.provider.write({"company_id": other_company.id})
        self.assertEqual(self.provider.company_id, self.account.company_id)

    def test_diarization_provider_rejects_unsupported_prompt_configuration(self):
        provider_model = self.env["contact.center.transcription.provider"]
        for backend, base_url in [
            ("openai", ""),
            ("openai_compatible", "http://192.0.2.55:8000/v1"),
        ]:
            with self.subTest(backend=backend):
                values = {
                    "name": "Diarization configuration fixture",
                    "backend": backend,
                    "base_url": base_url,
                    "model": "gpt-4o-transcribe-diarize",
                    "api_key": "fixture-key",
                    "prompt": "Unsupported transcription context",
                }
                with self.assertRaises(ValidationError), self.cr.savepoint():
                    provider_model.create(values)
                compatible = provider_model.create(dict(values, prompt=False))
                self.assertEqual(compatible.model, "gpt-4o-transcribe-diarize")
        self.transcribe.assert_not_called()

    def test_supervisor_cannot_enable_transcription_through_create_defaults(self):
        account_model = self.env["contact.center.account"].with_user(self.supervisor)
        with self.assertRaises(AccessError), self.cr.savepoint():
            account_model.with_context(
                default_transcription_mode="automatic",
                default_transcription_provider_id=self.provider.id,
            ).create(
                {
                    "name": "Untrusted transcription defaults",
                    "company_id": self.env.company.id,
                    "platform": "whatsapp",
                    "access_user_ids": [(6, 0, self.supervisor.ids)],
                }
            )

    def test_media_create_rejects_private_result_from_context_defaults(self):
        media = self._media()
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env["contact.center.media.binding"].sudo().with_context(
                default_transcription_text="Forged transcript",
                default_transcription_state="done",
                contact_center_skip_enqueue=True,
            ).create(
                {
                    "message_binding_id": media.message_binding_id.id,
                    "sequence": 2,
                    "kind": "audio",
                    "state": "ready",
                }
            )

    def test_provider_routing_revision_rejects_explicit_and_default_override(self):
        provider_model = self.env["contact.center.transcription.provider"]
        values = {"name": "Forged revision", "api_key": "fixture-key"}
        with self.assertRaises(AccessError), self.cr.savepoint():
            provider_model.create(dict(values, routing_revision=42))
        with self.assertRaises(AccessError), self.cr.savepoint():
            provider_model.with_context(default_routing_revision=42).create(values)

    def test_internal_fields_reject_admin_and_forged_context_writes(self):
        media = self._media()
        for actor in [
            media,
            media.with_user(self.admin),
            media.sudo().with_context(contact_center_transcription_token="forged"),
        ]:
            with self.assertRaises(AccessError):
                actor.write(
                    {"transcription_state": "done", "transcription_text": "forged text"}
                )
        with self.assertRaises(AccessError):
            media.sudo().write(
                {"transcription_config_json": {"base_url": "http://wrong.example"}}
            )
        with self.assertRaises(AccessError):
            media.sudo().write({"transcription_queue_job_uuid": "forged"})

    def test_redaction_purges_text_and_prevents_late_restoration(self):
        self._enable()
        media = self._media()
        self._request(media)
        self._perform(media)
        self._redact(media)
        self.assertEqual(media.message_binding_id.deleted_display_mode, "redact")
        self.assertFalse(media.transcription_text)
        self.assertFalse(media.transcription_config_json)
        self.assertEqual(media.transcription_state, "skipped")
        self.assertFalse(self._perform(media))
        projection = self._api()._serialize_message(
            media.message_binding_id.message_id, media.message_binding_id
        )
        self.assertEqual(projection["transcriptions"], [])

    def test_redaction_during_provider_call_discards_returned_transcript(self):
        self._enable()
        media = self._media()
        self._request(media)

        def redact_during_transcription(snapshot, request):
            self._redact(media)
            return TranscriptionResult(
                "Content removed while provider was running", "pt"
            )

        self.transcribe.side_effect = redact_during_transcription
        self.assertFalse(self._perform(media))
        self.assertEqual(media.transcription_state, "skipped")
        self.assertEqual(media.transcription_error_code, "deleted")
        self.assertFalse(media.transcription_text)

    def test_retention_collects_current_and_historical_transcription_jobs(self):
        self._enable()
        media = self._media()
        self._request(media)
        first = self._job(media)
        stored = Job.load(self.env, first.uuid)
        stored.set_done()
        stored.store()
        media._finish_transcription("failed", error="provider_error")
        self._request(media)
        second = self._job(media)
        self.assertNotEqual(first, second)
        collected = self.env["contact.center.retention"]._jobs(
            [media], media.message_binding_id.message_id
        )
        self.assertTrue(first in collected)
        self.assertTrue(second in collected)
