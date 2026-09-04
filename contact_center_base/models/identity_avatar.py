import base64
import datetime

from psycopg2 import OperationalError

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.mimetypes import guess_mimetype

from odoo.addons.queue_job.exception import RetryableJobError

from ..services.adapter import (
    AdapterError,
    ProviderPausedError,
    TransientAdapterError,
    UnsupportedEventError,
)
from ..services.dto import AddressDTO, DTOValidationError, IdentityProfileResult
from ..services.job import (
    ACTIVE_QUEUE_JOB_STATES,
    QUEUE_ATTEMPT_CEILING,
    canonical_queue_job,
    provider_paused_retry_seconds,
    queue_job_owns_record,
)

_ACTIVE_QUEUE_JOB_STATES = ACTIVE_QUEUE_JOB_STATES
_IDENTITY_AVATAR_MAX_BYTES = 2 * 1024 * 1024
_IDENTITY_AVATAR_MIMETYPES = frozenset(("image/jpeg", "image/png", "image/webp"))
_IDENTITY_AVATAR_TTL = datetime.timedelta(hours=24)
_IDENTITY_AVATAR_JOB_PRIORITY = 60
_IDENTITY_AVATAR_NAMESPACE_PRIORITY = {
    "whatsapp.pn": 0,
    "whatsapp.jid": 1,
    "phone": 2,
    "whatsapp.lid": 3,
}

# Keep the independently versioned avatar workflow isolated from group metadata.
# pylint: disable=consider-merging-classes-inherited


