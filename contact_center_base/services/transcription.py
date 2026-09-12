"""Provider-independent speech transcription contracts and HTTP adapters.

No Odoo records, queues, credentials lookup, or audio conversion belongs here.
The compatible adapter expects an explicitly administered HTTP service exposing
the OpenAI multipart transcription contract; faster-whisper itself is a library.
"""

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import requests

MAX_AUDIO_BYTES = 25_000_000
MAX_RESPONSE_BYTES = 1_048_576
MAX_TEXT_CHARS = 100_000
MAX_PROMPT_CHARS = 2_000
OPENAI_BASE_URL = "https://api.openai.com/v1"

_FORMAT_BY_MIME = {
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/ogg": "ogg",
    "application/ogg": "ogg",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/x-wav": "wav",
    "audio/vnd.wave": "wav",
    "audio/webm": "webm",
    "video/webm": "webm",
    "video/mp4": "mp4",
    "video/mpeg": "mpeg",
}
_COMPATIBLE_FORMATS = {
    **_FORMAT_BY_MIME,
    "audio/aac": "aac",
    "audio/x-aac": "aac",
    "audio/amr": "amr",
    "audio/opus": "opus",
}
_ERROR_CODES = frozenset(
    {
        "invalid_config",
        "invalid_request",
        "audio_too_large",
        "empty_audio",
        "unsupported_audio_format",
        "invalid_credentials",
        "access_denied",
        "rate_limited",
        "provider_unavailable",
        "timeout",
        "connection_error",
        "tls_error",
        "redirect_blocked",
        "model_or_endpoint_not_found",
        "invalid_response",
        "response_too_large",
        "no_speech",
        "provider_error",
        "unknown_provider",
    }
)


@dataclass(frozen=True)
class TranscriptionRequest:
    content: bytes = field(repr=False)
    filename: str = field(repr=False)
    mime_type: str
    language: str = ""
    prompt: str = field(default="", repr=False)


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str = field(repr=False)
    model: str
    api_key: str = field(default="", repr=False)
    timeout_seconds: int = 60


@dataclass(frozen=True)
class TranscriptionResult:
    text: str = field(repr=False)
    language: str = ""


class TranscriptionError(Exception):
    """Only safe symbolic codes cross the provider boundary or reach job logs."""

    def __init__(self, code, retryable=False, retry_after_seconds=0):
        self.code = code if code in _ERROR_CODES else "provider_error"
        self.retryable = bool(retryable)
        self.retry_after_seconds = min(3600, max(0, int(retry_after_seconds)))
        super().__init__(self.code)


def normalize_base_url(base_url, *, openai=False):
    """Validate an admin-selected URL, allowing explicit LAN HTTP services."""
    if openai and not base_url:
        return OPENAI_BASE_URL
    if (
        not isinstance(base_url, str)
        or not base_url
        or len(base_url) > 2048
        or any(ord(char) <= 32 or ord(char) == 127 for char in base_url)
        or "\\" in base_url
        or "?" in base_url
        or "#" in base_url
    ):
        raise TranscriptionError("invalid_config")
    try:
        parts = urlsplit(base_url)
        valid = (
            parts.scheme in {"http", "https"}
            and parts.hostname
            and parts.username is None
            and parts.password is None
            and (parts.port is None or 1 <= parts.port <= 65535)
        )
    except ValueError:
        raise TranscriptionError("invalid_config") from None
    if not valid:
        raise TranscriptionError("invalid_config")
    result = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    if openai and result != OPENAI_BASE_URL:
        raise TranscriptionError("invalid_config")
    return result


