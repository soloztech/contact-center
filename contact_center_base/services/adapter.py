import abc
import json
import math
import re
import threading
from typing import Dict, Optional, Type

from .dto import (
    AdapterResult,
    AddressDTO,
    AvatarResult,
    CommandDTO,
    EventDTO,
    GroupMetadataDTO,
    IdentityProfileResult,
    MediaDownloadResult,
    MediaDTO,
)


class AdapterError(Exception):
    """Base error raised by a provider adapter."""

    classification = "permanent"


class TransientAdapterError(AdapterError):
    classification = "transient"


class AmbiguousTimeoutError(AdapterError):
    classification = "uncertain"


class ProviderPausedError(AdapterError):
    classification = "paused"


class ProviderRateLimitError(ProviderPausedError):
    """Authoritative provider refusal that is safe to retry without a ceiling.

    A rate-limit response is observed after the network boundary, but proves that
    the provider did not accept the operation.  Keeping it distinct from generic
    transient failures lets queue ledgers refund the probing attempt while still
    sharing the provider's Retry-After deadline with sibling jobs.
    """

    classification = "rate_limited"


class UnsupportedEventError(AdapterError):
    classification = "unsupported"


_PROVIDER_REQUEST_FORBIDDEN_KEYS = {
    "authorization",
    "cookie",
    "headers",
    "hmac",
    "hmackey",
    "hmacsecret",
    "password",
    "proxyauthorization",
    "setcookie",
}
_PROVIDER_REQUEST_MAX_BYTES = 512 * 1024


def conversation_capabilities(capabilities: Dict, conversation_type: str) -> Dict:
    """Return the explicit provider capability scope for one conversation type.

    Existing top-level capabilities describe direct conversations. Other conversation
    types require an additive ``conversation_types`` scope so installing a
    direct-capable adapter can never enable group outbound by accident.
    """

    if not isinstance(capabilities, dict):
        return {}
    if conversation_type == "direct":
        return {
            key: value
            for key, value in capabilities.items()
            if key != "conversation_types"
        }
    scoped_capabilities = capabilities.get("conversation_types")
    if not isinstance(scoped_capabilities, dict):
        return {}
    scoped = scoped_capabilities.get(conversation_type)
    if isinstance(scoped, dict):
        return dict(scoped)
    return {}