class ContactCenterChannelBinding(models.Model):
    """Provider-neutral, account-scoped avatar projection for direct bindings."""

    _inherit = "contact.center.channel.binding"

    direct_avatar_state = fields.Selection(
        [
            ("unavailable", "Unavailable"),
            ("pending", "Pending"),
            ("ready", "Ready"),
            ("absent", "Absent"),
            ("failed", "Failed"),
        ],
        required=True,
        default="unavailable",
        index=True,
        copy=False,
    )
    direct_avatar_attachment_id = fields.Many2one(
        "ir.attachment",
        copy=False,
        ondelete="set null",
        groups="contact_center_base.group_contact_center_admin",
    )
    direct_avatar_sha256 = fields.Char(
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    direct_avatar_provider_revision = fields.Char(
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    direct_avatar_connection_id = fields.Many2one(
        "contact.center.provider.connection",
        copy=False,
        index=True,
        ondelete="set null",
        check_company=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    direct_avatar_last_synced_at = fields.Datetime(copy=False, index=True)
    direct_avatar_next_sync_at = fields.Datetime(copy=False, index=True)
    direct_avatar_sync_requested_at = fields.Datetime(copy=False)
    direct_avatar_sync_revision = fields.Integer(default=0, required=True, copy=False)
    direct_avatar_applied_revision = fields.Integer(
        default=0, required=True, copy=False
    )
    direct_avatar_attempts = fields.Integer(default=0, required=True, copy=False)
    direct_avatar_queue_job_uuid = fields.Char(
        index=True,
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    direct_avatar_last_error_class = fields.Char(
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    direct_avatar_last_error_message = fields.Text(
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )

    def unlink(self):
        attachments = self.sudo().mapped("direct_avatar_attachment_id")
        result = super().unlink()
        attachments.exists().sudo().unlink()
        return result

    def _direct_avatar_has_active_job(self, *, adopt=True, require_pointer_match=False):
        self.ensure_one()
        job = canonical_queue_job(
            self,
            "contact_center:identity_avatar:%s:%s"
            % (self.id, self.direct_avatar_sync_revision),
            _ACTIVE_QUEUE_JOB_STATES,
            uuid_field="direct_avatar_queue_job_uuid",
            adopt=adopt,
        )
        return bool(
            job
            and (
                not require_pointer_match
                or self.direct_avatar_queue_job_uuid == job.uuid
            )
        )

    def _direct_avatar_default_connection(self, adapter_key=None):
        self.ensure_one()
        candidates = self.env["contact.center.provider.connection"]
        if self.direct_avatar_connection_id.active:
            candidates |= self.direct_avatar_connection_id
        latest_message = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("channel_binding_id", "=", self.id),
                    ("provider_connection_id", "!=", False),
                ],
                order="id desc",
                limit=1,
            )
        )
        if latest_message.provider_connection_id.active:
            candidates |= latest_message.provider_connection_id
        candidates |= self.account_id.connection_ids.filtered("active")
        candidates = candidates.filtered(
            lambda connection: connection.account_id == self.account_id
            and (not adapter_key or connection.adapter_key == adapter_key)
        )
        return (
            candidates.filtered(lambda item: item.state == "connected")[:1]
            or candidates[:1]
        )

    def _direct_avatar_address(self):
        self.ensure_one()
        if self.conversation_type != "direct" or not self.identity_id:
            raise UnsupportedEventError("identity avatars require a direct binding")

        def profile_order(aliases):
            # Namespace is authoritative; within one namespace use the freshest
            # evidence so a replaced/bridged PN never reads an obsolete profile.
            aliases = aliases.sorted(
                key=lambda alias: (
                    alias.last_seen_at or alias.first_seen_at,
                    alias.id,
                ),
                reverse=True,
            )
            return aliases.sorted(
                key=lambda alias: _IDENTITY_AVATAR_NAMESPACE_PRIORITY.get(
                    alias.namespace, 50
                )
            )

        aliases = self.identity_id.alias_ids.filtered(
            lambda alias: alias.account_id == self.account_id
        )
        aliases = profile_order(aliases)
        if aliases:
            alias = aliases[0]
            return AddressDTO(
                namespace=alias.namespace,
                value=alias.value_raw,
                value_normalized=alias.value_normalized,
                role="primary",
                source_field=alias.source_field or "identity_alias",
                confidence=alias.confidence,
            )
        channel_aliases = profile_order(
            self.alias_ids.filtered(
                lambda alias: alias.account_id == self.account_id
                and alias.role in ("primary", "routing")
            )
        )
        if not channel_aliases:
            raise UnsupportedEventError("direct identity has no avatar lookup address")
        alias = channel_aliases[0]
        return AddressDTO(
            namespace=alias.namespace,
            value=alias.value_raw,
            value_normalized=alias.value_normalized,
            role="primary",
            source_field=alias.source_field or "channel_alias",
            confidence=alias.confidence,
        )

    def _request_identity_avatar_sync(self, connection=None, force=False, eta=None):
        for candidate in self.sudo():
            binding = candidate.exists()
            if (
                not binding
                or binding.conversation_type != "direct"
                or not binding.identity_id
                or not binding.active
                or binding.merged_into_id
            ):
                continue
            target_connection = (
                connection or binding._direct_avatar_default_connection()
            )
            if not target_connection:
                continue
            target_connection.ensure_one()
            if target_connection.account_id != binding.account_id:
                raise ValidationError(
                    _("The identity avatar connection belongs to another account.")
                )
            if not target_connection.get_adapter().supports_identity_profile(
                target_connection
            ):
                continue
            now = fields.Datetime.now()
            due = (
                not binding.direct_avatar_next_sync_at
                or binding.direct_avatar_next_sync_at <= now
            )
            connection_changed = (
                binding.direct_avatar_connection_id != target_connection
            )
            if (
                not force
                and not connection_changed
                and binding._direct_avatar_has_active_job(
                    adopt=False,
                    require_pointer_match=True,
                )
            ):
                continue
            if not (
                force
                or due
                or connection_changed
                or binding.direct_avatar_state in ("unavailable", "failed")
            ):
                continue

            # Most inbound messages observe an avatar that is still inside its
            # TTL.  Avoid locking the hot conversation rows for that no-op.  The
            # same conditions are deliberately checked again after acquiring the
            # canonical channel -> binding locks so this optimization cannot race
            # another worker into creating two revisions/jobs.
            if not binding._contact_center_lock_channel_then_binding():
                continue
            binding.invalidate_recordset()
            if (
                binding.conversation_type != "direct"
                or not binding.identity_id
                or not binding.active
                or binding.merged_into_id
            ):
                continue
            target_connection = (
                connection or binding._direct_avatar_default_connection()
            )
            if not target_connection:
                continue
            target_connection.ensure_one()
            if target_connection.account_id != binding.account_id:
                raise ValidationError(
                    _("The identity avatar connection belongs to another account.")
                )
            if not target_connection.get_adapter().supports_identity_profile(
                target_connection
            ):
                continue
            now = fields.Datetime.now()
            due = (
                not binding.direct_avatar_next_sync_at
                or binding.direct_avatar_next_sync_at <= now
            )
            connection_changed = (
                binding.direct_avatar_connection_id != target_connection
            )
            if (
                not force
                and not connection_changed
                and binding._direct_avatar_has_active_job()
            ):
                continue
            if not (
                force
                or due
                or connection_changed
                or binding.direct_avatar_state in ("unavailable", "failed")
            ):
                continue
            binding.write(
                {
                    "direct_avatar_connection_id": target_connection.id,
                    "direct_avatar_state": (
                        "ready" if binding.direct_avatar_attachment_id else "pending"
                    ),
                    "direct_avatar_sync_requested_at": now,
                    "direct_avatar_sync_revision": (
                        binding.direct_avatar_sync_revision + 1
                    ),
                    "direct_avatar_attempts": 0,
                    "direct_avatar_last_error_class": False,
                    "direct_avatar_last_error_message": False,
                }
            )
            binding.invalidate_recordset(
                [
                    "direct_avatar_sync_revision",
                    "direct_avatar_queue_job_uuid",
                ]
            )
            if (
                not self.env.context.get("contact_center_skip_enqueue")
                and not binding._direct_avatar_has_active_job()
            ):
                binding._enqueue_identity_avatar_sync(
                    binding.direct_avatar_sync_revision, eta=eta
                )
        return True

    def _enqueue_identity_avatar_sync(self, revision=None, eta=None):
        for binding in self.sudo():
            target_revision = (
                revision
                if revision is not None
                else binding.direct_avatar_sync_revision
            )
            binding.invalidate_recordset(["direct_avatar_sync_revision"])
            if target_revision != binding.direct_avatar_sync_revision:
                continue
            identity_key = "contact_center:identity_avatar:%s:%s" % (
                binding.id,
                target_revision,
            )
            if canonical_queue_job(
                binding,
                identity_key,
                _ACTIVE_QUEUE_JOB_STATES,
                uuid_field="direct_avatar_queue_job_uuid",
            ):
                continue
            delayed = (
                binding.with_company(binding.company_id)
                .with_delay(
                    identity_key=identity_key,
                    max_retries=0,
                    priority=_IDENTITY_AVATAR_JOB_PRIORITY,
                    description="Contact Center identity avatar %s" % binding.id,
                    eta=eta,
                )
                ._job_sync_identity_avatar(target_revision)
            )
            binding.write({"direct_avatar_queue_job_uuid": delayed.uuid})
        return True

    def _direct_avatar_job_attempt(self):
        self.ensure_one()
        job_uuid = self.env.context.get("job_uuid")
        if job_uuid:
            job = (
                self.env["queue.job"].sudo().search([("uuid", "=", job_uuid)], limit=1)
            )
            if job:
                return self.direct_avatar_attempts + job.retry + 1
        return self.direct_avatar_attempts + 1

    def _direct_avatar_superseded(
        self, expected_revision, job_uuid, connection_id=None
    ):
        binding = self.exists()
        if not binding:
            return True
        binding.ensure_one()
        binding.invalidate_recordset(
            [
                "direct_avatar_sync_revision",
                "direct_avatar_queue_job_uuid",
                "direct_avatar_connection_id",
            ]
        )
        return bool(
            binding.direct_avatar_sync_revision != expected_revision
            or not binding.direct_avatar_queue_job_uuid
            or (binding.direct_avatar_queue_job_uuid != job_uuid)
            or (
                connection_id is not None
                and binding.direct_avatar_connection_id.id != connection_id
            )
        )

    def _direct_avatar_schedule_latest(self, job_uuid):
        binding = self.exists()
        if not binding:
            return False
        binding.ensure_one()
        if not binding._contact_center_lock_channel_then_binding():
            return False
        binding.invalidate_recordset(
            ["direct_avatar_sync_revision", "direct_avatar_queue_job_uuid"]
        )
        if job_uuid and binding.direct_avatar_queue_job_uuid == job_uuid:
            binding.write({"direct_avatar_queue_job_uuid": False})
            binding._enqueue_identity_avatar_sync(binding.direct_avatar_sync_revision)
        return False

    def _direct_avatar_provider_available(self, connection):
        connection.ensure_one()
        adapter = connection.get_adapter()
        return bool(
            connection.active
            and connection.account_id.active
            and not connection.identity_mismatch_latched
            and adapter.is_provider_read_ready(connection, "identity_profile") is True
        )

    def _direct_avatar_preflight(self, connection):
        self.ensure_one()
        connection.invalidate_recordset()
        self.invalidate_recordset()
        self.channel_id.invalidate_recordset(["active", "channel_type"])
        connection.account_id.invalidate_recordset(["active"])
        if (
            not self.active
            or self.merged_into_id
            or self.conversation_type != "direct"
            or not self.identity_id
            or not self.channel_id.active
            or self.channel_id.channel_type != "contact_center"
            or not connection.active
            or not connection.account_id.active
            or connection.account_id != self.account_id
        ):
            raise UnsupportedEventError("identity avatar scope is no longer active")
        if not connection.get_adapter().supports_identity_profile(connection):
            raise UnsupportedEventError(
                "provider adapter does not support identity profiles"
            )
        if not self._direct_avatar_provider_available(connection):
            raise ProviderPausedError(
                "provider connection is not ready for identity avatar"
            )
        return connection.health_configuration_revision, self._direct_avatar_address()

    def _direct_avatar_lock_for_projection(self, connection):
        self.ensure_one()
        for table, record_id, lock in (
            ("contact_center_account", self.account_id.id, "FOR SHARE"),
            ("contact_center_provider_connection", connection.id, "FOR SHARE"),
        ):
            self.env.cr.execute(
                "SELECT id FROM %s WHERE id = %%s %s" % (table, lock),
                [record_id],
            )
            if not self.env.cr.fetchone():
                return False
        if not self._contact_center_lock_identity_channel_binding():
            raise TransientAdapterError(
                "identity avatar scope changed while acquiring projection locks"
            )
        return True

    def _direct_avatar_validate_projection(self, connection, health_revision):
        self.ensure_one()
        connection.invalidate_recordset()
        self.invalidate_recordset()
        self.channel_id.invalidate_recordset(["active", "channel_type"])
        connection.account_id.invalidate_recordset(["active"])
        if (
            not self.active
            or self.merged_into_id
            or self.conversation_type != "direct"
            or not self.identity_id
            or not self.channel_id.active
            or self.channel_id.channel_type != "contact_center"
            or connection.account_id != self.account_id
        ):
            raise UnsupportedEventError(
                "identity avatar scope changed while reading the provider"
            )
        if (
            connection.health_configuration_revision != health_revision
            or not self._direct_avatar_provider_available(connection)
        ):
            raise TransientAdapterError(
                "provider connection changed while reading identity avatar"
            )

    def _job_sync_identity_avatar(self, expected_revision):
        binding = self.exists()
        if not binding:
            return False
        binding.ensure_one()
        job_uuid = self.env.context.get("job_uuid") or ""
        if not queue_job_owns_record(
            binding, uuid_field="direct_avatar_queue_job_uuid"
        ):
            return False
        if not isinstance(expected_revision, int) or isinstance(
            expected_revision, bool
        ):
            raise ValidationError(_("The identity avatar revision is invalid."))
        if binding._direct_avatar_superseded(expected_revision, job_uuid):
            return binding._direct_avatar_schedule_latest(job_uuid)
        connection = binding.direct_avatar_connection_id
        connection_id = connection.id
        attempt = binding._direct_avatar_job_attempt()
        try:
            result = binding._run_identity_avatar_sync(
                connection,
                expected_revision,
                job_uuid,
                connection_id,
                attempt,
            )
        except OperationalError:
            # Preserve SQLSTATE for queue_job's transaction-level PostgreSQL
            # retry. The cursor may already be aborted, so the generic handler
            # must not query the binding and mask a serialization failure.
            raise
        except Exception as error:
            return binding._handle_identity_avatar_sync_error(
                error,
                attempt,
                expected_revision,
                job_uuid,
                connection_id,
            )
        if result is False:
            return False
        binding._notify_identity_avatar_updated()
        return True

    def _run_identity_avatar_sync(
        self, connection, expected_revision, job_uuid, connection_id, attempt
    ):
        health_revision, address = self._direct_avatar_preflight(connection)
        result = connection.get_adapter().fetch_identity_profile(connection, address)
        if not isinstance(result, IdentityProfileResult):
            raise AdapterError(
                "fetch_identity_profile must return IdentityProfileResult"
            )
        if not self._direct_avatar_lock_for_projection(connection):
            return False
        if self._direct_avatar_superseded(expected_revision, job_uuid, connection_id):
            return self._direct_avatar_schedule_latest(job_uuid)
        self._direct_avatar_validate_projection(connection, health_revision)
        with self.env.cr.savepoint():
            self._apply_identity_profile(result, expected_revision, attempt)
        return True

    def _handle_identity_avatar_sync_error(
        self, error, attempt, expected_revision, job_uuid, connection_id
    ):
        if self._direct_avatar_superseded(expected_revision, job_uuid, connection_id):
            return self._direct_avatar_schedule_latest(job_uuid)
        if isinstance(error, ProviderPausedError):
            raise RetryableJobError(
                str(error),
                seconds=provider_paused_retry_seconds(
                    error,
                    ("identity_avatar", self.id),
                ),
                ignore_retry=True,
            ) from error
        if isinstance(error, TransientAdapterError):
            if attempt >= QUEUE_ATTEMPT_CEILING:
                self._finish_identity_avatar_failure(error, attempt)
                return False
            raise RetryableJobError(
                str(error),
                seconds=getattr(error, "retry_after_seconds", 0) or None,
            ) from error
        if isinstance(error, UnsupportedEventError):
            self._finish_identity_avatar_unavailable(error, attempt)
            return False
        if isinstance(error, (AdapterError, DTOValidationError, ValidationError)):
            self._finish_identity_avatar_failure(error, attempt)
            return False
        if attempt >= QUEUE_ATTEMPT_CEILING:
            self._finish_identity_avatar_failure(error, attempt)
            return False
        raise RetryableJobError(str(error), seconds=None) from error

    def _identity_avatar_attachment_values(self, avatar):
        self.ensure_one()
        if avatar.state == "unavailable":
            return {}
        if avatar.state == "absent":
            return {
                "direct_avatar_attachment_id": False,
                "direct_avatar_sha256": False,
                "direct_avatar_provider_revision": (avatar.provider_revision or False),
            }
        if avatar.size_bytes > _IDENTITY_AVATAR_MAX_BYTES:
            raise AdapterError("identity avatar exceeds the size limit")
        declared = avatar.mime_type.split(";", 1)[0].strip().lower()
        detected = guess_mimetype(avatar.content, default=declared)
        detected = detected.split(";", 1)[0].strip().lower()
        if declared not in _IDENTITY_AVATAR_MIMETYPES or detected != declared:
            raise AdapterError("identity avatar MIME type is not supported")
        if (
            self.direct_avatar_sha256 == avatar.sha256
            and self.direct_avatar_attachment_id
        ):
            return {
                "direct_avatar_provider_revision": (avatar.provider_revision or False)
            }
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .with_context(image_no_postprocess=True)
            .create(
                {
                    "name": (avatar.file_name or "identity-avatar")[:255],
                    "type": "binary",
                    "datas": base64.b64encode(avatar.content),
                    "mimetype": declared,
                    "res_model": self._name,
                    "res_id": self.id,
                }
            )
        )
        return {
            "direct_avatar_attachment_id": attachment.id,
            "direct_avatar_sha256": avatar.sha256,
            "direct_avatar_provider_revision": avatar.provider_revision or False,
        }

    def _apply_identity_avatar(self, avatar, expected_revision, attempt):
        self.ensure_one()
        previous_attachment = self.direct_avatar_attachment_id
        now = fields.Datetime.now()
        values = {
            "direct_avatar_state": (
                "ready"
                if avatar.state == "unavailable" and self.direct_avatar_attachment_id
                else avatar.state
            ),
            "direct_avatar_last_synced_at": now,
            "direct_avatar_next_sync_at": now + _IDENTITY_AVATAR_TTL,
            "direct_avatar_applied_revision": expected_revision,
            "direct_avatar_attempts": attempt,
            "direct_avatar_queue_job_uuid": False,
            "direct_avatar_last_error_class": False,
            "direct_avatar_last_error_message": False,
        }
        values.update(self._identity_avatar_attachment_values(avatar))
        self.write(values)
        if (
            previous_attachment
            and previous_attachment != self.direct_avatar_attachment_id
        ):
            previous_attachment.sudo().unlink()

    def _apply_identity_profile(self, profile, expected_revision, attempt):
        """Project one fetched profile without bypassing identity name provenance."""

        self.ensure_one()
        if not isinstance(profile, IdentityProfileResult):
            raise AdapterError("identity profile result is invalid")
        self.identity_id._contact_center_observe_name(
            profile.display_name,
            fields.Datetime.now(),
            inbox_event=None,
        )
        self._apply_identity_avatar(profile.avatar, expected_revision, attempt)

    def _finish_identity_avatar_failure(self, error, attempt):
        self.ensure_one()
        self.write(
            {
                "direct_avatar_state": "failed",
                "direct_avatar_attempts": attempt,
                "direct_avatar_queue_job_uuid": False,
                "direct_avatar_next_sync_at": (
                    fields.Datetime.now() + _IDENTITY_AVATAR_TTL
                ),
                "direct_avatar_last_error_class": error.__class__.__name__[:128],
                "direct_avatar_last_error_message": str(error)[:4000],
            }
        )
        self._notify_identity_avatar_updated()

    def _finish_identity_avatar_unavailable(self, error, attempt):
        self.ensure_one()
        self.write(
            {
                "direct_avatar_state": (
                    "ready" if self.direct_avatar_attachment_id else "unavailable"
                ),
                "direct_avatar_attempts": attempt,
                "direct_avatar_queue_job_uuid": False,
                "direct_avatar_next_sync_at": (
                    fields.Datetime.now() + _IDENTITY_AVATAR_TTL
                ),
                "direct_avatar_last_error_class": error.__class__.__name__[:128],
                "direct_avatar_last_error_message": str(error)[:4000],
            }
        )
        self._notify_identity_avatar_updated()

    def _notify_identity_avatar_updated(self):
        for binding in self.exists():
            self.env["contact.center.application"]._notify_ui(
                binding.channel_id,
                "conversation_updated",
                {"channel_id": binding.channel_id.id},
            )
        return True

    @api.model
    def _cron_schedule_identity_avatars(
        self, limit=100, adapter_key=None, stagger_seconds=2
    ):
        now = fields.Datetime.now()
        bindings = self.sudo().search(
            [
                ("conversation_type", "=", "direct"),
                ("identity_id", "!=", False),
                ("active", "=", True),
                ("merged_into_id", "=", False),
                ("channel_id.active", "=", True),
                ("account_id.active", "=", True),
                "|",
                ("direct_avatar_next_sync_at", "=", False),
                ("direct_avatar_next_sync_at", "<=", now),
            ],
            order="direct_avatar_next_sync_at, id",
            limit=limit,
        )
        for index, binding in enumerate(bindings):
            connection = binding._direct_avatar_default_connection(
                adapter_key=adapter_key
            )
            if not connection:
                continue
            eta = (
                now + datetime.timedelta(seconds=index * max(0, stagger_seconds))
                if stagger_seconds
                else None
            )
            binding._request_identity_avatar_sync(connection, eta=eta)
        return True