class TranscriptionAdapter:
    """Register a subclass to add another engine without changing chat or jobs."""

    def validate_config(self, config):
        if (
            not isinstance(config, ProviderConfig)
            or not isinstance(config.model, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", config.model)
            or type(config.timeout_seconds) is not int
            or not 5 <= config.timeout_seconds <= 300
            or not isinstance(config.api_key, str)
            or len(config.api_key) > 4096
            or any(ord(char) <= 32 or ord(char) >= 127 for char in config.api_key)
        ):
            raise TranscriptionError("invalid_config")

    def transcribe(self, config, request):
        raise NotImplementedError


class TranscriptionRegistry:
    def __init__(self):
        self._adapters = {}
        self._labels = {}
        self._lock = threading.RLock()

    def register(self, key, adapter=None, label=None):
        def decorator(candidate):
            if not isinstance(candidate, type) or not issubclass(
                candidate, TranscriptionAdapter
            ):
                raise TypeError("adapter must inherit TranscriptionAdapter")
            if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_.]*", key):
                raise ValueError("invalid transcription adapter key")
            if label is not None and (not isinstance(label, str) or not label.strip()):
                raise ValueError("invalid transcription adapter label")
            with self._lock:
                if key in self._adapters and self._adapters[key] is not candidate:
                    raise ValueError("transcription adapter key already registered")
                self._adapters[key] = candidate
                self._labels[key] = label or key
            return candidate

        return decorator(adapter) if adapter is not None else decorator

    def get(self, key):
        with self._lock:
            try:
                return self._adapters[key]
            except KeyError:
                raise TranscriptionError("unknown_provider") from None

    def selection(self):
        with self._lock:
            return [(key, self._labels[key]) for key in sorted(self._adapters)]


transcription_registry = TranscriptionRegistry()


def _audio_metadata(request, formats):
    if not isinstance(request, TranscriptionRequest):
        raise TranscriptionError("invalid_request")
    if not isinstance(request.content, bytes):
        raise TranscriptionError("invalid_request")
    if not request.content:
        raise TranscriptionError("empty_audio")
    if len(request.content) > MAX_AUDIO_BYTES:
        raise TranscriptionError("audio_too_large")
    if (
        not isinstance(request.prompt, str)
        or len(request.prompt) > MAX_PROMPT_CHARS
        or "\x00" in request.prompt
        or any(0xD800 <= ord(char) <= 0xDFFF for char in request.prompt)
        or not isinstance(request.language, str)
        or (request.language and not re.fullmatch(r"[a-z]{2}", request.language))
        or not isinstance(request.filename, str)
        or len(request.filename) > 1024
        or not isinstance(request.mime_type, str)
        or len(request.mime_type) > 256
    ):
        raise TranscriptionError("invalid_request")
    mime = request.mime_type.split(";", 1)[0].strip().lower()
    extension = formats.get(mime)
    if not extension:
        raise TranscriptionError("unsupported_audio_format")
    # Send enough format metadata, without disclosing the client's original name.
    # Renaming .oga to .ogg does not convert bytes; both are the Ogg container.
    return "audio." + extension, mime


