from psycopg2.errors import SerializationFailure

# Keep independent feature extensions in their own source files.
# pylint: disable=consider-merging-classes-inherited
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.queue_job.exception import RetryableJobError

from ..services.job import (
    ACTIVE_QUEUE_JOB_STATES,
    canonical_queue_job,
    queue_job_owns_record,
)
from ..services.transcription import (
    MAX_AUDIO_BYTES,
    MAX_TEXT_CHARS,
    TranscriptionError,
    TranscriptionRequest,
    TranscriptionResult,
)

_INTERNAL_TOKEN = object()
_CONTEXT_KEY = "contact_center_transcription_token"
_FIELDS = {
    "transcription_state",
    "transcription_text",
    "transcription_provider_id",
    "transcription_model",
    "transcription_language",
    "transcription_queue_job_uuid",
    "transcription_config_json",
    "transcription_requested_at",
    "transcription_completed_at",
    "transcription_requested_by_id",
    "transcription_error_code",
}
_MAX_ATTEMPTS = 3


class ContactCenterMediaBinding(models.Model):
    _inherit = "contact.center.media.binding"

    transcription_state = fields.Selection(
        [
            ("idle", "Not Requested"),
            ("pending", "Queued"),
            ("done", "Transcribed"),
            ("failed", "Failed"),
            ("skipped", "Skipped"),
        ],
        default="idle",
        required=True,
        readonly=True,
        copy=False,
        index=True,
    )
    transcription_text = fields.Text(readonly=True, copy=False)
    transcription_provider_id = fields.Many2one(
        "contact.center.transcription.provider",
        readonly=True,
        copy=False,
        ondelete="set null",
        groups="contact_center_base.group_contact_center_admin",
    )
    transcription_model = fields.Char(readonly=True, copy=False)
    transcription_language = fields.Char(readonly=True, copy=False)
    transcription_queue_job_uuid = fields.Char(readonly=True, copy=False, index=True)
    transcription_config_json = fields.Json(
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    transcription_requested_at = fields.Datetime(readonly=True, copy=False)
    transcription_completed_at = fields.Datetime(readonly=True, copy=False)
    transcription_requested_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False
    )
    transcription_error_code = fields.Char(readonly=True, copy=False)

    def _transcription_internal(self):
        return self.sudo().with_context(**{_CONTEXT_KEY: _INTERNAL_TOKEN})

    @api.model_create_multi
    def create(self, vals_list):
        if any(_FIELDS.intersection(values) for values in vals_list) or any(
            "default_" + name in self.env.context for name in _FIELDS
        ):
            self._check_transcription_write()
        records = super().create(vals_list)
        records._maybe_enqueue_transcription()
        return records

    def write(self, values):
        if _FIELDS.intersection(values):
            self._check_transcription_write()
        result = super().write(values)
        if values.get("state") == "ready":
            self._maybe_enqueue_transcription()
        return result

    def _check_transcription_write(self):
        if not self.env.su or self.env.context.get(_CONTEXT_KEY) is not _INTERNAL_TOKEN:
            raise AccessError(
                _("Transcription results are managed by the speech service.")
            )

    def _transcription_identity(self):
        self.ensure_one()
        return "contact_center:transcription:%s" % self.id

    def _transcription_mode(self):
        self.ensure_one()
        media = self.sudo()
        return media.account_id._transcription_mode_for_conversation(
            media.message_binding_id.channel_binding_id.conversation_type
        )

    def _transcription_eligible(self):
        self.ensure_one()
        media = self.sudo()
        provider = media.account_id.transcription_provider_id
        return bool(
            media.kind == "audio"
            and media.state == "ready"
            and media.attachment_id
            and media.message_binding_id.direction == "inbound"
            and media.message_binding_id.message_state != "deleted"
            and media.account_id.active
            and media._transcription_mode() != "disabled"
            and provider
            and provider.active
            and provider.company_id == media.company_id
        )

    def _transcription_descriptor(self):
        self.ensure_one()
        result = {
            "media_id": self.id,
            "state": "idle",
            "text": "",
            "can_request": False,
            "error_code": "",
        }
        if self._deleted_content_is_hidden():
            return result
        state = self.transcription_state
        error_code = self.transcription_error_code or ""
        if state == "pending" and not canonical_queue_job(
            self,
            self._transcription_identity(),
            ACTIVE_QUEUE_JOB_STATES,
            uuid_field="transcription_queue_job_uuid",
            adopt=False,
        ):
            # A cancelled/failed/lost queue job must not leave an endless spinner.
            # Projection is read-only; a retry takes the normal locked request path.
            state, error_code = "failed", "job_unavailable"
        result.update(
            {
                "state": state,
                "text": self.transcription_text or ""
                if self.transcription_state == "done"
                else "",
                "can_request": state in ("idle", "failed", "skipped")
                and self._transcription_eligible(),
                "error_code": error_code,
            }
        )
        return result

    def action_request_transcription(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        self.env["contact.center.ui.api"]._authorized_channel(
            self.message_binding_id.channel_binding_id.channel_id.id
        )
        if not self._transcription_eligible():
            raise ValidationError(_("Transcription is unavailable for this audio."))
        return self._enqueue_transcription(requested_by=self.env.user.id)

    def _maybe_enqueue_transcription(self):
        for media in self.sudo():
            if (
                media.transcription_state == "idle"
                and media._transcription_mode() == "automatic"
                and media._transcription_eligible()
            ):
                media._enqueue_transcription()

    def _enqueue_transcription(self, requested_by=False):
        self.ensure_one()
        media = self._transcription_internal()
        # Consistent order with deletion and media download: binding, then media.
        media._deleted_content_is_hidden(lock=True)
        self.env.cr.execute(
            "SELECT id FROM contact_center_media_binding WHERE id = %s FOR UPDATE",
            [self.id],
        )
        media.invalidate_recordset()
        if not media._transcription_eligible():
            return False
        if media.transcription_state == "done":
            return True
        if canonical_queue_job(
            media,
            media._transcription_identity(),
            ACTIVE_QUEUE_JOB_STATES,
            uuid_field="transcription_queue_job_uuid",
        ):
            return True
        provider = media.account_id.transcription_provider_id
        snapshot = provider._configuration_snapshot()
        media.write(
            {
                "transcription_state": "pending",
                "transcription_text": False,
                "transcription_provider_id": provider.id,
                "transcription_model": snapshot["model"],
                "transcription_language": snapshot["language"],
                "transcription_config_json": snapshot,
                "transcription_requested_at": fields.Datetime.now(),
                "transcription_requested_by_id": requested_by,
                "transcription_completed_at": False,
                "transcription_error_code": False,
            }
        )
        delayed = (
            media.with_company(media.company_id)
            .with_delay(
                identity_key=media._transcription_identity(),
                max_retries=_MAX_ATTEMPTS,
                description="Contact Center transcription %s" % media.id,
            )
            ._job_transcribe()
        )
        media.write({"transcription_queue_job_uuid": delayed.uuid})
        media._notify_transcription()
        return True

    def _notify_transcription(self):
        self.ensure_one()
        self.env["contact.center.application"]._notify_ui(
            self.message_binding_id.channel_binding_id.channel_id,
            "message_updated",
            {"message_id": self.message_binding_id.message_id.id},
        )

    def _finish_transcription(self, state, *, text=False, language=False, error=False):
        self._transcription_internal().write(
            {
                "transcription_state": state,
                "transcription_text": text,
                "transcription_language": language,
                "transcription_completed_at": fields.Datetime.now(),
                "transcription_error_code": error,
            }
        )
        self._notify_transcription()
        return state == "done"

    def _job_transcribe(self):
        self.ensure_one()
        media = self._transcription_internal().exists()
        if not media or not queue_job_owns_record(
            media, "transcription_queue_job_uuid"
        ):
            return False
        if media.transcription_state != "pending":
            return media.transcription_state == "done"
        if (
            not media._transcription_eligible()
            or (
                media.transcription_provider_id
                != media.account_id.transcription_provider_id
            )
            or (
                media._transcription_mode() != "automatic"
                and not media.transcription_requested_by_id
            )
        ):
            return media._finish_transcription("skipped", error="unavailable")
        snapshot = media.transcription_config_json
        if (
            snapshot.get("routing_revision")
            != media.transcription_provider_id.routing_revision
        ):
            return media._finish_transcription("skipped", error="routing_changed")
        if media.duration_seconds > snapshot["max_duration_seconds"]:
            return media._finish_transcription("skipped", error="duration_limit")
        if (
            media.size_bytes > MAX_AUDIO_BYTES
            or media.attachment_id.file_size > MAX_AUDIO_BYTES
        ):
            return media._finish_transcription("skipped", error="size_limit")
        content = media.attachment_id.raw
        request = TranscriptionRequest(
            content=content,
            filename=media.file_name or "audio",
            mime_type=media.mime_type or "",
            language=snapshot["language"],
            prompt=snapshot["prompt"],
        )
        try:
            result = media.transcription_provider_id._transcribe(snapshot, request)
            if (
                not isinstance(result, TranscriptionResult)
                or not isinstance(result.text, str)
                or not result.text.strip()
                or len(result.text) > MAX_TEXT_CHARS
                or "\x00" in result.text
                or any(0xD800 <= ord(char) <= 0xDFFF for char in result.text)
                or not isinstance(result.language, str)
                or len(result.language) > 16
            ):
                raise TranscriptionError("invalid_response")
            # Refresh the deletion fence after external I/O before saving derived content.
            if media._deleted_content_is_hidden(lock=True):
                return media._finish_transcription("skipped", error="deleted")
            self.env.cr.execute(
                "SELECT id FROM contact_center_media_binding WHERE id = %s FOR UPDATE",
                [media.id],
            )
            return media._finish_transcription(
                "done", text=result.text, language=result.language or False
            )
        except SerializationFailure as error:
            raise RetryableJobError(
                "Transcription state changed.", ignore_retry=True
            ) from error
        except TranscriptionError as error:
            job = (
                self.env["queue.job"]
                .sudo()
                .search([("uuid", "=", self.env.context.get("job_uuid"))], limit=1)
            )
            if error.retryable and (job.retry or 0) < _MAX_ATTEMPTS:
                raise RetryableJobError(
                    "Speech service temporarily unavailable.",
                    seconds=error.retry_after_seconds or None,
                ) from None
            return media._finish_transcription("failed", error=error.code)
        except Exception:
            # Provider exceptions may contain URLs, credentials or customer speech.
            return media._finish_transcription("failed", error="internal_error")


class ContactCenterMessageMutation(models.Model):
    _inherit = "contact.center.message.mutation"

    def _purge_redacted_operational_content(self, target):
        result = super()._purge_redacted_operational_content(target)
        target.media_ids._transcription_internal().write(
            {
                "transcription_text": False,
                "transcription_state": "skipped",
                "transcription_language": False,
                "transcription_config_json": False,
                "transcription_error_code": "deleted",
            }
        )
        return result
