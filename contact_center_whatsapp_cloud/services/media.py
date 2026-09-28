"""Inbound media download and outbound media upload for the Cloud API.

Inbound: ``GET /{version}/{media_id}`` returns a short-lived (5 minute) URL that
is downloaded with the same Bearer token, from an allow-listed host, within the
core size limits and with the webhook SHA-256. An expired URL is resolved again
by the media ID; a media object gone after its 7-day retention is permanent.
"""

import hashlib
import mimetypes
import re
from urllib.parse import urljoin, urlsplit

import requests

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    AmbiguousTimeoutError,
    TransientAdapterError,
)
from odoo.addons.contact_center_base.services.dto import MediaDownloadResult
from odoo.addons.contact_center_base.services.media import MEDIA_SIZE_LIMITS

from .api import graph_request, resolve_graph_runtime
from .normalizer import _sha256_hex

_DOWNLOAD_TIMEOUT = (5, 30)
_MAX_REDIRECTS = 2
_MEDIA_HOST_SUFFIXES = ("fbsbx.com", "fbcdn.net", "whatsapp.net", "facebook.com")
_MEDIA_ID_RE = re.compile(r"^[0-9]{1,40}$")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._() -]+")
_EXPIRED_STATUSES = (401, 403, 404, 410)
_MB = 1000 * 1000
# Cloud API send limits (provider), intersected with the core and with the
# shared upload client cap (``_GRAPH_MAX_UPLOAD_BYTES`` = 25 MiB).
SHARED_UPLOAD_MAX_BYTES = 25 * 1024 * 1024
PROVIDER_SEND_LIMITS = {
    "image": 5 * _MB,
    "audio": 16 * _MB,
    "video": 16 * _MB,
    "document": 100 * _MB,
}
STICKER_LIMITS = {"static": 100 * 1000, "animated": 500 * 1000}
SEND_FORMATS = {
    "image": ("image/jpeg", "image/png"),
    "audio": ("audio/aac", "audio/amr", "audio/mpeg", "audio/mp4", "audio/ogg"),
    "video": ("video/mp4", "video/3gpp"),
    "document": (
        "application/pdf",
        "text/plain",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
}
CAPTION_KINDS = frozenset({"image", "video", "document"})
MAX_CAPTION_CHARACTERS = 1024


def send_limit(kind):
    """The smallest of the provider, core and shared transport limits."""

    return min(
        PROVIDER_SEND_LIMITS[kind], MEDIA_SIZE_LIMITS[kind], SHARED_UPLOAD_MAX_BYTES
    )


def receive_limit(kind):
    """Inbound downloads are bounded by the core limit (document: 50 MiB)."""

    return MEDIA_SIZE_LIMITS[kind]


def media_capabilities():
    return {
        kind: {
            "enabled": True,
            "caption": kind in CAPTION_KINDS,
            "max_bytes": send_limit(kind),
            "mimetypes": list(formats),
        }
        for kind, formats in SEND_FORMATS.items()
    }


def _header(headers, name):
    target = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == target:
            return str(value or "").strip()
    return ""


def _retry_after(response):
    try:
        return max(0, min(int(_header(response.headers, "Retry-After")), 3600))
    except (TypeError, ValueError):
        return 0


def _validated_media_url(value):
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise AdapterError("WhatsApp media URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise AdapterError("WhatsApp media URL is invalid") from None
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or port not in (None, 443)
        or not any(
            hostname == suffix or hostname.endswith(".%s" % suffix)
            for suffix in _MEDIA_HOST_SUFFIXES
        )
    ):
        raise AdapterError("WhatsApp media host is not allowed")
    return value, hostname


def _media_id(media):
    locator = media.remote_locator
    if not isinstance(locator, dict) or set(locator) != {"media_id"}:
        raise AdapterError("WhatsApp media locator is invalid")
    media_id = locator.get("media_id")
    if not isinstance(media_id, str) or not _MEDIA_ID_RE.fullmatch(media_id):
        raise AdapterError("WhatsApp media locator is invalid")
    return media_id


def _resolve_media(runtime, token, media_id):
    """Return the short-lived URL and metadata of one media ID (a read)."""

    payload = graph_request(runtime, token, "GET", media_id, max_response_bytes=16384)
    if not isinstance(payload, dict) or str(payload.get("id") or "") != media_id:
        raise TransientAdapterError("WhatsApp media resolution is inconsistent")
    url, hostname = _validated_media_url(payload.get("url"))
    size = payload.get("file_size")
    if isinstance(size, str) and size.strip().isdigit():
        size = int(size.strip())
    return {
        "url": url,
        "hostname": hostname,
        "mime_type": str(payload.get("mime_type") or "").split(";", 1)[0].strip(),
        "sha256": _sha256_hex(payload.get("sha256")),
        "file_size": size
        if isinstance(size, int) and not isinstance(size, bool)
        else 0,
    }


def _request(url, hostname, token):
    """GET one media URL; the Bearer token only reaches the resolved host."""

    current_url, current_host = url, hostname
    for redirect_count in range(_MAX_REDIRECTS + 1):
        headers = {"Accept": "*/*"}
        if current_host == hostname:
            headers["Authorization"] = "Bearer %s" % token
        try:
            response = requests.request(
                "GET",
                current_url,
                headers=headers,
                timeout=_DOWNLOAD_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException:
            # The request exception can contain the complete signed URL.
            raise TransientAdapterError("WhatsApp media host did not respond") from None
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        try:
            location = _header(response.headers, "Location")
            if not location or redirect_count >= _MAX_REDIRECTS:
                raise AdapterError("WhatsApp media redirect chain is invalid")
            current_url, current_host = _validated_media_url(
                urljoin(current_url, location)
            )
        finally:
            response.close()
    raise AdapterError("WhatsApp media redirect chain is invalid")


def _bounded_content(response, maximum):
    declared = _header(response.headers, "Content-Length")
    if declared.isdigit() and int(declared) > maximum:
        raise AdapterError("WhatsApp media exceeds the size limit")
    content = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > maximum:
                raise AdapterError("WhatsApp media exceeds the size limit")
    except requests.RequestException:
        raise TransientAdapterError("WhatsApp media stream was interrupted") from None
    if not content:
        raise AdapterError("WhatsApp media is empty")
    return bytes(content)


def _mime_type(kind, response, *fallbacks):
    for value in (_header(response.headers, "Content-Type"), *fallbacks):
        value = (value or "").split(";", 1)[0].strip().lower()
        if value and value != "application/octet-stream":
            break
    else:
        value = ""
    if not value or len(value) > 255 or any(c.isspace() for c in value):
        raise AdapterError("WhatsApp media MIME type is invalid")
    if kind != "document" and not value.startswith("%s/" % kind):
        raise AdapterError("WhatsApp media MIME type does not match its kind")
    return value


def _file_name(media, mime_type):
    value = str(media.file_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    value = _SAFE_FILENAME_RE.sub("_", value).strip(" .")
    if not value:
        value = "%s%s" % (
            media.external_media_id or "whatsapp-media",
            mimetypes.guess_extension(mime_type) or ".bin",
        )
    return value[:255]


def download_media(connection, media):
    media_id = _media_id(media)
    maximum = receive_limit(media.kind)
    runtime, token, _revision = resolve_graph_runtime(connection)
    for attempt in range(2):
        resolved = _resolve_media(runtime, token, media_id)
        if resolved["file_size"] > maximum:
            raise AdapterError("WhatsApp media exceeds the size limit")
        response = _request(resolved["url"], resolved["hostname"], token)
        try:
            status = response.status_code
            if status in _EXPIRED_STATUSES:
                # The five-minute URL expired between resolution and download.
                # Resolve the media ID once more; a media object that no longer
                # exists fails permanently at the Graph read above.
                if attempt == 0:
                    continue
                raise TransientAdapterError("WhatsApp media URL expired again")
            if status in (408, 425, 429) or status >= 500:
                error = TransientAdapterError("WhatsApp media host is unavailable")
                error.retry_after_seconds = _retry_after(response)
                raise error
            if not 200 <= status < 300:
                raise AdapterError("WhatsApp media host rejected the download")
            mime_type = _mime_type(
                media.kind, response, resolved["mime_type"], media.mime_type
            )
            content = _bounded_content(response, maximum)
        finally:
            response.close()
        digest = hashlib.sha256(content).hexdigest()
        for expected in (media.sha256, resolved["sha256"]):
            if expected and expected.lower() != digest:
                raise AdapterError("WhatsApp media checksum does not match")
        return MediaDownloadResult(
            content=content,
            mime_type=mime_type,
            file_name=_file_name(media, mime_type),
        )
    raise TransientAdapterError("WhatsApp media URL expired again")


def outbound_media_content(env, connection, command):
    """Resolve the private attachment owned by this outbound message only."""

    if len(command.message.media) != 1:
        raise AdapterError("WhatsApp sends exactly one media object per message")
    media = command.message.media[0]
    formats = SEND_FORMATS.get(media.kind)
    if not formats or command.message.content_type != media.kind or media.is_voice_note:
        raise AdapterError("WhatsApp outbound media kind is unsupported")
    if media.mime_type not in formats:
        raise AdapterError("WhatsApp outbound media MIME type is unsupported")
    if not 0 < media.size_bytes <= send_limit(media.kind):
        raise AdapterError("WhatsApp outbound media exceeds the provider size limit")
    caption = command.message.text or ""
    if caption and (
        media.kind not in CAPTION_KINDS or len(caption) > MAX_CAPTION_CHARACTERS
    ):
        raise AdapterError("WhatsApp outbound caption is not accepted for this media")
    locator = media.remote_locator
    attachment_id = locator.get("attachment_id") if isinstance(locator, dict) else None
    if (
        isinstance(attachment_id, bool)
        or not isinstance(attachment_id, int)
        or attachment_id <= 0
    ):
        raise AdapterError("WhatsApp outbound media requires a private attachment")
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
                ("message_binding_id.origin", "in", ("agent", "automation")),
            ],
            limit=2,
        )
    )
    if (
        len(rows) != 1
        or rows.state != "ready"
        or rows._as_dto().to_dict() != media.to_dict()
    ):
        raise AdapterError("WhatsApp outbound attachment has no exact ownership")
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
        raise AdapterError("WhatsApp outbound attachment evidence is inconsistent")
    content = attachment.raw
    if isinstance(content, memoryview):
        content = content.tobytes()
    if not isinstance(content, bytes) or len(content) != media.size_bytes:
        raise AdapterError("WhatsApp outbound attachment size does not match")
    if hashlib.sha256(content).hexdigest() != media.sha256:
        raise AdapterError("WhatsApp outbound attachment hash does not match")
    extension = (mimetypes.guess_extension(media.mime_type) or ".bin").lstrip(".")
    descriptor = {
        "content_omitted": True,
        "attachment_id": attachment.id,
        "mime_type": media.mime_type,
        # ASCII multipart name; the original name stays in Odoo.
        "file_name": "media-%s.%s" % (media.sha256[:20], extension),
        "document_name": _file_name(media, media.mime_type)
        if media.kind == "document"
        else "",
        "size_bytes": len(content),
        "sha256": media.sha256,
    }
    return media, content, descriptor


def upload_media(runtime, token, phone_number_id, media, content, descriptor):
    """Upload one private file; retrying an uncertain upload cannot duplicate a
    recipient-visible message (the orphan media expires after 30 days)."""

    try:
        response = graph_request(
            runtime,
            token,
            "POST",
            "%s/media" % phone_number_id,
            data={"messaging_product": "whatsapp", "type": media.mime_type},
            files={"file": (descriptor["file_name"], content, media.mime_type)},
            mutating=True,
            max_response_bytes=16 * 1024,
        )
    except AmbiguousTimeoutError as error:
        raise TransientAdapterError("WhatsApp media upload needs a retry") from error
    media_id = response.get("id") if isinstance(response, dict) else None
    if not isinstance(media_id, str) or not _MEDIA_ID_RE.fullmatch(media_id):
        raise TransientAdapterError("WhatsApp media upload returned no media ID")
    return media_id