def _retry_after(value):
    if not isinstance(value, str) or len(value) > 128:
        return 0
    try:
        seconds = int(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = int((date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0
    return min(3600, max(0, seconds))


def _check_status(response):
    status = response.status_code
    if status == 200:
        return
    if status == 401:
        raise TranscriptionError("invalid_credentials")
    if status == 403:
        raise TranscriptionError("access_denied")
    if status == 408:
        raise TranscriptionError("timeout", retryable=True)
    if status == 429 or 500 <= status <= 599:
        raise TranscriptionError(
            "rate_limited" if status == 429 else "provider_unavailable",
            retryable=True,
            retry_after_seconds=_retry_after(response.headers.get("Retry-After")),
        )
    code = {
        400: "invalid_request",
        404: "model_or_endpoint_not_found",
        413: "audio_too_large",
        415: "unsupported_audio_format",
        422: "invalid_request",
    }.get(status, "redirect_blocked" if 300 <= status < 400 else "provider_error")
    # Do not read failed bodies: they may echo the audio, credentials, or prompt.
    raise TranscriptionError(code)


def _parse_response(response, deadline):
    size_header = response.headers.get("Content-Length", "")
    if size_header.isdigit():
        normalized_size = size_header.lstrip("0") or "0"
        if len(normalized_size) > 8 or int(normalized_size) > MAX_RESPONSE_BYTES:
            raise TranscriptionError("response_too_large")
    content = bytearray()
    for chunk in response.iter_content(chunk_size=16_384):
        if time.monotonic() > deadline:
            raise TranscriptionError("timeout", retryable=True)
        if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
            raise TranscriptionError("response_too_large")
        content.extend(chunk)
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeError, RecursionError):
        raise TranscriptionError("invalid_response") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise TranscriptionError("invalid_response")
    text = payload["text"]
    if len(text) > MAX_TEXT_CHARS:
        raise TranscriptionError("response_too_large")
    if "\x00" in text or any(0xD800 <= ord(char) <= 0xDFFF for char in text):
        raise TranscriptionError("invalid_response")
    if not text.strip():
        raise TranscriptionError("no_speech")
    language = payload.get("language", "")
    languages = payload.get("languages")
    if isinstance(languages, list) and len(languages) == 1:
        if isinstance(languages[0], dict):
            language = languages[0].get("code", "")
    if not isinstance(language, str) or not re.fullmatch(r"[a-z]{2}", language):
        language = ""
    return TranscriptionResult(text=text.strip(), language=language)


class OpenAICompatibleAdapter(TranscriptionAdapter):
    """Multipart /audio/transcriptions for an administrator-selected service."""

    openai = False

    def validate_config(self, config):
        result = super().validate_config(config)
        normalize_base_url(config.base_url, openai=self.openai)
        if self.openai and not config.api_key:
            raise TranscriptionError("invalid_credentials")
        return result

    def transcribe(self, config, request):
        self.validate_config(config)
        filename, mime = _audio_metadata(
            request, _FORMAT_BY_MIME if self.openai else _COMPATIBLE_FORMATS
        )
        url = normalize_base_url(config.base_url, openai=self.openai)
        data = [("model", config.model), ("response_format", "json")]
        # Current API: gpt-transcribe uses languages[], older engines language.
        # https://developers.openai.com/api/docs/guides/speech-to-text
        if request.language:
            field_name = (
                "languages[]"
                if config.model == "gpt-transcribe"
                or config.model.startswith("gpt-transcribe-")
                else "language"
            )
            data.append((field_name, request.language))
        if request.prompt:
            if config.model == "gpt-4o-transcribe-diarize":
                raise TranscriptionError("invalid_request")
            data.append(("prompt", request.prompt))
        if config.model == "gpt-4o-transcribe-diarize":
            data.append(("chunking_strategy", "auto"))
        headers = {"Accept": "application/json"}
        if config.api_key:
            headers["Authorization"] = "Bearer " + config.api_key
        deadline = time.monotonic() + config.timeout_seconds
        try:
            with requests.Session() as session:
                # Avoid implicit .netrc credentials and environment proxies.
                session.trust_env = False
                with session.post(
                    url + "/audio/transcriptions",
                    headers=headers,
                    files={"file": (filename, request.content, mime)},
                    data=data,
                    timeout=(min(10, config.timeout_seconds), config.timeout_seconds),
                    allow_redirects=False,
                    stream=True,
                ) as response:
                    _check_status(response)
                    return _parse_response(response, deadline)
        except requests.exceptions.Timeout:
            raise TranscriptionError("timeout", retryable=True) from None
        except requests.exceptions.SSLError:
            raise TranscriptionError("tls_error") from None
        except requests.exceptions.ConnectionError:
            raise TranscriptionError("connection_error", retryable=True) from None
        except requests.exceptions.RequestException:
            raise TranscriptionError("provider_error") from None


class OpenAIAdapter(OpenAICompatibleAdapter):
    """Official OpenAI endpoint; changing provider requires explicit selection."""

    openai = True


transcription_registry.register("openai", OpenAIAdapter, "OpenAI")
transcription_registry.register(
    "openai_compatible", OpenAICompatibleAdapter, "OpenAI-compatible service"
)
