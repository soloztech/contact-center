import hashlib
import mimetypes
import re
from urllib.parse import urljoin, urlsplit

import requests

from odoo.exceptions import ValidationError

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    TransientAdapterError,
)
from odoo.addons.contact_center_base.services.dto import (
    AvatarResult,
    MediaDownloadResult,
)

from .tokens import CONTACT_CENTER_META_INTERNAL_TOKEN

_DOWNLOAD_TIMEOUT = (5, 30)
_MAX_REDIRECTS = 3
_MAX_MEDIA_BYTES = {
    "image": 8 * 1024 * 1024,
    "audio": 16 * 1024 * 1024,
    "video": 25 * 1024 * 1024,
    "document": 25 * 1024 * 1024,
}
_MAX_AVATAR_BYTES = 2 * 1024 * 1024
_AVATAR_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
_META_MEDIA_HOST_SUFFIXES = (
    "facebook.com",
    "fbcdn.net",
    "fbsbx.com",
    "instagram.com",
    "cdninstagram.com",
)
_SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._() -]+")


def _header(headers, name):
    target = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == target:
            return str(value or "").strip()
    return ""


def _retry_after(response):
    raw_value = _header(getattr(response, "headers", {}), "Retry-After")
    try:
        return max(0, min(int(raw_value), 3600))
    except (TypeError, ValueError):
        return 0


def _validated_meta_media_url(value):
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise AdapterError("Meta media locator URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise AdapterError("Meta media locator URL is invalid") from None
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
            for suffix in _META_MEDIA_HOST_SUFFIXES
        )
    ):
        raise AdapterError("Meta media locator host is not allowed")
    return value


def _safe_file_name(media, mime_type):
    value = str(media.file_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    value = _SAFE_FILENAME_PATTERN.sub("_", value).strip(" .")
    if not value:
        extension = mimetypes.guess_extension(mime_type) or ".bin"
        value = "%s%s" % (
            media.external_media_id
            or hashlib.sha256(mime_type.encode()).hexdigest()[:16],
            extension,
        )
    return value[:255]


def _validated_mime_type(kind, response, fallback):
    value = _header(getattr(response, "headers", {}), "Content-Type") or fallback
    value = value.split(";", 1)[0].strip().lower()
    if not value or len(value) > 255 or any(character.isspace() for character in value):
        raise AdapterError("Meta media response MIME type is invalid")
    if kind != "document" and not value.startswith("%s/" % kind):
        raise AdapterError("Meta media response MIME type does not match its kind")
    if kind == "document" and value != "application/pdf":
        raise AdapterError("Meta document media is limited to PDF")
    return value


def _raise_http_error(response):
    status = int(getattr(response, "status_code", 0) or 0)
    if status in (408, 425, 429) or status >= 500:
        error = TransientAdapterError(
            "Meta media endpoint returned a transient HTTP error"
        )
        error.retry_after_seconds = _retry_after(response)
        raise error
    if status in (401, 403, 404, 410):
        raise AdapterError("Meta media locator is unavailable or expired")
    raise AdapterError("Meta media endpoint rejected the download")


def _request_media(url):
    current_url = _validated_meta_media_url(url)
    for redirect_count in range(_MAX_REDIRECTS + 1):
        try:
            response = requests.request(
                "GET",
                current_url,
                headers={"Accept": "*/*"},
                timeout=_DOWNLOAD_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException:
            raise TransientAdapterError("Meta media endpoint did not respond") from None
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        try:
            location = _header(response.headers, "Location")
            if not location or redirect_count >= _MAX_REDIRECTS:
                raise AdapterError("Meta media redirect chain is invalid")
            current_url = _validated_meta_media_url(urljoin(current_url, location))
        finally:
            response.close()
    raise AdapterError("Meta media redirect chain is invalid")


def _bounded_content(response, maximum):
    declared = _header(getattr(response, "headers", {}), "Content-Length")
    if declared:
        try:
            declared_size = int(declared)
        except ValueError:
            declared_size = 0
        if declared_size > maximum:
            raise AdapterError("Meta media response exceeds the size limit")
    content = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > maximum:
                raise AdapterError("Meta media response exceeds the size limit")
    except requests.RequestException:
        raise TransientAdapterError(
            "Meta media response stream was interrupted"
        ) from None
    if not content:
        raise AdapterError("Meta media response is empty")
    return bytes(content)


def download_private_media(env, connection, media):
    """Resolve, consume and return one private Meta media object.

    Signed CDN URLs stay in the provider-private vault.  Neither errors nor result
    objects disclose the URL, tokenized query string or vault record identifier.
    """

    locator_values = media.remote_locator
    if not isinstance(locator_values, dict) or set(locator_values) != {
        "private_locator_ref"
    }:
        raise AdapterError("Meta media locator reference is invalid")
    reference = locator_values.get("private_locator_ref")
    try:
        locator = (
            env["contact.center.meta.media.locator"]
            .sudo()
            .with_context(
                contact_center_meta_internal=CONTACT_CENTER_META_INTERNAL_TOKEN
            )
            ._resolve_for_download(connection, reference)
        )
    except ValidationError as error:
        # Keep the provider boundary generic.  Callers must not learn whether a
        # locator exists under another connection or merely expired.
        raise AdapterError("Meta media locator reference is unavailable") from error
    maximum = _MAX_MEDIA_BYTES.get(media.kind)
    if not maximum:
        raise AdapterError("Meta media kind is unsupported")
    response = _request_media(locator.download_url)
    try:
        if not 200 <= response.status_code < 300:
            _raise_http_error(response)
        mime_type = _validated_mime_type(media.kind, response, media.mime_type)
        content = _bounded_content(response, maximum)
    finally:
        response.close()
    return MediaDownloadResult(
        content=content,
        mime_type=mime_type,
        file_name=_safe_file_name(media, mime_type),
    )


def finalize_private_media(env, connection, media, *, succeeded):
    locator_values = media.remote_locator
    if not isinstance(locator_values, dict):
        return None
    reference = locator_values.get("private_locator_ref")
    if not reference:
        return None
    # A failed binding remains manually retryable in the core. Keep its private
    # locator available only until the vault's short local retention deadline; the
    # cleanup cron clears it even when no retry occurs.
    if not succeeded:
        return None
    locator_model = (
        env["contact.center.meta.media.locator"]
        .sudo()
        .with_context(contact_center_meta_internal=CONTACT_CENTER_META_INTERNAL_TOKEN)
    )
    try:
        locator = locator_model._resolve_for_download(connection, reference)
    except ValidationError:
        # Finalization is idempotent: an already consumed/expired locator is done.
        return None
    locator._finalize(succeeded=bool(succeeded))
    return None


def download_profile_avatar(url, *, provider_revision=""):
    response = _request_media(url)
    try:
        if not 200 <= response.status_code < 300:
            _raise_http_error(response)
        mime_type = _validated_mime_type("image", response, "")
        if mime_type not in _AVATAR_MIME_TYPES:
            raise AdapterError("Meta profile image MIME type is unsupported")
        content = _bounded_content(response, _MAX_AVATAR_BYTES)
    finally:
        response.close()
    return AvatarResult(
        state="ready",
        provider_revision=provider_revision,
        content=content,
        mime_type=mime_type,
        file_name="meta-profile%s" % (mimetypes.guess_extension(mime_type) or ".bin"),
    )
