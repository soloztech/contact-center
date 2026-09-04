import datetime
import hashlib
import logging
import uuid

from psycopg2.errors import SerializationFailure

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.queue_job.exception import RetryableJobError

from ..services.adapter import (
    AdapterError,
    ProviderPausedError,
    TransientAdapterError,
    conversation_capabilities,
)
from ..services.dto import MediaDownloadResult, MediaDTO
from ..services.job import (
    ACTIVE_QUEUE_JOB_STATES,
    QUEUE_ATTEMPT_CEILING,
    canonical_queue_job,
    provider_paused_retry_seconds,
    queue_job_owns_record,
)
from ..services.media import (
    canonical_recorded_audio_duration_seconds,
    validate_media_metadata,
    validate_provider_media_capability,
    validate_provider_recorded_audio_capability,
)
from ..services.tokens import CONTACT_CENTER_POST_TOKEN

_MEDIA_UPLOAD_CLEANUP_BATCH = 500
_logger = logging.getLogger(__name__)


class ContactCenterMediaBinding(models.Model):
    _name = "contact.center.media.binding"
    _description = "Contact Center Message Media"
    _order = "message_binding_id, sequence, id"

    message_binding_id = fields.Many2one(
        "contact.center.message.binding", required=True, index=True, ondelete="cascade"
    )
    account_id = fields.Many2one(
        related="message_binding_id.account_id", store=True, readonly=True, index=True
    )
    company_id = fields.Many2one(
        related="message_binding_id.company_id", store=True, readonly=True, index=True
    )
    provider_connection_id = fields.Many2one(
        related="message_binding_id.provider_connection_id",
        store=True,
        readonly=True,
        index=True,
    )
    attachment_id = fields.Many2one(
        "ir.attachment", index=True, ondelete="set null", copy=False
    )
    sequence = fields.Integer(default=1, required=True)
    kind = fields.Selection(
        [
            ("image", "Image"),
            ("audio", "Audio"),
            ("video", "Video"),
            ("document", "Document"),
        ],
        required=True,
        index=True,
    )
    external_media_id = fields.Char(index=True, copy=False)
    remote_locator_json = fields.Json(
        default=dict,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    mime_type = fields.Char(index=True)
    file_name = fields.Char()
    size_bytes = fields.Integer(default=0)
    sha256 = fields.Char(copy=False)
    is_voice_note = fields.Boolean(default=False)
    duration_seconds = fields.Integer(default=0)
    width = fields.Integer(default=0)
    height = fields.Integer(default=0)
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("downloading", "Downloading"),
            ("ready", "Ready"),
            ("failed", "Failed"),
            ("discarded", "Discarded by Deletion Policy"),
        ],
        default="pending",
        required=True,
        index=True,
        copy=False,
    )
    attempts = fields.Integer(default=0, required=True, copy=False)
    queue_job_uuid = fields.Char(index=True, readonly=True, copy=False)
    downloaded_at = fields.Datetime(copy=False)
    last_error_class = fields.Char(copy=False)
    last_error_message = fields.Text(copy=False)

    _sql_constraints = [
        (
            "message_sequence_unique",
            "unique(message_binding_id, sequence)",
            "A message media sequence must be unique.",
        ),
        (
            "attempts_nonnegative",
            "check(attempts >= 0)",
            "Attempts cannot be negative.",
        ),
        (
            "size_nonnegative",
            "check(size_bytes >= 0)",
            "Media size cannot be negative.",
        ),
        (
            "voice_note_is_audio",
            "check(NOT is_voice_note OR kind = 'audio')",
            "A voice note must be audio media.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.context.get("contact_center_skip_enqueue"):
            records.filtered(lambda media: media.state == "pending")._enqueue_download()
        return records

    def _has_active_queue_job(self):
        self.ensure_one()
        return bool(
            canonical_queue_job(
                self,
                "contact_center:media:%s" % self.id,
                ACTIVE_QUEUE_JOB_STATES,
            )
        )

    def _enqueue_download(self, eta=None):
        for media in self.filtered(lambda item: item.state == "pending"):
            if media._has_active_queue_job():
                continue
            delayed = (
                media.sudo()
                .with_company(media.company_id)
                .with_delay(
                    identity_key="contact_center:media:%s" % media.id,
                    max_retries=0,
                    description="Contact Center media %s" % media.id,
                    eta=eta,
                )
                ._job_download()
            )
            media.sudo().write({"queue_job_uuid": delayed.uuid})
        return True

    def action_retry_download(self):
        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        ):
            raise AccessError(_("Only supervisors can retry media downloads."))
        self.check_access_rights("read")
        self.check_access_rule("read")
        failed_media = self.filtered(lambda media: media.state == "failed")
        failed_media.sudo().write(
            {
                "state": "pending",
                "attempts": 0,
                "queue_job_uuid": False,
                "last_error_class": False,
                "last_error_message": False,
            }
        )
        failed_media.invalidate_recordset(
            [
                "state",
                "attempts",
                "queue_job_uuid",
                "last_error_class",
                "last_error_message",
            ]
        )
        return failed_media._enqueue_download()

    def _as_dto(self):
        self.ensure_one()
        return MediaDTO(
            kind=self.kind,
            external_media_id=self.external_media_id or "",
            remote_locator=self.remote_locator_json or {},
            mime_type=self.mime_type or "",
            file_name=self.file_name or "",
            size_bytes=self.size_bytes or 0,
            sha256=self.sha256 or "",
            is_voice_note=bool(self.is_voice_note),
            duration_seconds=self.duration_seconds or 0,
            width=self.width or 0,
            height=self.height or 0,
        )

    def _job_attempt(self):
        self.ensure_one()
        job_uuid = self.env.context.get("job_uuid")
        if job_uuid:
            job = (
                self.env["queue.job"].sudo().search([("uuid", "=", job_uuid)], limit=1)
            )
            if job:
                return self.attempts + job.retry + 1
        return self.attempts + 1

    def _provider_read_preflight(self):
        """Stop paused media jobs before they cross the provider boundary."""

        self.ensure_one()
        connection = self.provider_connection_id
        connection.invalidate_recordset(
            [
                "active",
                "account_id",
                "state",
                "identity_mismatch_latched",
                "last_state_observed_at",
                "last_health_at",
            ]
        )
        connection.account_id.invalidate_recordset(["active"])
        adapter = connection.get_adapter()
        if not (
            connection.active
            and connection.account_id.active
            and not connection.identity_mismatch_latched
            and adapter.is_provider_read_ready(connection, "media_download") is True
        ):
            raise ProviderPausedError(
                "provider connection is not ready for media download"
            )
        return connection

    def _finalize_provider_locator(self, *, succeeded):
        """Best-effort cleanup for adapter-private, short-lived locator material."""

        self.ensure_one()
        try:
            self.provider_connection_id.get_adapter().finalize_media_download(
                self.provider_connection_id,
                self._as_dto(),
                succeeded=bool(succeeded),
            )
        except Exception:  # pylint: disable=broad-except
            # Attachment state is canonical and must not be reverted merely because
            # a provider-private cleanup hook failed. Providers need a bounded cron
            # fallback for any secret-bearing locator vault.
            _logger.exception(
                "Failed to finalize provider locator for media binding %s",
                self.id,
            )

    def _deleted_content_is_hidden(self, *, lock=False):
        """Return the snapshotted deletion policy, refreshed across job commits."""

        self.ensure_one()
        binding = self.message_binding_id
        if lock:
            # A provider download performs I/O without holding this lock.  Taking
            # it afterwards either observes a stable active row or collides with
            # a delete committed since this REPEATABLE READ transaction started;
            # PostgreSQL then aborts this attempt and queue_job retries safely.
            self.env.cr.execute(
                "SELECT id FROM contact_center_message_binding "
                "WHERE id = %s FOR UPDATE",
                [binding.id],
            )
        binding.invalidate_recordset(["message_state", "deleted_display_mode"])
        return bool(
            binding.message_state == "deleted"
            and binding.deleted_display_mode != "strike"
        )

    def _discard_hidden_deleted_content(self, attempt):
        """Finish a queued download without materializing redacted message media."""

        self.ensure_one()
        self.write(
            {
                "state": "discarded",
                "attempts": attempt,
                "last_error_class": "DeletedMessageContentHidden",
                "last_error_message": "Media discarded by the inbox deletion policy.",
            }
        )
        self._finalize_provider_locator(succeeded=True)
        self.env["contact.center.application"]._notify_ui(
            self.message_binding_id.channel_binding_id.channel_id,
            "message_updated",
            {"message_id": self.message_binding_id.message_id.id},
        )
        return True

    def _job_download(self):
        self.ensure_one()
        if not queue_job_owns_record(self):
            return False
        if self.state == "ready":
            return True
        attempt = self._job_attempt()
        if self._deleted_content_is_hidden():
            return self._discard_hidden_deleted_content(attempt)
        try:
            connection = self._provider_read_preflight()
            result = connection.get_adapter().download_media(connection, self._as_dto())
            if not isinstance(result, MediaDownloadResult):
                raise AdapterError("download_media must return MediaDownloadResult")
            mimetype = validate_media_metadata(
                self.kind, result.mime_type, result.size_bytes
            )
            if self.size_bytes and self.size_bytes != result.size_bytes:
                raise AdapterError(
                    "downloaded media size differs from provider metadata"
                )
            if self.sha256 and self.sha256.lower() != result.sha256.lower():
                raise AdapterError("downloaded media checksum differs from metadata")
            # A delete can be committed while the provider I/O above is in flight.
            # Re-read the binding before persisting bytes so the redaction policy
            # cannot be bypassed by a late queue job.
            if self._deleted_content_is_hidden(lock=True):
                return self._discard_hidden_deleted_content(attempt)
            # Keep the global lock order binding -> media.  This state is local to
            # the transaction anyway, so writing it before provider I/O conveyed
            # no observable progress and could deadlock against delete projection.
            self.write({"state": "downloading", "attempts": attempt})
            attachment = (
                self.env["ir.attachment"]
                .sudo()
                .with_context(image_no_postprocess=True)
                .create(
                    {
                        "name": (result.file_name or self.file_name or self.kind)[:255],
                        "type": "binary",
                        "raw": result.content,
                        "mimetype": mimetype,
                        "res_model": "mail.message",
                        "res_id": self.message_binding_id.message_id.id,
                    }
                )
            )
            self.message_binding_id.message_id.sudo().with_context(
                contact_center_post_token=CONTACT_CENTER_POST_TOKEN
            ).write({"attachment_ids": [(4, attachment.id)]})
            self.write(
                {
                    "attachment_id": attachment.id,
                    "state": "ready",
                    "attempts": attempt,
                    "mime_type": mimetype,
                    "file_name": attachment.name,
                    "size_bytes": result.size_bytes,
                    "sha256": result.sha256,
                    "downloaded_at": fields.Datetime.now(),
                    "last_error_class": False,
                    "last_error_message": False,
                }
            )
            self._finalize_provider_locator(succeeded=True)
        except SerializationFailure as error:
            # The post-I/O row lock intentionally converts a concurrent delete
            # into a transaction retry.  This is not a provider attempt and must
            # never consume the media retry ceiling.
            raise RetryableJobError(
                str(error), seconds=None, ignore_retry=True
            ) from error
        except ProviderPausedError as error:
            raise RetryableJobError(
                str(error),
                seconds=provider_paused_retry_seconds(error, ("media", self.id)),
                ignore_retry=True,
            ) from error
        except TransientAdapterError as error:
            if attempt >= QUEUE_ATTEMPT_CEILING:
                self._finish_failure(error, attempt)
                return False
            raise RetryableJobError(
                str(error),
                seconds=getattr(error, "retry_after_seconds", 0) or None,
            ) from error
        except (AdapterError, ValidationError) as error:
            self._finish_failure(error, attempt)
            return False
        except Exception as error:
            if attempt >= QUEUE_ATTEMPT_CEILING:
                self._finish_failure(error, attempt)
                return False
            raise RetryableJobError(str(error), seconds=None) from error
        self.env["contact.center.application"]._notify_ui(
            self.message_binding_id.channel_binding_id.channel_id,
            "message_updated",
            {"message_id": self.message_binding_id.message_id.id},
        )
        return True

    def _finish_failure(self, error, attempt):
        self.write(
            {
                "state": "failed",
                "attempts": attempt,
                "last_error_class": error.__class__.__name__,
                "last_error_message": str(error)[:4000],
            }
        )
        self._finalize_provider_locator(succeeded=False)
        self.env["contact.center.application"]._notify_ui(
            self.message_binding_id.channel_binding_id.channel_id,
            "message_updated",
            {"message_id": self.message_binding_id.message_id.id},
        )


class ContactCenterMediaUpload(models.Model):
    _name = "contact.center.media.upload"
    _description = "Contact Center Pending Media Upload"
    _order = "create_date desc, id desc"

    reference = fields.Char(
        required=True, default=lambda self: str(uuid.uuid4()), index=True, copy=False
    )
    channel_binding_id = fields.Many2one(
        "contact.center.channel.binding", required=True, index=True, ondelete="cascade"
    )
    account_id = fields.Many2one(
        related="channel_binding_id.account_id", store=True, readonly=True, index=True
    )
    company_id = fields.Many2one(
        related="channel_binding_id.company_id", store=True, readonly=True, index=True
    )
    uploaded_by_user_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="cascade"
    )
    attachment_id = fields.Many2one(
        "ir.attachment", required=True, index=True, ondelete="restrict"
    )
    consumed_message_binding_id = fields.Many2one(
        "contact.center.message.binding", index=True, ondelete="set null"
    )
    kind = fields.Selection(
        [
            ("image", "Image"),
            ("audio", "Audio"),
            ("video", "Video"),
            ("document", "Document"),
        ],
        required=True,
        index=True,
    )
    mime_type = fields.Char(required=True)
    file_name = fields.Char(required=True)
    size_bytes = fields.Integer(required=True)
    sha256 = fields.Char(required=True)
    is_voice_note = fields.Boolean(default=False)
    duration_seconds = fields.Integer(default=0)
    state = fields.Selection(
        [("pending", "Pending"), ("consumed", "Consumed")],
        required=True,
        default="pending",
        index=True,
    )
    expires_at = fields.Datetime(
        required=True,
        default=lambda self: fields.Datetime.now() + datetime.timedelta(hours=24),
        index=True,
    )

    _sql_constraints = [
        (
            "reference_unique",
            "unique(reference)",
            "The upload reference must be unique.",
        ),
        ("size_positive", "check(size_bytes > 0)", "Upload size must be positive."),
        (
            "duration_range",
            "check(duration_seconds >= 0 AND duration_seconds <= 900)",
            "Upload duration must be between zero and fifteen minutes.",
        ),
        (
            "voice_note_is_audio",
            "check(NOT is_voice_note OR kind = 'audio')",
            "A voice note must be an audio upload.",
        ),
        (
            "voice_note_has_duration",
            "check(NOT is_voice_note OR duration_seconds > 0)",
            "A voice note must declare a positive duration.",
        ),
        (
            "duration_is_audio",
            "check(duration_seconds = 0 OR kind = 'audio')",
            "Only audio uploads may declare a duration.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        binding_ids = {
            values.get("channel_binding_id")
            for values in vals_list
            if values.get("channel_binding_id")
        }
        bindings = self.env["contact.center.channel.binding"].browse(list(binding_ids))
        for binding in bindings.exists():
            binding._contact_center_ensure_outbound_supported(
                operation="upload_media", has_media=True
            )
        return super().create(vals_list)

    def _consume(self, message_binding, user=None):
        self.ensure_one()
        message_binding.ensure_one()
        self.channel_binding_id._contact_center_ensure_outbound_supported(
            operation="upload_media", has_media=True
        )
        user = user or self.env.user
        self.env.cr.execute(
            "SELECT id FROM contact_center_media_upload WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset(["state", "expires_at"])
        if self.state != "pending" or self.expires_at < fields.Datetime.now():
            raise ValidationError(_("The media upload is no longer available."))
        if self.uploaded_by_user_id != user:
            raise AccessError(_("The media upload belongs to another user."))
        if self.channel_binding_id != message_binding.channel_binding_id:
            raise AccessError(_("The media upload belongs to another conversation."))
        connection = message_binding.provider_connection_id
        if not connection:
            raise ValidationError(_("The outbound message has no provider connection."))
        if self.channel_binding_id.conversation_type == "group":
            profile = self.channel_binding_id.group_profile_ids[:1]
            if (
                not self.channel_binding_id.account_id.group_outbound_enabled
                or not profile
                or profile.provider_connection_id != connection
                or not connection.active
                or not connection.outbound_active
            ):
                raise ValidationError(
                    _("The group media upload no longer has an active provider route.")
                )
        capabilities = conversation_capabilities(
            connection.capabilities_json or {},
            self.channel_binding_id.conversation_type,
        )
        validate_provider_media_capability(
            capabilities,
            self.kind,
            self.mime_type,
            self.size_bytes,
        )
        canonical_duration = self.duration_seconds or 0
        if self.is_voice_note or self.duration_seconds:
            attachment_content = self.attachment_id.sudo().raw or b""
            if isinstance(attachment_content, memoryview):
                attachment_content = attachment_content.tobytes()
            elif isinstance(attachment_content, bytearray):
                attachment_content = bytes(attachment_content)
            if (
                not isinstance(attachment_content, bytes)
                or len(attachment_content) != self.size_bytes
                or hashlib.sha256(attachment_content).hexdigest() != self.sha256
            ):
                raise ValidationError(_("The recorded audio content is inconsistent."))
            canonical_duration = canonical_recorded_audio_duration_seconds(
                attachment_content, self.mime_type
            )
            if canonical_duration != self.duration_seconds:
                raise ValidationError(
                    _("The recorded audio duration is inconsistent with its container.")
                )
            try:
                connection.get_adapter().validate_recorded_audio_upload(
                    connection,
                    content=attachment_content,
                    mimetype=self.mime_type,
                    is_voice_note=bool(self.is_voice_note),
                    duration_seconds=self.duration_seconds or 0,
                )
            except AdapterError as error:
                raise ValidationError(
                    _("The active provider rejected the recorded audio file.")
                ) from error
        validate_provider_recorded_audio_capability(
            capabilities,
            self.mime_type,
            bool(self.is_voice_note),
            canonical_duration,
        )
        attachment = self.attachment_id.sudo().with_context(
            contact_center_post_token=CONTACT_CENTER_POST_TOKEN
        )
        attachment.write(
            {
                "res_model": "mail.message",
                "res_id": message_binding.message_id.id,
            }
        )
        message_binding.message_id.sudo().with_context(
            contact_center_post_token=CONTACT_CENTER_POST_TOKEN
        ).write({"attachment_ids": [(4, attachment.id)]})
        self.sudo().write(
            {
                "state": "consumed",
                "consumed_message_binding_id": message_binding.id,
                "expires_at": fields.Datetime.now() + datetime.timedelta(days=7),
            }
        )
        return attachment

    def _as_outbound_media_dto(self, attachment):
        self.ensure_one()
        return MediaDTO(
            kind=self.kind,
            external_media_id="",
            remote_locator={
                "attachment_id": attachment.id,
                "upload_ref": self.reference,
            },
            mime_type=self.mime_type,
            file_name=self.file_name,
            size_bytes=self.size_bytes,
            sha256=self.sha256,
            is_voice_note=bool(self.is_voice_note),
            duration_seconds=self.duration_seconds or 0,
        )

    @api.model
    def _cron_cleanup_expired(self):
        expired = self.sudo().search(
            [("expires_at", "<", fields.Datetime.now())],
            order="expires_at, id",
            limit=_MEDIA_UPLOAD_CLEANUP_BATCH,
        )
        pending_attachment_ids = expired.filtered(
            lambda upload: upload.state == "pending"
        ).attachment_id.ids
        expired.unlink()
        # Odoo normally deletes generic attachments whose ``res_model/res_id``
        # points at an unlinked record. Re-browse the captured IDs because that
        # automatic cleanup invalidates the original attachment recordset; trying
        # to unlink it again raises MissingError/CacheMiss. The explicit fallback
        # still cleans a pending attachment if its generic link was inconsistent.
        pending_attachments = (
            self.env["ir.attachment"].sudo().browse(pending_attachment_ids).exists()
        )
        if pending_attachments:
            pending_attachments.with_context(
                contact_center_post_token=CONTACT_CENTER_POST_TOKEN
            ).unlink()
        return True
