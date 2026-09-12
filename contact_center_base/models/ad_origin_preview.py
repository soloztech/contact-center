"""Private ad presentation, independent from immutable acquisition evidence."""

import hashlib
import uuid
from datetime import timedelta

from psycopg2 import Error as DatabaseError
from psycopg2.errors import DeadlockDetected, LockNotAvailable, SerializationFailure

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from odoo.addons.queue_job.exception import RetryableJobError

from ..services.ad_origin_preview import (
    PreviewError,
    clean_text,
    fetch_thumbnail,
    normalize_creative,
    public_source_url,
    thumbnail_url,
)
from ..services.job import (
    ACTIVE_QUEUE_JOB_STATES,
    canonical_queue_job,
    queue_job_owns_record,
)

_TOKEN = object()
_CONTEXT = "contact_center_ad_preview_token"


def _internal(records):
    return records.env.su and records.env.context.get(_CONTEXT) is _TOKEN


def _owned(records):
    return records.sudo().with_context(**{_CONTEXT: _TOKEN})


class ContactCenterAdPreviewLocator(models.Model):
    _name = "contact.center.ad.preview.locator"
    _description = "Private Ad Thumbnail Locator"
    _order = "id"

    reference = fields.Char(
        required=True, default=lambda self: str(uuid.uuid4()), index=True
    )
    connection_id = fields.Many2one(
        "contact.center.provider.connection",
        required=True,
        ondelete="cascade",
        index=True,
    )
    company_id = fields.Many2one(
        related="connection_id.company_id", store=True, index=True
    )
    source_key = fields.Char(required=True, index=True)
    url_hash = fields.Char(required=True, index=True)
    download_url = fields.Char(copy=False)
    expires_at = fields.Datetime(
        required=True,
        default=lambda self: fields.Datetime.now() + timedelta(days=1),
        index=True,
    )
    consumed = fields.Boolean(default=False, copy=False)
    _sql_constraints = [
        (
            "reference_unique",
            "unique(reference)",
            "The private locator reference must be unique.",
        ),
        (
            "source_url_unique",
            "unique(connection_id, source_key, url_hash)",
            "This ad thumbnail locator already exists.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            raise AccessError(_("Ad thumbnail locators are managed internally."))
        return super().create(vals_list)

    def write(self, values):
        if (
            not _internal(self)
            or set(values) - {"download_url", "consumed"}
            or values.get("download_url")
        ):
            raise AccessError(_("Private ad thumbnail locators cannot be changed."))
        return super().write(values)

    def unlink(self):
        if not _internal(self):
            raise AccessError(_("Ad thumbnail locators are managed internally."))
        return super().unlink()

    @api.model
    def _register_thumbnail_locator(self, connection, source_key, url):
        connection.ensure_one()
        url = thumbnail_url(url)
        if not url or not source_key or clean_text(source_key, 512) != source_key:
            return ""
        digest = hashlib.sha256(url.encode()).hexdigest()
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ["cc:ad-locator:%s:%s:%s" % (connection.id, source_key, digest)],
        )
        existing = self.sudo().search(
            [
                ("connection_id", "=", connection.id),
                ("source_key", "=", source_key),
                ("url_hash", "=", digest),
            ],
            limit=1,
        )
        if existing:
            return existing.reference
        return (
            _owned(self)
            .create(
                {
                    "connection_id": connection.id,
                    "source_key": source_key,
                    "url_hash": digest,
                    "download_url": url,
                }
            )
            .reference
        )

    @api.model
    def _resolve(self, reference, connection, source_key):
        record = self.sudo().search(
            [
                ("reference", "=", reference),
                ("connection_id", "=", connection.id),
                ("source_key", "=", source_key),
            ],
            limit=1,
        )
        if (
            not record
            or record.consumed
            or not record.download_url
            or record.expires_at <= fields.Datetime.now()
        ):
            raise PreviewError("expired")
        return record

    def _consume(self):
        return _owned(self).write({"download_url": False, "consumed": True})

    @api.model
    def _cron_expire(self):
        self.sudo().search(
            [("consumed", "=", False), ("expires_at", "<=", fields.Datetime.now())],
            limit=500,
        )._consume()
        return True


class ContactCenterAttributionPreview(models.Model):
    _name = "contact.center.attribution.preview"
    _description = "Contact Center Ad Origin Preview"
    _order = "id"

    public_ref = fields.Char(
        required=True, default=lambda self: str(uuid.uuid4()), index=True, copy=False
    )
    touchpoint_id = fields.Many2one(
        "contact.center.attribution.touchpoint",
        required=True,
        ondelete="cascade",
        index=True,
        copy=False,
    )
    account_id = fields.Many2one(
        related="touchpoint_id.account_id", store=True, index=True
    )
    company_id = fields.Many2one(
        related="touchpoint_id.company_id", store=True, index=True
    )
    title = fields.Char(copy=False)
    body = fields.Text(copy=False)
    source_public_url = fields.Char(copy=False)
    media_type = fields.Char(copy=False)
    thumbnail_ref = fields.Char(copy=False)
    thumbnail_attachment_id = fields.Many2one(
        "ir.attachment", copy=False, ondelete="set null"
    )
    width = fields.Integer(copy=False)
    height = fields.Integer(copy=False)
    state = fields.Selection(
        [("pending", "Pending"), ("ready", "Ready"), ("unavailable", "Unavailable")],
        required=True,
        default="unavailable",
    )
    presentation_source = fields.Selection(
        [
            ("provider_snapshot", "Provider Snapshot"),
            ("marketing_catalog", "Marketing Catalog"),
        ],
        required=True,
        default="provider_snapshot",
    )
    observed_at = fields.Datetime(required=True, default=fields.Datetime.now)
    fetched_at = fields.Datetime(copy=False)
    expires_at = fields.Datetime(
        required=True,
        default=lambda self: fields.Datetime.now() + timedelta(days=30),
        index=True,
    )
    expired = fields.Boolean(default=False, index=True)
    queue_job_uuid = fields.Char(copy=False, index=True)
    enrichment_attempted = fields.Boolean(default=False, copy=False)
    marketing_wake_key = fields.Char(copy=False)
    error_code = fields.Char(copy=False)
    _sql_constraints = [
        (
            "touchpoint_unique",
            "unique(touchpoint_id)",
            "The ad origin already has a preview.",
        ),
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The ad preview reference must be unique.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            raise AccessError(_("Ad previews are managed internally."))
        return super().create(vals_list)

    def write(self, values):
        if not _internal(self):
            raise AccessError(_("Ad previews are managed internally."))
        if {"touchpoint_id", "public_ref"}.intersection(values):
            raise AccessError(_("Ad preview identity cannot be changed."))
        return super().write(values)

    def unlink(self):
        if not _internal(self):
            raise AccessError(_("Ad previews are managed internally."))
        attachments = self.mapped("thumbnail_attachment_id")
        result = super().unlink()
        attachments.sudo().unlink()
        return result

    @api.model
    def _capture(self, touchpoint, creative, *, merge=False):
        touchpoint.ensure_one()
        if touchpoint.touchpoint_type not in ("paid_ad_click", "paid_ad_signal"):
            return self.browse()
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ["cc:ad-preview:%s" % touchpoint.id],
        )
        preview = self.sudo().search([("touchpoint_id", "=", touchpoint.id)], limit=1)
        if preview and (preview.expired or preview.expires_at <= fields.Datetime.now()):
            return preview
        values = normalize_creative(creative)
        if not values.get("public_url"):
            values["public_url"] = public_source_url(touchpoint.source_url)
        mapped = {
            "title": values.get("title"),
            "body": values.get("body"),
            "source_public_url": values.get("public_url"),
            "media_type": values.get("media_type"),
            "thumbnail_ref": values.get("thumbnail_ref"),
        }
        if not preview:
            mapped.update(
                touchpoint_id=touchpoint.id, observed_at=touchpoint.occurred_at
            )
            preview = _owned(self).create(mapped)
        elif merge:
            missing = {
                name: value
                for name, value in mapped.items()
                if value and not preview[name]
            }
            if missing:
                _owned(preview).write(missing)
        return preview

    def _identity(self):
        self.ensure_one()
        return "contact_center:ad_preview:%s" % self.id

    def _eligible(self):
        self.ensure_one()
        point = self.touchpoint_id
        binding = point.channel_binding_id
        message = point.message_binding_id
        return bool(
            not self.expired
            and self.expires_at > fields.Datetime.now()
            and point.conflict_state == "clean"
            and point.account_id.active
            and point.provider_connection_id.active
            and binding
            and binding.active
            and binding.channel_id.active
            and binding.account_id == self.account_id
            and binding.company_id == self.company_id
            and (
                point.source_key_kind != "message"
                or (
                    message
                    and message.message_id
                    and message.message_state != "deleted"
                )
            )
        )

    def _lock_projection(self):
        """Follow the message deletion fence before locking a derived preview."""
        self.ensure_one()
        point = self.touchpoint_id
        binding = point.message_binding_id
        if binding:
            self.env.cr.execute(
                "SELECT id FROM contact_center_message_binding WHERE id = %s FOR UPDATE",
                [binding.id],
            )
            if not self.env.cr.fetchone():
                return False
            binding.invalidate_recordset()
        self.env.cr.execute(
            "SELECT id FROM contact_center_attribution_preview WHERE id = %s FOR UPDATE",
            [self.id],
        )
        if not self.env.cr.fetchone():
            return False
        self.invalidate_recordset()
        point.invalidate_recordset()
        return self._eligible()

    def _needs_enrichment(self):
        self.ensure_one()
        return bool(
            self.account_id.ad_preview_enrichment_enabled
            and not self.enrichment_attempted
            and (
                not self.title
                or not self.body
                or not self.source_public_url
                or not self.thumbnail_ref
            )
        )

    def _queue_work(self):
        for preview in _owned(self):
            if not preview._lock_projection():
                continue
            needs_image = preview.thumbnail_ref and not preview.thumbnail_attachment_id
            if not needs_image and not preview._needs_enrichment():
                preview.write(
                    {
                        "state": "ready"
                        if preview.title
                        or preview.body
                        or preview.source_public_url
                        or preview.thumbnail_attachment_id
                        else "unavailable"
                    }
                )
                continue
            if canonical_queue_job(
                preview, preview._identity(), ACTIVE_QUEUE_JOB_STATES, adopt=False
            ):
                continue
            job = preview.with_delay(
                identity_key=preview._identity(),
                max_retries=3,
                priority=60,
                description="Contact Center ad preview %s" % preview.id,
            )._job_prepare_preview()
            preview.write(
                {"queue_job_uuid": job.uuid, "state": "pending", "error_code": False}
            )
        return True

    def _marketing_preview_values(self):
        """Optional Marketing bridge returns bounded copy and a private CDN locator."""
        self.ensure_one()
        return {}

    def _marketing_preview_validate(self, values):
        """Optional bridge checks its in-memory scope after all remote reads."""
        self.ensure_one()
        return True

    def _wake_marketing_enrichment(self, wake_key):
        """A catalog/link revision can wake an earlier incomplete attempt once."""
        self.ensure_one()
        preview = _owned(self)
        if (
            not wake_key
            or wake_key == preview.marketing_wake_key
            or not preview.account_id.ad_preview_enrichment_enabled
            or not preview._lock_projection()
        ):
            return False
        preview.write({"marketing_wake_key": wake_key})
        if canonical_queue_job(
            preview, preview._identity(), ACTIVE_QUEUE_JOB_STATES, adopt=False
        ):
            return False
        preview.write({"enrichment_attempted": False})
        preview._queue_work()
        return True

    def _retry_enrichment(self):
        """Explicit administrative retry, bounded by the caller's batch."""
        count = 0
        for preview in _owned(self):
            if (
                not preview.account_id.ad_preview_enrichment_enabled
                or not preview._lock_projection()
                or canonical_queue_job(
                    preview, preview._identity(), ACTIVE_QUEUE_JOB_STATES, adopt=False
                )
            ):
                continue
            values = {"enrichment_attempted": False, "error_code": False}
            if preview.thumbnail_ref and not preview.thumbnail_attachment_id:
                try:
                    self.env["contact.center.ad.preview.locator"]._resolve(
                        preview.thumbnail_ref,
                        preview.touchpoint_id.provider_connection_id,
                        preview.touchpoint_id.source_external_key,
                    )
                except PreviewError:
                    # The old URL remains consumed; a newly authorized lookup
                    # can now provide a fresh image without changing evidence.
                    values["thumbnail_ref"] = False
            preview.write(values)
            if preview._needs_enrichment() or (
                preview.thumbnail_ref and not preview.thumbnail_attachment_id
            ):
                preview._queue_work()
                count += 1
        return count

    def _enrichment_updates(self):
        self.ensure_one()
        updates, enriched = {}, {}
        preview = self
        if preview._needs_enrichment():
            enriched = preview._marketing_preview_values()
            if not isinstance(enriched, dict):
                enriched = {}
            normalized = normalize_creative(enriched)
            updates["enrichment_attempted"] = True
            for src, dest in (
                ("title", "title"),
                ("body", "body"),
                ("public_url", "source_public_url"),
                ("media_type", "media_type"),
            ):
                if normalized.get(src) and not preview[dest]:
                    updates[dest] = normalized[src]
            if len(updates) > 1:
                updates.update(
                    presentation_source="marketing_catalog",
                    fetched_at=fields.Datetime.now(),
                )
        return updates, enriched

    def _store_thumbnail(self, locator, pending_url, derivative):
        self.ensure_one()
        updates = {}
        if pending_url:
            reference = locator._register_thumbnail_locator(
                self.touchpoint_id.provider_connection_id,
                self.touchpoint_id.source_external_key,
                pending_url,
            )
            locator = locator.sudo().search([("reference", "=", reference)], limit=1)
            updates.update(
                thumbnail_ref=reference,
                presentation_source="marketing_catalog",
            )
        content, width, height = derivative
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .create(
                {
                    "name": "ad-preview.jpg",
                    "type": "binary",
                    "raw": content,
                    "mimetype": "image/jpeg",
                    "public": False,
                    "res_model": self._name,
                    "res_id": self.id,
                }
            )
        )
        updates.update(
            thumbnail_attachment_id=attachment.id,
            width=width,
            height=height,
            fetched_at=fields.Datetime.now(),
        )
        locator._consume()
        return updates

    def _finish_preview_error(self, updates, enriched, locator, error):
        """A failed download has the same authorization fence as a success."""
        self.ensure_one()
        try:
            if error.retryable:
                job = (
                    self.env["queue.job"]
                    .sudo()
                    .search([("uuid", "=", self.queue_job_uuid)], limit=1)
                )
                if job and job.retry < 3:
                    raise RetryableJobError(
                        "Ad preview download temporarily unavailable", seconds=60
                    ) from None
            values = dict(updates)
            code = error.code
            if not self._marketing_preview_validate(enriched):
                values = {}
                code = "invalid_scope"
            if not self._lock_projection():
                return False
            if locator.ids:
                locator._consume()
            values.update(state="unavailable", error_code=code)
            self.write(values)
            return True
        except RetryableJobError:
            raise
        except (SerializationFailure, DeadlockDetected, LockNotAvailable):
            raise RetryableJobError(
                "Ad preview content changed during processing",
                seconds=5,
                ignore_retry=True,
            ) from None
        except Exception:
            # Finalization must abort without publishing either rejected copy or
            # provider/SQL exception text. An aborted cursor is never reused.
            raise RuntimeError("Ad preview finalization unavailable") from None

    def _job_prepare_preview(self):
        self.ensure_one()
        preview = _owned(self).exists()
        if (
            not preview
            or not queue_job_owns_record(preview)
            or preview.state != "pending"
            or not preview._eligible()
        ):
            return False
        locator = self.env["contact.center.ad.preview.locator"]
        updates, enriched, derivative = {}, {}, None
        try:
            updates, enriched = preview._enrichment_updates()
            pending_url = (
                thumbnail_url(enriched.get("thumbnail_url"))
                if not preview.thumbnail_ref and not preview.thumbnail_attachment_id
                else ""
            )
            if pending_url:
                derivative = fetch_thumbnail(pending_url)
            elif preview.thumbnail_ref and not preview.thumbnail_attachment_id:
                locator = locator._resolve(
                    preview.thumbnail_ref,
                    preview.touchpoint_id.provider_connection_id,
                    preview.touchpoint_id.source_external_key,
                )
                derivative = fetch_thumbnail(locator.download_url)
            # Both remote operations finish before taking the deletion fence.
            if not preview._marketing_preview_validate(enriched):
                updates = {}
                raise PreviewError("invalid_scope")
            if not preview._lock_projection():
                if locator.ids:
                    locator._consume()
                return False
            if derivative:
                updates.update(
                    preview._store_thumbnail(locator, pending_url, derivative)
                )
            preview.write(updates)
            preview.write(
                {
                    "state": "ready"
                    if preview.title
                    or preview.body
                    or preview.source_public_url
                    or preview.thumbnail_attachment_id
                    else "unavailable",
                    "error_code": False,
                }
            )
        except (SerializationFailure, DeadlockDetected, LockNotAvailable):
            raise RetryableJobError(
                "Ad preview content changed during processing",
                seconds=5,
                ignore_retry=True,
            ) from None
        except DatabaseError:
            # Keep an aborted cursor out of the fallback write path, and keep
            # SQL parameters (which can include signed locators) out of job logs.
            raise RuntimeError("Ad preview storage unavailable") from None
        except PreviewError as error:
            if not preview._finish_preview_error(updates, enriched, locator, error):
                return False
        except Exception:
            # Third-party bridge failures must not leak URLs or credentials to jobs.
            updates["enrichment_attempted"] = True
            if not preview._finish_preview_error(
                updates, enriched, locator, PreviewError("unavailable")
            ):
                return False
        preview._notify()
        return True

    def _notify(self):
        for preview in self:
            point = preview.touchpoint_id
            if point.channel_binding_id.channel_id:
                self.env["contact.center.application"]._notify_ui(
                    point.channel_binding_id.channel_id, "attribution_updated", {}
                )
                if point.message_binding_id.message_id:
                    self.env["contact.center.application"]._notify_ui(
                        point.channel_binding_id.channel_id,
                        "message_updated",
                        {"message_id": point.message_binding_id.message_id.id},
                    )

    def _authorize_read(self):
        self.ensure_one()
        if (
            not self._eligible()
            or not self.account_id._contact_center_user_can_view_attribution()
        ):
            raise AccessError(_("The ad preview is not available."))
        self.env["contact.center.ui.api"].sudo(False)._authorized_channel(
            self.touchpoint_id.channel_binding_id.channel_id.id
        )
        return True

    def _descriptor(self):
        self.ensure_one()
        self._authorize_read()
        state = self.state
        if state == "pending" and not canonical_queue_job(
            self, self._identity(), ACTIVE_QUEUE_JOB_STATES, adopt=False
        ):
            state = "unavailable"
        return {
            "public_ref": self.public_ref,
            "title": self.title or "",
            "body": self.body or "",
            "source_url": self.source_public_url or "",
            "thumbnail_url": "/contact_center/attribution/%s/thumbnail"
            % self.public_ref
            if self.thumbnail_attachment_id
            else "",
            "media_type": self.media_type or "",
            "state": state,
            "presentation_source": self.presentation_source,
            "observed_at": fields.Datetime.to_string(self.observed_at),
            "fetched_at": fields.Datetime.to_string(self.fetched_at)
            if self.fetched_at
            else "",
        }

    def _expire(self):
        # Retention locks jobs before previews; use the same order and do not
        # wait for running jobs. Their post-download fence sees expired content.
        jobs = (
            self.env["queue.job"]
            .sudo()
            .search(
                [
                    ("identity_key", "in", [item._identity() for item in self]),
                    ("state", "in", ("pending", "enqueued", "wait_dependencies")),
                ]
            )
        )
        if jobs:
            self.env.cr.execute(
                "SELECT id FROM queue_job WHERE id IN %s ORDER BY id FOR UPDATE SKIP LOCKED",
                [tuple(jobs.ids)],
            )
            locked = jobs.browse([row[0] for row in self.env.cr.fetchall()])
            locked.invalidate_recordset()
            locked.filtered(
                lambda job: job.state in ("pending", "enqueued", "wait_dependencies")
            ).button_cancelled()
        for preview in _owned(self):
            self.env.cr.execute(
                "SELECT id FROM contact_center_attribution_preview WHERE id = %s FOR UPDATE",
                [preview.id],
            )
            preview.invalidate_recordset()
            attachment = preview.thumbnail_attachment_id
            locators = (
                self.env["contact.center.ad.preview.locator"]
                .sudo()
                .search([("reference", "=", preview.thumbnail_ref)])
            )
            preview.write(
                {
                    "title": False,
                    "body": False,
                    "source_public_url": False,
                    "thumbnail_ref": False,
                    "thumbnail_attachment_id": False,
                    "width": 0,
                    "height": 0,
                    "expired": True,
                    "state": "unavailable",
                    "error_code": "expired",
                }
            )
            locators._consume()
            attachment.sudo().unlink()
        return True

    @api.model
    def _cron_expire(self):
        self.sudo().search(
            [("expired", "=", False), ("expires_at", "<=", fields.Datetime.now())],
            limit=100,
        )._expire()
        self.env["contact.center.ad.preview.locator"]._cron_expire()
        return True
