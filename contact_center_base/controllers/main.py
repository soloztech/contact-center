import json
import os
import uuid as uuidlib

from werkzeug.exceptions import NotFound

from odoo import _, http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request
from odoo.tools.mimetypes import guess_mimetype

from odoo.addons.mail.controllers.bus import MailChatController
from odoo.addons.mail.controllers.discuss import DiscussController

from ..services.adapter import AdapterError, conversation_capabilities
from ..services.media import (
    MEDIA_SIZE_LIMITS,
    canonical_recorded_audio_duration_seconds,
    is_iso_bmff_audio_only,
    media_kind_for_mimetype,
    upload_values_from_content,
    validate_provider_media_capability,
    validate_provider_recorded_audio_capability,
)

_INLINE_MEDIA_KINDS = frozenset(("image", "audio", "video"))
_MEDIA_CONTENT_SECURITY_POLICY = "default-src 'none'; sandbox"
_AVATAR_MAX_BYTES = 2 * 1024 * 1024
_AVATAR_MIMETYPES = frozenset(("image/jpeg", "image/png", "image/webp"))
_RECORDED_AUDIO_MAX_DURATION_SECONDS = 15 * 60
_RECORDED_AUDIO_AMBIGUOUS_CONTAINERS = {
    "audio/mp4": frozenset(("application/mp4", "video/mp4")),
    "audio/ogg": frozenset(("application/ogg",)),
}


def _audio_upload_metadata(form):
    """Validate provider-neutral recorded-audio metadata from multipart data."""

    raw_voice_note = form.get("is_voice_note")
    if raw_voice_note not in (None, "", "0", "1"):
        raise ValidationError(_("The voice-note flag must be 0 or 1."))
    is_voice_note = raw_voice_note == "1"
    raw_duration = form.get("duration_seconds")
    if raw_duration in (None, ""):
        duration_seconds = 0
    elif not str(raw_duration).isdigit():
        raise ValidationError(_("The audio duration must be an integer."))
    else:
        duration_seconds = int(raw_duration)
    if not 0 <= duration_seconds <= _RECORDED_AUDIO_MAX_DURATION_SECONDS:
        raise ValidationError(_("The audio duration is outside the allowed range."))
    if is_voice_note and not duration_seconds:
        raise ValidationError(_("A voice note must declare a positive duration."))
    return is_voice_note, duration_seconds


def _upload_mimetype(content, declared_mimetype, is_recorded_audio=False):
    """Sniff an upload while preserving audio-only ambiguous recorder containers."""

    declared_mimetype = (declared_mimetype or "").split(";", 1)[0].strip().lower()
    detected_mimetype = guess_mimetype(content, default=declared_mimetype)
    detected_mimetype = (detected_mimetype or "").split(";", 1)[0].strip().lower()
    # ``guess_mimetype`` returns its caller-provided default for an ISO-BMFF
    # stream it cannot classify.  Never let a manual video upload become audio
    # merely because the multipart header claimed ``audio/mp4``.  Browser
    # recordings are admitted provisionally and then structurally checked by
    # the selected provider both here and again at consumption/dispatch.
    if declared_mimetype == "audio/mp4" and not is_recorded_audio:
        if is_iso_bmff_audio_only(content):
            return "audio/mp4"
        if detected_mimetype == "audio/mp4":
            return "application/mp4"
    if detected_mimetype in ("application/octet-stream", "application/zip"):
        detected_mimetype = declared_mimetype or detected_mimetype
    if is_recorded_audio and detected_mimetype in (
        _RECORDED_AUDIO_AMBIGUOUS_CONTAINERS.get(declared_mimetype, ())
    ):
        detected_mimetype = declared_mimetype
    return detected_mimetype or declared_mimetype