def validate_provider_request_snapshot(snapshot: Dict) -> Dict:
    """Return a JSON-safe audit snapshot after rejecting credential/binary leaks."""

    if not isinstance(snapshot, dict) or not snapshot:
        raise AdapterError("provider request snapshot must be a non-empty object")

    def validate(value, path="request"):
        if isinstance(value, dict):
            safe = {}
            for key, child in value.items():
                if not isinstance(key, str) or not key:
                    raise AdapterError(
                        "provider request snapshot keys must be non-empty strings"
                    )
                normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
                field_path = "%s.%s" % (path, key)
                if (
                    normalized_key in _PROVIDER_REQUEST_FORBIDDEN_KEYS
                    or normalized_key.endswith(
                        (
                            "apikey",
                            "credential",
                            "credentials",
                            "password",
                            "privatekey",
                            "secret",
                            "token",
                        )
                    )
                    or "authorization" in normalized_key
                    or "base64" in normalized_key
                    or "binary" in normalized_key
                ):
                    raise AdapterError(
                        "provider request snapshot contains forbidden field: %s"
                        % field_path
                    )
                safe[key] = validate(child, field_path)
            return safe
        if isinstance(value, (list, tuple)):
            return [
                validate(child, "%s[%s]" % (path, index))
                for index, child in enumerate(value)
            ]
        if isinstance(value, bytes):
            raise AdapterError("provider request snapshot cannot contain binary data")
        if isinstance(value, float) and not math.isfinite(value):
            raise AdapterError("provider request snapshot contains a non-finite number")
        if isinstance(value, str):
            normalized_value = value.lstrip().lower()
            if normalized_value.startswith("data:") and ";base64," in normalized_value:
                raise AdapterError(
                    "provider request snapshot cannot contain a base64 data URI"
                )
            return value
        if value is None or isinstance(value, (bool, int, float)):
            return value
        raise AdapterError(
            "provider request snapshot contains a non-JSON value at %s" % path
        )

    safe_snapshot = validate(snapshot)
    try:
        serialized = json.dumps(
            safe_snapshot,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise AdapterError("provider request snapshot is not valid JSON") from error
    if len(serialized) > _PROVIDER_REQUEST_MAX_BYTES:
        raise AdapterError("provider request snapshot exceeds the audit size limit")
    return safe_snapshot


class ProviderAdapter(abc.ABC):
    """Provider boundary. Adapters may translate and transport, never mutate domain records."""

    key = None
    display_name = None

    def __init__(self, env):
        self.env = env

    @abc.abstractmethod
    def authenticate_webhook(self, connection, headers, body) -> bool:
        """Validate a webhook without persisting authentication material."""

    @abc.abstractmethod
    def normalize_event(self, connection, envelope) -> EventDTO:
        """Translate a provider envelope to EventDTO v1."""

    @abc.abstractmethod
    def execute_command(self, connection, command: CommandDTO) -> AdapterResult:
        """Execute one already-persisted outbound command."""

    @abc.abstractmethod
    def prepare_request_snapshot(self, connection, command: CommandDTO) -> Dict:
        """Build the provider request audit view without performing provider I/O.

        The returned object must describe the exact method, endpoint and safe payload
        that will be dispatched.  It must never contain credentials, headers, binary
        content or base64 media.  Core validates and durably persists this snapshot
        before calling :meth:`execute_command`.
        """

    @abc.abstractmethod
    def get_capabilities(self, connection) -> Dict:
        """Return provider/platform capabilities for this connection."""

    @abc.abstractmethod
    def get_health(self, connection) -> Dict:
        """Return connection health without changing core domain records."""

    def derive_client_message_id(self, command_id: str) -> Optional[str]:
        """Pure optional correlation hook called before any provider I/O."""

        return None

    def outbound_min_interval_seconds(self, connection) -> int:
        """Return the minimum spacing between dispatch starts on one connection.

        The hook must be pure and return an integer from zero through 300.  Core
        serializes and durably reserves the next dispatch instant before provider
        I/O.  Zero keeps the provider unpaced while still honoring shared
        ``Retry-After`` cooldowns.
        """

        return 0

    def matches_outbound_mutation_echo(
        self, connection, command: CommandDTO, event_mutation: Dict
    ) -> bool:
        """Return whether a self-side provider mutation confirms ``command``.

        Providers that transform outbound content (for example, by adding
        platform markup) must override this pure comparison.  The core then
        reuses the clean outbound mutation ledger instead of projecting the
        provider's wire representation into ``mail.message``.
        """

        return False

    def validate_outbound_signature(
        self, connection, text: str, sender_signature: Dict
    ) -> bool:
        """Validate provider-neutral signature semantics without provider I/O."""

        if (
            not isinstance(text, str)
            or not text
            or not isinstance(sender_signature, dict)
            or set(sender_signature) != {"display_name"}
        ):
            raise AdapterError("outbound sender signature is invalid")
        display_name = sender_signature.get("display_name")
        if (
            not isinstance(display_name, str)
            or not display_name
            or len(display_name) > 120
            or display_name != " ".join(display_name.split())
        ):
            raise AdapterError("outbound sender signature is invalid")
        return True

    def validate_recorded_audio_upload(
        self,
        connection,
        *,
        content: bytes,
        mimetype: str,
        is_voice_note: bool,
        duration_seconds: int,
    ) -> bool:
        """Pure provider-specific validation after the core capability checks."""

        return True

    def prepare_reply_reference(
        self,
        connection,
        *,
        conversation_type: str,
        external_message_id: str,
        protocol_snapshot: Dict,
        protocol_participant: Dict,
    ) -> Dict:
        """Return the provider-neutral reply reference accepted by the adapter.

        Direct-message adapters keep the historical snapshot contract. Group replies
        are fail-closed because providers commonly require an exact participant in
        addition to the target message ID; an adapter must opt in and validate that
        representation explicitly.
        """

        if not isinstance(external_message_id, str) or not external_message_id.strip():
            raise AdapterError("reply target requires a provider message ID")
        if conversation_type != "direct":
            raise AdapterError(
                "%s does not implement group reply references" % self.key
            )
        return {
            "external_message_id": external_message_id.strip(),
            "protocol_snapshot": (
                dict(protocol_snapshot) if isinstance(protocol_snapshot, dict) else {}
            ),
        }

    def download_media(self, connection, media: MediaDTO) -> MediaDownloadResult:
        """Download one inbound media object without mutating Odoo records."""

        raise UnsupportedEventError(
            "%s does not implement asynchronous media download" % self.key
        )

    def finalize_media_download(
        self, connection, media: MediaDTO, *, succeeded: bool
    ) -> None:
        """Release provider-private locator material after a terminal outcome.

        Most providers keep all required evidence inside the normalized locator and
        therefore need no cleanup.  Adapters backed by a short-lived private vault
        may override this hook; it must be idempotent and must not perform network
        I/O.
        """

        return None

    def fetch_group_metadata(
        self, connection, conversation_ref: str
    ) -> GroupMetadataDTO:
        """Fetch one complete group snapshot without mutating Odoo records."""

        raise UnsupportedEventError(
            "%s does not implement group metadata synchronization" % self.key
        )

    def fetch_group_avatar(self, connection, conversation_ref: str) -> AvatarResult:
        """Fetch a bounded avatar result without exposing a provider URL."""

        raise UnsupportedEventError(
            "%s does not implement group avatar synchronization" % self.key
        )

    def fetch_identity_avatar(self, connection, address: AddressDTO) -> AvatarResult:
        """Fetch one bounded direct-identity avatar without exposing a remote URL."""

        raise UnsupportedEventError(
            "%s does not implement identity avatar synchronization" % self.key
        )

    def fetch_identity_profile(
        self, connection, address: AddressDTO
    ) -> IdentityProfileResult:
        """Fetch one direct profile using the provider's unified profile contract."""

        raise UnsupportedEventError(
            "%s does not implement identity profile synchronization" % self.key
        )

    def supports_identity_profile(self, connection) -> bool:
        """Declare explicit unified profile support before core schedules work."""

        return False

    def is_provider_read_ready(self, connection, purpose: str) -> bool:
        """Return whether a provider read may run for the requested purpose.

        Active account/connection and identity-latch checks remain core invariants.
        The default preserves the historical connected-and-fresh read policy while a
        provider may explicitly support a narrower degraded read-only state.
        """

        return bool(
            connection.state == "connected"
            and connection._contact_center_observation_is_fresh()
        )


class AdapterRegistry:
    def __init__(self):
        self._adapters = {}
        self._owners = {}
        self._lock = threading.RLock()

    def register(
        self,
        key: str,
        adapter_class: Optional[Type[ProviderAdapter]] = None,
        module: Optional[str] = None,
    ):
        def decorator(candidate):
            if not isinstance(candidate, type) or not issubclass(
                candidate, ProviderAdapter
            ):
                raise TypeError("adapter must inherit ProviderAdapter")
            if not key or not isinstance(key, str):
                raise ValueError("adapter key must be a non-empty string")
            if module is not None and (
                not isinstance(module, str) or not module.strip()
            ):
                raise ValueError("adapter owner module must be a non-empty string")
            with self._lock:
                existing = self._adapters.get(key)
                if existing and existing is not candidate:
                    raise ValueError("adapter key already registered: %s" % key)
                candidate.key = key
                self._adapters[key] = candidate
                self._owners[key] = module
            return candidate

        if adapter_class is not None:
            return decorator(adapter_class)
        return decorator

    def unregister(self, key: str):
        with self._lock:
            self._owners.pop(key, None)
            return self._adapters.pop(key, None)

    def get(self, key: str) -> Type[ProviderAdapter]:
        with self._lock:
            try:
                return self._adapters[key]
            except KeyError as error:
                raise KeyError("unknown contact center adapter: %s" % key) from error

    def keys(self):
        with self._lock:
            return tuple(sorted(self._adapters))

    def choices(self):
        """Return provider choices exposed by installed adapter addons."""

        with self._lock:
            return tuple(
                (
                    key,
                    adapter_class.display_name
                    or key.replace("_", " ").replace(".", " ").title(),
                )
                for key, adapter_class in sorted(self._adapters.items())
            )

    def owner(self, key: str) -> Optional[str]:
        with self._lock:
            if key not in self._adapters:
                raise KeyError("unknown contact center adapter: %s" % key)
            return self._owners.get(key)


adapter_registry = AdapterRegistry()
