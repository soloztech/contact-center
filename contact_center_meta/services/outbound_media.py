"""Private, bounded attachment uploads for the Page-linked Send API."""

import hashlib
import json
import re

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    AmbiguousTimeoutError,
    TransientAdapterError,
)
from odoo.addons.contact_center_base.services.media import MEDIA_SIZE_LIMITS

from .api import graph_request

_MB = 1000 * 1000
_FORMATS = {
    "image": {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif"},
    "audio": {
        "audio/aac": "aac",
        "audio/mp4": "m4a",
        "audio/x-m4a": "m4a",
        "audio/mpeg": "mp3",
        "audio/ogg": "ogg",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
    },
    "video": {
        "video/mp4": "mp4",
        "video/ogg": "ogg",
        "video/x-msvideo": "avi",
        "video/quicktime": "mov",
        "video/webm": "webm",
    },
    "document": {"application/pdf": "pdf"},
}


def media_capabilities(connection):
    instagram = connection.meta_transport_mode == "instagram_page_linked"
    result = {}
    for kind, formats in _FORMATS.items():
        mimetypes = list(formats)
        if instagram and kind == "image":
            mimetypes.remove("image/gif")
        if instagram and kind == "audio":
            mimetypes = [
                value for value in mimetypes if value not in ("audio/mpeg", "audio/ogg")
            ]
        result[kind] = {
            "enabled": True,
            "caption": False,
            "max_bytes": min(
                MEDIA_SIZE_LIMITS[kind],
                8 * _MB if instagram and kind == "image" else 25 * _MB,
            ),
            "mimetypes": mimetypes,
        }
    return result


def private_media_content(env, connection, command):
    """Resolve only the immutable private media owned by this outbound message."""
    if len(command.message.media) != 1 or command.message.text:
        raise AdapterError("Meta sends one attachment per message without a caption")
    media = command.message.media[0]
    policy = media_capabilities(connection).get(media.kind)
    if not policy or command.message.content_type != media.kind or media.is_voice_note:
        raise AdapterError("Meta outbound media kind is unsupported")
    if media.mime_type not in policy["mimetypes"]:
        raise AdapterError("Meta outbound media MIME type is unsupported")
    if not 0 < media.size_bytes <= policy["max_bytes"]:
        raise AdapterError("Meta outbound media exceeds the provider size limit")
    locator = media.remote_locator
    attachment_id = locator.get("attachment_id") if isinstance(locator, dict) else None
    if (
        isinstance(attachment_id, bool)
        or not isinstance(attachment_id, int)
        or attachment_id <= 0
    ):
        raise AdapterError("Meta outbound media requires a private attachment")
    rows = (
        env["contact.center.media.binding"]
        .sudo()
        .search(
            [
                ("attachment_id", "=", attachment_id),
                ("provider_connection_id", "=", connection.id),
                (
                    "message_binding_id.channel_binding_id.conversation_ref",
                    "=",
                    command.conversation_ref,
                ),
                ("message_binding_id.direction", "=", "outbound"),
                ("message_binding_id.origin", "=", "agent"),
            ],
            limit=2,
        )
    )
    if (
        len(rows) != 1
        or rows.state != "ready"
        or rows._as_dto().to_dict() != media.to_dict()
    ):
        raise AdapterError("Meta outbound attachment has no exact message ownership")
    binding = rows.message_binding_id
    attachment = rows.attachment_id.exists()
    if (
        binding.account_id != connection.account_id
        or (binding.client_message_id or "") != command.message.client_message_id
        or not attachment
        or attachment.type != "binary"
        or attachment.public
        or attachment.res_model != "mail.message"
        or attachment.res_id != binding.message_id.id
        or attachment.file_size != media.size_bytes
        or attachment.mimetype != media.mime_type
    ):
        raise AdapterError("Meta outbound private attachment evidence is inconsistent")
    content = attachment.raw
    if isinstance(content, memoryview):
        content = content.tobytes()
    if not isinstance(content, bytes) or len(content) != media.size_bytes:
        raise AdapterError("Meta outbound attachment size does not match its content")
    if hashlib.sha256(content).hexdigest() != media.sha256:
        raise AdapterError("Meta outbound attachment hash does not match its content")
    # Meta rejects non-ASCII multipart filenames. Preserve the original name in
    # Odoo and use a deterministic safe name at the provider boundary.
    filename = "media-%s.%s" % (
        media.sha256[:20],
        _FORMATS[media.kind][media.mime_type],
    )
    descriptor = {
        "content_omitted": True,
        "attachment_id": attachment.id,
        "mime_type": media.mime_type,
        "file_name": filename,
        "size_bytes": len(content),
        "sha256": media.sha256,
    }
    return media, content, descriptor


def upload_private_media(
    runtime, page_token, page_id, contract, media, content, descriptor
):
    payload = {
        "message": json.dumps(
            {
                "attachment": {
                    "type": "file" if media.kind == "document" else media.kind,
                    "payload": {"is_reusable": True},
                }
            },
            separators=(",", ":"),
        ),
    }
    if contract["platform"] == "instagram":
        payload["platform"] = "instagram"
    try:
        response = graph_request(
            runtime,
            page_token,
            "POST",
            "%s/message_attachments" % page_id,
            data=payload,
            files={"filedata": (descriptor["file_name"], content, media.mime_type)},
            mutating=True,
            max_response_bytes=64 * 1024,
        )
    except AmbiguousTimeoutError as exc:
        # This endpoint creates an unattached media object. Retrying an uncertain
        # upload cannot duplicate a recipient-visible message; the Send API below
        # remains subject to the ordinary uncertain-dispatch fence.
        raise TransientAdapterError("Meta attachment upload needs a retry") from exc
    attachment_id = (
        response.get("attachment_id") if isinstance(response, dict) else None
    )
    if not isinstance(attachment_id, str) or not re.fullmatch(
        r"[0-9]{1,80}", attachment_id
    ):
        raise TransientAdapterError(
            "Meta attachment upload returned no usable attachment ID"
        )
    return attachment_id