def _media_response_policy(kind, declared_mimetype, content, download=False):
    """Return a sniffed MIME and a safe content disposition.

    Documents are downloads even when their stored MIME is browser-renderable.  Media
    declared as image/audio/video may render inline only when content sniffing agrees
    with the declared kind.
    """

    declared_mimetype = (
        (declared_mimetype or "application/octet-stream")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    detected_mimetype = guess_mimetype(content, default=declared_mimetype)
    detected_mimetype = (
        (detected_mimetype or "application/octet-stream")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    force_download = str(download).lower() in ("1", "true")
    can_render_inline = (
        not force_download
        and kind in _INLINE_MEDIA_KINDS
        and media_kind_for_mimetype(detected_mimetype) == kind
    )
    return detected_mimetype, "inline" if can_render_inline else "attachment"


def _json_response(payload, status=200):
    return request.make_response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        headers=[("Content-Type", "application/json; charset=utf-8")],
        status=status,
    )


def _lock_media_upload_reference(cursor, reference):
    """Serialize one client UUID before the global idempotency lookup/create."""

    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        ["contact_center.media.upload:%s" % reference],
    )


def _avatar_response(attachment, etag=""):
    """Serve one private, bounded and MIME-sniffed local avatar."""

    content = attachment.sudo().raw or b""
    if not content or len(content) > _AVATAR_MAX_BYTES:
        raise NotFound()
    declared_mimetype = (
        (attachment.mimetype or "application/octet-stream")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    detected_mimetype = guess_mimetype(content, default=declared_mimetype)
    detected_mimetype = detected_mimetype.split(";", 1)[0].strip().lower()
    if (
        declared_mimetype not in _AVATAR_MIMETYPES
        or detected_mimetype != declared_mimetype
    ):
        raise NotFound()
    if etag and request.httprequest.headers.get("If-None-Match") == '"%s"' % etag:
        return request.make_response(
            b"",
            headers=[
                ("Cache-Control", "private, max-age=3600"),
                ("ETag", '"%s"' % etag),
            ],
            status=304,
        )
    headers = [
        ("Cache-Control", "private, max-age=3600"),
        ("Content-Length", str(len(content))),
        ("Content-Security-Policy", _MEDIA_CONTENT_SECURITY_POLICY),
        ("Content-Type", detected_mimetype),
        ("X-Content-Type-Options", "nosniff"),
    ]
    if etag:
        headers.append(("ETag", '"%s"' % etag))
    return request.make_response(content, headers=headers, status=200)


class ContactCenterMailChatController(MailChatController):
    @staticmethod
    def _contact_center_channel(uuid):
        return (
            request.env["mail.channel"]
            .sudo()
            .search(
                [("uuid", "=", uuid), ("channel_type", "=", "contact_center")],
                limit=1,
            )
        )

    @http.route()
    def mail_chat_post(self, uuid, message_content, **kwargs):
        if self._contact_center_channel(uuid):
            return False
        return super().mail_chat_post(uuid, message_content, **kwargs)

    @http.route()
    def mail_chat_history(self, uuid, last_id=False, limit=20):
        if self._contact_center_channel(uuid):
            return []
        return super().mail_chat_history(uuid, last_id=last_id, limit=limit)


class ContactCenterDiscussController(DiscussController):
    @staticmethod
    def _reject_contact_center(channel_sudo):
        if channel_sudo.channel_type == "contact_center":
            raise NotFound()

    def _response_discuss_channel_invitation(
        self, channel_sudo, is_channel_token_secret=True
    ):
        self._reject_contact_center(channel_sudo)
        return super()._response_discuss_channel_invitation(
            channel_sudo,
            is_channel_token_secret=is_channel_token_secret,
        )

    def _response_discuss_public_channel_template(
        self, channel_sudo, discuss_public_view_data=None
    ):
        self._reject_contact_center(channel_sudo)
        return super()._response_discuss_public_channel_template(
            channel_sudo,
            discuss_public_view_data=discuss_public_view_data,
        )


class ContactCenterMediaController(http.Controller):
    @http.route(
        "/contact_center/media/upload",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def upload_media(self, **_kwargs):
        try:
            is_voice_note, duration_seconds = _audio_upload_metadata(
                request.httprequest.form
            )
            channel_id = request.httprequest.form.get("channel_id")
            channel, _member = request.env["contact.center.ui.api"]._authorized_channel(
                channel_id
            )
            binding = request.env["contact.center.ui.api"]._binding_for_channel(channel)
            if not binding:
                raise ValidationError(_("The conversation has no active binding."))
            binding._contact_center_ensure_outbound_supported(
                operation="upload_media", has_media=True
            )
            upload_file = request.httprequest.files.get("file")
            if not upload_file or not upload_file.filename:
                raise ValidationError(_("A media file is required."))
            reference_value = request.httprequest.form.get("client_upload_id")
            try:
                reference_value = str(uuidlib.UUID(str(reference_value)))
            except (AttributeError, TypeError, ValueError) as error:
                raise ValidationError(
                    _("The client upload ID must be a UUID.")
                ) from error
            max_size = max(MEDIA_SIZE_LIMITS.values())
            content = upload_file.stream.read(max_size + 1)
            if not content:
                raise ValidationError(_("The media file is empty."))
            if len(content) > max_size:
                raise ValidationError(_("The media file exceeds the upload limit."))
            declared_mimetype = (upload_file.mimetype or "").split(";", 1)[0]
            upload_mimetype = _upload_mimetype(
                content,
                declared_mimetype,
                is_recorded_audio=bool(duration_seconds),
            )
            if duration_seconds:
                # The multipart value only marks a browser recording.  The
                # canonical duration always comes from the bounded container.
                duration_seconds = canonical_recorded_audio_duration_seconds(
                    content, upload_mimetype
                )
            filename = os.path.basename(upload_file.filename).strip()[:255]
            values = upload_values_from_content(content, upload_mimetype, filename)
            values.update(
                is_voice_note=is_voice_note,
                duration_seconds=duration_seconds,
            )
            if duration_seconds and values["kind"] != "audio":
                raise ValidationError(_("Only audio uploads may declare a duration."))
            connection = request.env["contact.center.application"]._outbound_connection(
                binding,
                "send_message",
                _("The active provider connection cannot send messages yet."),
            )
            capabilities = conversation_capabilities(
                connection.capabilities_json if connection else {},
                binding.conversation_type,
            )
            validate_provider_media_capability(
                capabilities,
                values["kind"],
                values["mime_type"],
                values["size_bytes"],
            )
            validate_provider_recorded_audio_capability(
                capabilities,
                values["mime_type"],
                is_voice_note,
                duration_seconds,
            )
            try:
                connection.get_adapter().validate_recorded_audio_upload(
                    connection,
                    content=content,
                    mimetype=values["mime_type"],
                    is_voice_note=is_voice_note,
                    duration_seconds=duration_seconds,
                )
            except AdapterError as error:
                raise ValidationError(
                    _("The active provider rejected the recorded audio file.")
                ) from error

            upload_model = request.env["contact.center.media.upload"].sudo()
            _lock_media_upload_reference(request.env.cr, reference_value)
            existing = upload_model.search(
                [("reference", "=", reference_value)],
                limit=1,
            )
            if existing:
                if (
                    existing.uploaded_by_user_id != request.env.user
                    or existing.channel_binding_id != binding
                    or existing.sha256 != values["sha256"]
                    or existing.is_voice_note != is_voice_note
                    or existing.duration_seconds != duration_seconds
                ):
                    raise ValidationError(
                        _("The client upload ID was already used for another file.")
                    )
                upload = existing
            else:
                # Expected failures become HTTP responses below, so the request
                # transaction itself will commit. Roll back the whole upload
                # aggregate before returning a validation/access error.
                with request.env.cr.savepoint():
                    attachment = (
                        request.env["ir.attachment"]
                        .sudo()
                        .with_context(image_no_postprocess=True)
                        .create(
                            {
                                "name": values["file_name"],
                                "type": "binary",
                                "raw": content,
                                "mimetype": values["mime_type"],
                                "res_model": "contact.center.media.upload",
                                "res_id": 0,
                            }
                        )
                    )
                    upload = upload_model.create(
                        {
                            **values,
                            "reference": reference_value,
                            "channel_binding_id": binding.id,
                            "uploaded_by_user_id": request.env.user.id,
                            "attachment_id": attachment.id,
                        }
                    )
                    attachment.write({"res_id": upload.id})
            return _json_response(
                {
                    "schema_version": 1,
                    "media_ref": upload.reference,
                    "media": {
                        "kind": upload.kind,
                        "name": upload.file_name,
                        "mimetype": upload.mime_type,
                        "size_bytes": upload.size_bytes,
                        "is_voice_note": upload.is_voice_note,
                        "duration_seconds": upload.duration_seconds,
                    },
                },
                200,
            )
        except AccessError:
            return _json_response({"error": "forbidden"}, 403)
        except (UserError, ValidationError) as error:
            return _json_response({"error": str(error)}, 400)

    @http.route(
        "/contact_center/media/<int:media_id>/content",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def media_content(self, media_id, download=False, **_kwargs):
        media = request.env["contact.center.media.binding"].browse(media_id).exists()
        if not media:
            raise NotFound()
        try:
            media.check_access_rights("read")
            media.check_access_rule("read")
        except AccessError as error:
            raise NotFound() from error
        message_binding = media.message_binding_id
        if (
            message_binding
            and message_binding.message_state == "deleted"
            and message_binding.deleted_display_mode != "strike"
        ):
            raise NotFound()
        if media.state != "ready" or not media.attachment_id:
            raise NotFound()
        attachment = media.attachment_id.sudo()
        stream = request.env["ir.binary"]._get_stream_from(
            attachment,
            "raw",
            filename=media.file_name or attachment.name,
        )
        if stream.type == "path":
            try:
                with open(stream.path, "rb") as media_file:
                    content_head = media_file.read(4096)
            except OSError as error:
                raise NotFound() from error
        elif stream.type == "data":
            content_head = (stream.data or b"")[:4096]
        else:
            raise NotFound()
        if (
            not content_head
            or not stream.size
            or (media.size_bytes and stream.size != media.size_bytes)
        ):
            raise NotFound()
        response_mimetype, disposition_type = _media_response_policy(
            media.kind,
            media.mime_type or attachment.mimetype,
            content_head,
            download=download,
        )
        stream.mimetype = response_mimetype
        stream.download_name = media.file_name or attachment.name or "media"
        stream.etag = media.sha256 or stream.etag
        stream.public = False
        stream.conditional = True
        response = stream.get_response(
            as_attachment=disposition_type == "attachment",
            content_security_policy=_MEDIA_CONTENT_SECURITY_POLICY,
            max_age=0,
        )
        response.headers["Accept-Ranges"] = "bytes"
        response.headers[
            "Cache-Control"
        ] = "private, no-cache, max-age=0, must-revalidate"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


class ContactCenterGroupAvatarController(http.Controller):
    @http.route(
        "/contact_center/group/<int:channel_id>/avatar",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def group_avatar(self, channel_id, **_kwargs):
        try:
            channel, _member = request.env["contact.center.ui.api"]._authorized_channel(
                channel_id
            )
        except (AccessError, ValidationError) as error:
            raise NotFound() from error
        profile = (
            request.env["contact.center.group.profile"]
            .sudo()
            .search(
                [
                    ("channel_id", "=", channel.id),
                    ("channel_binding_id.conversation_type", "=", "group"),
                ],
                limit=1,
            )
        )
        if not profile or not profile.avatar_attachment_id:
            raise NotFound()
        return _avatar_response(
            profile.avatar_attachment_id, etag=profile.avatar_sha256 or ""
        )


class ContactCenterIdentityAvatarController(http.Controller):
    @http.route(
        "/contact_center/conversation/<int:channel_id>/avatar",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def identity_avatar(self, channel_id, **_kwargs):
        try:
            channel, _member = request.env["contact.center.ui.api"]._authorized_channel(
                channel_id
            )
        except (AccessError, ValidationError) as error:
            raise NotFound() from error
        binding = (
            request.env["contact.center.channel.binding"]
            .sudo()
            .search(
                [
                    ("channel_id", "=", channel.id),
                    ("conversation_type", "=", "direct"),
                    ("identity_id", "!=", False),
                    ("active", "=", True),
                    ("merged_into_id", "=", False),
                ],
                limit=1,
            )
        )
        if not binding or not binding.direct_avatar_attachment_id:
            raise NotFound()
        return _avatar_response(
            binding.direct_avatar_attachment_id,
            etag=binding.direct_avatar_sha256 or "",
        )
