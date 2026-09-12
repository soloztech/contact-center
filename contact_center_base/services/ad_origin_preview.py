"""Bounded presentation values and private, DNS-pinned ad thumbnail downloads."""

import io
import re
import time
import uuid
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

import requests
import urllib3
from PIL import Image, ImageOps, UnidentifiedImageError

from .link_preview import public_url_target

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
PUBLIC_HOSTS = frozenset(
    (
        "instagram.com",
        "www.instagram.com",
        "m.instagram.com",
        "facebook.com",
        "www.facebook.com",
        "m.facebook.com",
        "fb.me",
        "www.fb.me",
    )
)
PUBLIC_QUERY_KEYS = frozenset(("id", "story_fbid", "fbid", "v"))
CDN_SUFFIXES = ("fbcdn.net", "cdninstagram.com", "fbsbx.com", "whatsapp.net")
CREATIVE_KEYS = frozenset(
    ("media_type", "title", "body", "public_url", "thumbnail_ref")
)


class PreviewError(Exception):
    def __init__(self, code="download_failed", *, retryable=False):
        self.code = (
            code
            if code
            in {
                "download_failed",
                "invalid_image",
                "image_too_large",
                "invalid_url",
                "unavailable",
                "expired",
                "invalid_scope",
            }
            else "unavailable"
        )
        self.retryable = bool(retryable)
        super().__init__(self.code)


def clean_text(value, limit):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    return value.encode("utf-8", "ignore").decode("utf-8").strip()[:limit]


def _url_parts(value, maximum=8192):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        or "\\" in value
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.port not in (None, 443)
        ):
            return None
        return parsed
    except ValueError:
        return None


def public_source_url(value):
    parsed = _url_parts(value, maximum=2048)
    if not parsed or parsed.hostname not in PUBLIC_HOSTS:
        return ""
    first_segment = unquote(parsed.path).lstrip("/").split("/", 1)[0].lower()
    if first_segment in {
        "direct",
        "accounts",
        "messages",
        "login",
        "settings",
        "dialog",
        "adsmanager",
    }:
        return ""
    try:
        values = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return ""
    if any(
        key not in PUBLIC_QUERY_KEYS
        or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", val)
        for key, val in values
    ):
        return ""
    return value


def thumbnail_url(value):
    parsed = _url_parts(value)
    if not parsed or not any(
        parsed.hostname == domain or parsed.hostname.endswith("." + domain)
        for domain in CDN_SUFFIXES
    ):
        return ""
    return value


def normalize_creative(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for name, limit in (("title", 256), ("body", 2000), ("media_type", 32)):
        text = clean_text(value.get(name), limit)
        if text:
            result[name] = text
    source = public_source_url(value.get("public_url"))
    if source:
        result["public_url"] = source
    try:
        reference = value.get("thumbnail_ref")
        if isinstance(reference, str) and str(uuid.UUID(reference)) == reference:
            result["thumbnail_ref"] = reference
    except (ValueError, AttributeError):
        # Invalid references contribute no public presentation data.
        return result
    return result


def image_derivative(content):
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise PreviewError("image_too_large")
    try:
        with Image.open(io.BytesIO(content)) as original:
            if original.format not in {"JPEG", "PNG", "WEBP"}:
                raise PreviewError("invalid_image")
            if original.width * original.height > MAX_IMAGE_PIXELS:
                raise PreviewError("image_too_large")
            original.load()
            picture = ImageOps.exif_transpose(original).convert("RGB")
            picture.thumbnail((640, 360), getattr(Image, "Resampling", Image).LANCZOS)
            output = io.BytesIO()
            picture.save(output, format="JPEG", quality=85)
            return output.getvalue(), picture.width, picture.height
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        raise PreviewError("invalid_image") from None


def fetch_thumbnail(url):
    """Never forward API credentials, environment proxies, cookies or raw errors."""
    deadline = time.monotonic() + 12
    try:
        for _redirect in range(4):
            if not thumbnail_url(url):
                raise PreviewError("invalid_url")
            parsed, port, address = public_url_target(url)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PreviewError("download_failed", retryable=True)
            pool = urllib3.HTTPSConnectionPool(
                address,
                port=port,
                maxsize=1,
                server_hostname=parsed.hostname,
                assert_hostname=parsed.hostname,
                cert_reqs="CERT_REQUIRED",
            )
            response = None
            try:
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                response = pool.urlopen(
                    "GET",
                    path,
                    headers={
                        "Host": parsed.netloc,
                        "Accept": "image/jpeg,image/png,image/webp",
                        "Accept-Encoding": "identity",
                        "User-Agent": "OdooAdPreview/1.0",
                    },
                    redirect=False,
                    retries=False,
                    preload_content=False,
                    timeout=urllib3.Timeout(
                        connect=min(3, remaining), read=min(3, remaining)
                    ),
                )
                if response.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                if response.status != 200:
                    raise PreviewError(
                        "download_failed",
                        retryable=response.status == 429 or response.status >= 500,
                    )
                if (
                    response.headers.get("Content-Encoding", "identity").lower()
                    != "identity"
                ):
                    raise PreviewError("invalid_image")
                length = response.headers.get("Content-Length", "")
                if length.isdigit() and int(length) > MAX_IMAGE_BYTES:
                    raise PreviewError("image_too_large")
                chunks, size = [], 0
                while True:
                    chunk = response.read(16 * 1024, decode_content=False)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise PreviewError("image_too_large")
                    if time.monotonic() > deadline:
                        raise PreviewError("download_failed", retryable=True)
                    chunks.append(chunk)
                return image_derivative(b"".join(chunks))
            finally:
                if response is not None:
                    response.close()
                pool.close()
    except (requests.RequestException, urllib3.exceptions.HTTPError, OSError):
        raise PreviewError("download_failed", retryable=True) from None
    raise PreviewError("download_failed")
