import dataclasses
import datetime
import hashlib
import json
import math
import re
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import urlsplit

from .structured_content import validate_structured_content

SCHEMA_VERSION = 1
ATTRIBUTION_SCHEMA_VERSION = 1

ADDRESS_ROLES = {
    "primary",
    "alternate",
    "sender",
    "recipient",
    "device",
    "group",
    "routing",
}
ADDRESS_CONFIDENCES = {"observed", "protocol", "manual"}
ADDRESS_RESOLUTION_SCOPES = {"account", "company"}
CONVERSATION_TYPES = {"direct", "group", "other"}
EVENT_DIRECTIONS = {"inbound", "outbound"}
EVENT_ORIGINS = {"provider", "agent", "automation", "external_device"}
COMMAND_TYPES = {
    "send_message",
    "mark_read",
    "react",
    "edit_message",
    "delete_message",
}
MEDIA_KINDS = {"image", "audio", "video", "document"}
MUTATION_TYPES = {"react", "edit", "delete"}
REACTION_OPERATIONS = {"add", "remove"}
GROUP_PARTICIPANT_ROLES = {"member", "admin", "superadmin"}
GROUP_OWN_ROLES = {"unknown", "member", "admin", "superadmin"}
AVATAR_STATES = {"ready", "absent", "unavailable"}
ATTRIBUTION_TOUCHPOINT_TYPES = {
    "paid_ad_click",
    "paid_ad_signal",
    "entry_point",
    "organic_link",
    "unknown",
}
ATTRIBUTION_EVIDENCE_LEVELS = {
    "provider_asserted",
    "provider_asserted_non_paid",
    "provider_hint",
    "observed",
    "derived",
}
ATTRIBUTION_UTM_KEYS = {"source", "medium", "campaign", "content", "term"}
ATTRIBUTION_ENTRY_POINT_KEYS = {
    "source",
    "app",
    "delay_seconds",
    "external_source",
    "external_medium",
    "conversion_source",
    "conversion_delay_seconds",
}
ATTRIBUTION_CREATIVE_KEYS = {"media_type"}
ATTRIBUTION_FLAG_KEYS = {
    "show_ad_attribution",
    "always_show_ad_attribution",
}
MAX_EVENT_ATTRIBUTIONS = 8
MAX_ATTRIBUTION_IDENTIFIERS = 32
MAX_ATTRIBUTION_DELAY_SECONDS = 2_147_483_647
MAX_MUTATION_PROVIDER_REVISION = 2_147_483_647
MAX_FORWARDING_SCORE = 2_147_483_647
MAX_ADDRESS_COUNT = 64
MAX_GROUP_PARTICIPANTS = 4096
MAX_MESSAGE_MEDIA = 16
MAX_MESSAGE_TEXT = 131_072
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ATTRIBUTION_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


class DTOValidationError(ValueError):
    """Raised when a payload does not satisfy the local DTO contract."""


def _is_json_primitive(value: Any, field_name: str) -> bool:
    """Recognize scalar JSON values while rejecting Python's NaN extension."""

    if isinstance(value, float):
        if not math.isfinite(value):
            raise DTOValidationError("%s contains a non-finite number" % field_name)
        return True
    return value is None or isinstance(value, (bool, int))


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DTOValidationError("%s must be a non-empty string" % field_name)
    return value.strip()


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise DTOValidationError("%s must be a string" % field_name)
    return value


def _require_dict(value: Any, field_name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise DTOValidationError("%s must be an object" % field_name)
    return value


def _bounded_string(
    value: Any,
    field_name: str,
    *,
    maximum: int,
    required: bool = False,
    reject_controls: bool = False,
) -> str:
    value = _require_string(value, field_name)
    if required and not value.strip():
        raise DTOValidationError("%s must be a non-empty string" % field_name)
    if len(value) > maximum:
        raise DTOValidationError("%s is too long" % field_name)
    if reject_controls and _CONTROL_CHARACTER_PATTERN.search(value):
        raise DTOValidationError("%s contains control characters" % field_name)
    return value


def _validate_bounded_json(
    value: Any,
    field_name: str,
    *,
    maximum_bytes: int,
    maximum_depth: int = 8,
) -> None:
    _require_dict(value, field_name)

    def validate(item: Any, depth: int) -> None:
        if depth > maximum_depth:
            raise DTOValidationError("%s is nested too deeply" % field_name)
        if _is_json_primitive(item, field_name):
            return
        if isinstance(item, str):
            if len(item) > maximum_bytes:
                raise DTOValidationError("%s contains an oversized string" % field_name)
            return
        if isinstance(item, dict):
            for key, nested in item.items():
                if not isinstance(key, str) or not key:
                    raise DTOValidationError(
                        "%s keys must be non-empty strings" % field_name
                    )
                if len(key) > 256 or _CONTROL_CHARACTER_PATTERN.search(key):
                    raise DTOValidationError("%s contains an invalid key" % field_name)
                validate(nested, depth + 1)
            return
        if isinstance(item, (list, tuple)):
            for nested in item:
                validate(nested, depth + 1)
            return
        raise DTOValidationError("%s must contain JSON values only" % field_name)

    validate(value, 0)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise DTOValidationError("%s must be JSON serializable" % field_name) from error
    if len(encoded) > maximum_bytes:
        raise DTOValidationError("%s is too large" % field_name)


def _payload_dict(value: Any, field_name: str) -> Dict[str, Any]:
    if value is None:
        return {}
    return dict(_require_dict(value, field_name))


def _as_tuple(values: Optional[Iterable[Any]], field_name: str) -> Tuple[Any, ...]:
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        raise DTOValidationError("%s must be an array" % field_name)
    return tuple(values)


def _parse_utc(value: Any) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise DTOValidationError(
                "occurred_at must be an ISO-8601 datetime"
            ) from error
    else:
        raise DTOValidationError("occurred_at must be an ISO-8601 datetime")
    if parsed.tzinfo is None:
        raise DTOValidationError("occurred_at must include a timezone")
    return parsed.astimezone(datetime.timezone.utc)


def _serialize(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _serialize(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, datetime.datetime):
        return (
            value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        )
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    return value


def _validate_json_locator(value: Any, field_name: str, depth: int = 0) -> None:
    """Reject binary/inline media while allowing provider download metadata."""

    if depth > 16:
        raise DTOValidationError("%s is nested too deeply" % field_name)
    if _is_json_primitive(value, field_name):
        return
    if isinstance(value, str):
        if value.lstrip().lower().startswith("data:"):
            raise DTOValidationError(
                "%s cannot contain an inline data URI" % field_name
            )
        if len(value) > 8192:
            raise DTOValidationError("%s contains an oversized string" % field_name)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise DTOValidationError(
                    "%s keys must be non-empty strings" % field_name
                )
            _validate_json_locator(item, field_name, depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_locator(item, field_name, depth + 1)
        return
    raise DTOValidationError("%s must contain JSON values only" % field_name)


def _bounded_attribution_string(
    value: Any,
    field_name: str,
    *,
    maximum: int,
    required: bool = False,
) -> str:
    value = _require_string(value, field_name)
    if required and not value.strip():
        raise DTOValidationError("%s must be a non-empty string" % field_name)
    if len(value) > maximum:
        raise DTOValidationError("%s is too long" % field_name)
    if _CONTROL_CHARACTER_PATTERN.search(value):
        raise DTOValidationError("%s contains control characters" % field_name)
    if value.lstrip().lower().startswith("data:"):
        raise DTOValidationError("%s cannot contain an inline data URI" % field_name)
    return value


def _attribution_key(value: Any, field_name: str) -> str:
    value = _bounded_attribution_string(value, field_name, maximum=128, required=True)
    if not _ATTRIBUTION_KEY_PATTERN.fullmatch(value):
        raise DTOValidationError("%s is not a canonical key" % field_name)
    return value


def _validate_attribution_json(
    value: Any, field_name: str, *, maximum_bytes: int, depth: int = 0
) -> None:
    if depth > 8:
        raise DTOValidationError("%s is nested too deeply" % field_name)
    if _is_json_primitive(value, field_name):
        return
    if isinstance(value, str):
        _bounded_attribution_string(value, field_name, maximum=4096)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _attribution_key(key, "%s key" % field_name)
            _validate_attribution_json(
                item,
                field_name,
                maximum_bytes=maximum_bytes,
                depth=depth + 1,
            )
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_attribution_json(
                item,
                field_name,
                maximum_bytes=maximum_bytes,
                depth=depth + 1,
            )
    else:
        raise DTOValidationError("%s must contain JSON values only" % field_name)
    if depth == 0:
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise DTOValidationError(
                "%s must be JSON serializable" % field_name
            ) from error
        if len(encoded) > maximum_bytes:
            raise DTOValidationError("%s is too large" % field_name)


def _validate_attribution_source(source_platform, source_type, source_url):
    for field_name, value in (
        ("source_platform", source_platform),
        ("source_type", source_type),
    ):
        if value:
            _attribution_key(value, "attribution.%s" % field_name)
        else:
            _require_string(value, "attribution.%s" % field_name)
    source_url = _bounded_attribution_string(
        source_url, "attribution.source_url", maximum=2048
    )
    if source_url:
        parsed = urlsplit(source_url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            raise DTOValidationError(
                "attribution.source_url must be an absolute HTTP(S) URL"
            )


def _validate_attribution_identifiers(identifiers):
    if not isinstance(identifiers, tuple) or any(
        not isinstance(item, ExternalIdentifierDTO) for item in identifiers
    ):
        raise DTOValidationError(
            "attribution.external_identifiers must contain ExternalIdentifierDTO values"
        )
    if len(identifiers) > MAX_ATTRIBUTION_IDENTIFIERS:
        raise DTOValidationError("attribution has too many external identifiers")
    identifier_keys = {
        (item.namespace, item.role, item.comparison_hash) for item in identifiers
    }
    if len(identifier_keys) != len(identifiers):
        raise DTOValidationError("attribution external identifiers must be unique")


def _validate_attribution_mapping(field_name, value, allowed_keys, maximum_bytes):
    _require_dict(value, "attribution.%s" % field_name)
    if set(value) - allowed_keys:
        raise DTOValidationError(
            "attribution.%s contains unsupported keys" % field_name
        )
    _validate_attribution_json(
        value,
        "attribution.%s" % field_name,
        maximum_bytes=maximum_bytes,
    )


def _validate_attribution_utm(utm):
    _validate_attribution_mapping("utm", utm, ATTRIBUTION_UTM_KEYS, 4096)
    if any(not isinstance(value, str) or len(value) > 512 for value in utm.values()):
        raise DTOValidationError("attribution.utm values must be bounded strings")


def _validate_attribution_entry_point(entry_point):
    _validate_attribution_mapping(
        "entry_point", entry_point, ATTRIBUTION_ENTRY_POINT_KEYS, 8192
    )
    delay_keys = {"delay_seconds", "conversion_delay_seconds"}
    for key, value in entry_point.items():
        if key in delay_keys:
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                or value > MAX_ATTRIBUTION_DELAY_SECONDS
            ):
                raise DTOValidationError(
                    "attribution.entry_point.%s must fit a nonnegative integer" % key
                )
        elif not isinstance(value, str):
            raise DTOValidationError(
                "attribution.entry_point values must be strings or delays"
            )


def _validate_attribution_presentation(creative, flags):
    _validate_attribution_mapping("creative", creative, ATTRIBUTION_CREATIVE_KEYS, 4096)
    _validate_attribution_mapping("flags", flags, ATTRIBUTION_FLAG_KEYS, 2048)
    if creative and not isinstance(creative.get("media_type"), str):
        raise DTOValidationError("attribution.creative.media_type must be a string")
    if any(not isinstance(value, bool) for value in flags.values()):
        raise DTOValidationError("attribution.flags values must be booleans")


def _validate_provider_attribution_extensions(provider_extensions):
    _require_dict(provider_extensions, "attribution.provider_extensions")
    if any(
        not isinstance(key, str) or not key.startswith("provider.")
        for key in provider_extensions
    ):
        raise DTOValidationError(
            "attribution.provider_extensions keys must use provider.* namespaces"
        )
    _validate_attribution_json(
        provider_extensions,
        "attribution.provider_extensions",
        maximum_bytes=16384,
    )


@dataclasses.dataclass(frozen=True)
class AddressDTO:
    namespace: str
    value: str
    value_normalized: str
    role: str = "primary"
    source_field: str = ""
    confidence: str = "observed"
    resolution_scope: str = "account"

    def __post_init__(self):
        _bounded_string(
            self.namespace,
            "address.namespace",
            maximum=128,
            required=True,
            reject_controls=True,
        )
        _bounded_string(
            self.value,
            "address.value",
            maximum=2048,
            required=True,
            reject_controls=True,
        )
        _bounded_string(
            self.value_normalized,
            "address.value_normalized",
            maximum=2048,
            required=True,
            reject_controls=True,
        )
        if self.role not in ADDRESS_ROLES:
            raise DTOValidationError("invalid address.role")
        _bounded_string(
            self.source_field,
            "address.source_field",
            maximum=256,
            reject_controls=True,
        )
        if self.confidence not in ADDRESS_CONFIDENCES:
            raise DTOValidationError("invalid address.confidence")
        if self.resolution_scope not in ADDRESS_RESOLUTION_SCOPES:
            raise DTOValidationError("invalid address.resolution_scope")

    @classmethod
    def from_dict(cls, values: Dict[str, Any]) -> "AddressDTO":
        try:
            values = _payload_dict(values, "address")
            if "normalized" in values and "value_normalized" not in values:
                values["value_normalized"] = values.pop("normalized")
            return cls(**values)
        except DTOValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise DTOValidationError("invalid address payload") from error

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)


def _protocol_sender(
    value: Any, field_name: str, *, serialized: bool = False
) -> Optional[AddressDTO]:
    if value is None:
        return None
    if serialized:
        participant = AddressDTO.from_dict(value)
    elif isinstance(value, AddressDTO):
        participant = value
    else:
        raise DTOValidationError("%s must be an AddressDTO" % field_name)
    if participant.role != "sender" or participant.confidence != "protocol":
        raise DTOValidationError("%s must be a protocol sender" % field_name)
    return participant


def _validate_event_mutation_scope(
    mutation: Dict[str, Any], conversation_type: str
) -> None:
    participant = _protocol_sender(
        mutation.get("target_protocol_participant"),
        "mutation.target_protocol_participant",
        serialized=True,
    )
    if conversation_type == "group":
        if "target_from_me" in mutation and not isinstance(
            mutation.get("target_from_me"), bool
        ):
            raise DTOValidationError("group mutation.target_from_me must be a boolean")
        # A provider may omit the target participant from a valid reaction or
        # revoke webhook.  The persisted message binding remains canonical;
        # an observed participant is optional evidence that the application
        # validates against that binding.
    elif participant is not None:
        raise DTOValidationError(
            "mutation.target_protocol_participant is only valid for groups"
        )


def _validate_mutation_provider_revision(
    mutation: Dict[str, Any], mutation_type: str
) -> None:
    if "provider_revision" not in mutation:
        return
    provider_revision = mutation["provider_revision"]
    if mutation_type != "edit":
        raise DTOValidationError("mutation.provider_revision is only valid for edits")
    if (
        not isinstance(provider_revision, int)
        or isinstance(provider_revision, bool)
        or provider_revision < 0
        or provider_revision > MAX_MUTATION_PROVIDER_REVISION
    ):
        raise DTOValidationError(
            "mutation.provider_revision must be a bounded non-negative integer"
        )


def _validate_command_protocol_participants(
    command_type: str,
    conversation_type: str,
    options: Dict[str, Any],
    own_participant: Optional[AddressDTO],
    target_participant: Optional[AddressDTO],
) -> None:
    mutation_commands = {"react", "edit_message", "delete_message"}
    allowed_own_commands = mutation_commands | {"send_message"}
    options = _require_dict(options, "options")
    own_participant = _protocol_sender(own_participant, "own_protocol_participant")
    target_participant = _protocol_sender(
        target_participant, "target_protocol_participant"
    )
    if own_participant is not None and (
        conversation_type != "group" or command_type not in allowed_own_commands
    ):
        raise DTOValidationError(
            "own_protocol_participant is only valid for group outbound commands"
        )
    if target_participant is not None and (
        conversation_type != "group" or command_type not in mutation_commands
    ):
        raise DTOValidationError(
            "target_protocol_participant is only valid for group mutation commands"
        )
    if conversation_type != "group" or command_type not in mutation_commands:
        return
    if own_participant is None:
        raise DTOValidationError(
            "group mutation commands require own_protocol_participant"
        )
    if target_participant is None:
        raise DTOValidationError(
            "group mutation commands require target_protocol_participant"
        )
    if not isinstance(options.get("target_from_me"), bool):
        raise DTOValidationError(
            "group mutation options.target_from_me must be a boolean"
        )


@dataclasses.dataclass(frozen=True)
class ActorDTO:
    addresses: Tuple[AddressDTO, ...] = ()
    display_name: str = ""

    def __post_init__(self):
        if not isinstance(self.addresses, tuple) or any(
            not isinstance(address, AddressDTO) for address in self.addresses
        ):
            raise DTOValidationError("actor.addresses must contain AddressDTO values")
        if len(self.addresses) > MAX_ADDRESS_COUNT:
            raise DTOValidationError("actor has too many addresses")
        _bounded_string(self.display_name, "actor.display_name", maximum=1024)

    @classmethod
    def from_dict(cls, values: Optional[Dict[str, Any]]) -> "ActorDTO":
        values = _payload_dict(values, "actor")
        return cls(
            addresses=tuple(
                AddressDTO.from_dict(item)
                for item in _as_tuple(values.get("addresses"), "actor.addresses")
            ),
            display_name=values.get("display_name") or "",
        )

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)


@dataclasses.dataclass(frozen=True)
class ConversationDTO:
    addresses: Tuple[AddressDTO, ...] = ()
    conversation_type: str = "direct"

    def __post_init__(self):
        if not isinstance(self.addresses, tuple) or any(
            not isinstance(address, AddressDTO) for address in self.addresses
        ):
            raise DTOValidationError(
                "conversation.addresses must contain AddressDTO values"
            )
        if self.conversation_type not in CONVERSATION_TYPES:
            raise DTOValidationError("invalid conversation.conversation_type")
        if len(self.addresses) > MAX_ADDRESS_COUNT:
            raise DTOValidationError("conversation has too many addresses")

    @classmethod
    def from_dict(cls, values: Optional[Dict[str, Any]]) -> "ConversationDTO":
        values = _payload_dict(values, "conversation")
        return cls(
            addresses=tuple(
                AddressDTO.from_dict(item)
                for item in _as_tuple(values.get("addresses"), "conversation.addresses")
            ),
            conversation_type=values.get("conversation_type")
            or values.get("type")
            or "direct",
        )

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)


@dataclasses.dataclass(frozen=True)
class GroupParticipantDTO:
    """One participant in an authoritative provider-neutral group snapshot."""

    participant_ref: str
    addresses: Tuple[AddressDTO, ...]
    display_name: str = ""
    role: str = "member"

    def __post_init__(self):
        _bounded_string(
            self.participant_ref,
            "group_participant.participant_ref",
            maximum=512,
            required=True,
            reject_controls=True,
        )
        if (
            not isinstance(self.addresses, tuple)
            or not self.addresses
            or any(not isinstance(address, AddressDTO) for address in self.addresses)
        ):
            raise DTOValidationError(
                "group_participant.addresses must contain AddressDTO values"
            )
        if any(address.role in ("group", "routing") for address in self.addresses):
            raise DTOValidationError(
                "group participant addresses cannot be group routing identifiers"
            )
        address_keys = {
            (address.namespace, address.value_normalized) for address in self.addresses
        }
        if len(address_keys) != len(self.addresses):
            raise DTOValidationError("group participant addresses must be unique")
        if len(self.addresses) > MAX_ADDRESS_COUNT:
            raise DTOValidationError("group participant has too many addresses")
        _bounded_string(
            self.display_name, "group_participant.display_name", maximum=1024
        )
        if self.role not in GROUP_PARTICIPANT_ROLES:
            raise DTOValidationError("invalid group participant role")

    @classmethod
    def from_dict(cls, values: Dict[str, Any]) -> "GroupParticipantDTO":
        try:
            values = _payload_dict(values, "group_participant")
            values["addresses"] = tuple(
                AddressDTO.from_dict(item)
                for item in _as_tuple(
                    values.get("addresses"), "group_participant.addresses"
                )
            )
            return cls(**values)
        except DTOValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise DTOValidationError("invalid group participant payload") from error

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)


@dataclasses.dataclass(frozen=True)
class GroupMetadataDTO:
    """Complete provider-neutral group metadata fetched outside webhook handling."""

    conversation_ref: str
    observed_at: datetime.datetime
    participants: Tuple[GroupParticipantDTO, ...] = ()
    display_name: str = ""
    participant_count: int = 0
    own_role: str = "unknown"
    own_protocol_participant: Optional[AddressDTO] = None
    provider_revision: str = ""
    is_complete: bool = True

    def __post_init__(self):
        _bounded_string(
            self.conversation_ref,
            "group_metadata.conversation_ref",
            maximum=2048,
            required=True,
            reject_controls=True,
        )
        if not isinstance(self.participants, tuple) or any(
            not isinstance(participant, GroupParticipantDTO)
            for participant in self.participants
        ):
            raise DTOValidationError(
                "group_metadata.participants must contain GroupParticipantDTO values"
            )
        if len(self.participants) > MAX_GROUP_PARTICIPANTS:
            raise DTOValidationError("group metadata has too many participants")
        _bounded_string(self.display_name, "group_metadata.display_name", maximum=1024)
        if (
            not isinstance(self.participant_count, int)
            or isinstance(self.participant_count, bool)
            or self.participant_count < len(self.participants)
        ):
            raise DTOValidationError(
                "group_metadata.participant_count cannot be smaller than the roster"
            )
        if self.own_role not in GROUP_OWN_ROLES:
            raise DTOValidationError("invalid group own role")
        if self.own_protocol_participant is not None:
            participant = self.own_protocol_participant
            if not isinstance(participant, AddressDTO):
                raise DTOValidationError(
                    "group_metadata.own_protocol_participant must be an AddressDTO"
                )
            if participant.role != "sender" or participant.confidence != "protocol":
                raise DTOValidationError(
                    "group own protocol participant must be a protocol sender"
                )
        _bounded_string(
            self.provider_revision,
            "group_metadata.provider_revision",
            maximum=512,
            reject_controls=True,
        )
        if not isinstance(self.is_complete, bool):
            raise DTOValidationError("group_metadata.is_complete must be a boolean")
        if self.is_complete and self.participant_count != len(self.participants):
            raise DTOValidationError(
                "a complete group snapshot count must equal its roster size"
            )
        participant_refs = {
            participant.participant_ref for participant in self.participants
        }
        if len(participant_refs) != len(self.participants):
            raise DTOValidationError("group participant references must be unique")
        address_keys = [
            (address.namespace, address.value_normalized)
            for participant in self.participants
            for address in participant.addresses
        ]
        if len(set(address_keys)) != len(address_keys):
            raise DTOValidationError(
                "one group address cannot belong to multiple participants"
            )
        if self.own_protocol_participant is not None and (
            self.own_protocol_participant.namespace,
            self.own_protocol_participant.value_normalized,
        ) not in set(address_keys):
            raise DTOValidationError(
                "group own protocol participant must belong to the roster"
            )
        object.__setattr__(self, "observed_at", _parse_utc(self.observed_at))

    @classmethod
    def from_dict(cls, values: Dict[str, Any]) -> "GroupMetadataDTO":
        try:
            values = _payload_dict(values, "group_metadata")
            values["participants"] = tuple(
                (
                    item
                    if isinstance(item, GroupParticipantDTO)
                    else GroupParticipantDTO.from_dict(item)
                )
                for item in _as_tuple(
                    values.get("participants"), "group_metadata.participants"
                )
            )
            values["observed_at"] = _parse_utc(values.get("observed_at"))
            if values.get("own_protocol_participant") is not None:
                values["own_protocol_participant"] = AddressDTO.from_dict(
                    values["own_protocol_participant"]
                )
            return cls(**values)
        except DTOValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise DTOValidationError("invalid group metadata payload") from error

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)


@dataclasses.dataclass(frozen=True)
class AvatarResult:
    """Ephemeral bounded avatar result; remote URLs never cross into the core."""

    state: str
    provider_revision: str = ""
    content: bytes = b""
    mime_type: str = ""
    file_name: str = ""
    size_bytes: int = 0
    sha256: str = ""

    def __post_init__(self):
        if self.state not in AVATAR_STATES:
            raise DTOValidationError("invalid avatar state")
        for field_name in (
            "provider_revision",
            "mime_type",
            "file_name",
            "sha256",
        ):
            _bounded_string(
                getattr(self, field_name),
                "avatar.%s" % field_name,
                maximum=512 if field_name == "provider_revision" else 255,
                reject_controls=True,
            )
        if not isinstance(self.content, bytes):
            raise DTOValidationError("avatar.content must be bytes")
        if (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes < 0
        ):
            raise DTOValidationError("avatar.size_bytes must be a nonnegative integer")
        if self.state != "ready":
            if self.content or self.mime_type or self.size_bytes or self.sha256:
                raise DTOValidationError(
                    "an absent or unavailable avatar cannot contain binary data"
                )
            return
        if not self.content or not self.mime_type:
            raise DTOValidationError("a ready avatar requires content and a MIME type")
        actual_size = len(self.content)
        if self.size_bytes not in (0, actual_size):
            raise DTOValidationError("avatar size does not match content")
        digest = hashlib.sha256(self.content).hexdigest()
        if self.sha256 and self.sha256.lower() != digest:
            raise DTOValidationError("avatar SHA-256 does not match content")
        object.__setattr__(self, "size_bytes", actual_size)
        object.__setattr__(self, "sha256", digest)

    def to_dict(self) -> Dict[str, Any]:
        values = _serialize(self)
        values.pop("content", None)
        return values


@dataclasses.dataclass(frozen=True)
class IdentityProfileResult:
    """Ephemeral provider-neutral direct-identity profile result."""

    display_name: str
    avatar: AvatarResult

    def __post_init__(self):
        _require_string(self.display_name, "identity_profile.display_name")
        if len(self.display_name) > 255:
            raise DTOValidationError("identity profile display name is too long")
        if _CONTROL_CHARACTER_PATTERN.search(self.display_name):
            raise DTOValidationError(
                "identity profile display name contains control characters"
            )
        if not isinstance(self.avatar, AvatarResult):
            raise DTOValidationError("identity_profile.avatar must be an AvatarResult")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "display_name": self.display_name,
            "avatar": self.avatar.to_dict(),
        }


@dataclasses.dataclass(frozen=True)
class MediaDTO:
    """Provider-neutral media descriptor; binary content never belongs in the DTO."""

    kind: str
    external_media_id: str = ""
    remote_locator: Dict[str, Any] = dataclasses.field(default_factory=dict)
    mime_type: str = ""
    file_name: str = ""
    size_bytes: int = 0
    sha256: str = ""
    is_voice_note: bool = False
    duration_seconds: int = 0
    width: int = 0
    height: int = 0

    def __post_init__(self):
        if self.kind not in MEDIA_KINDS:
            raise DTOValidationError("invalid media.kind")
        for field_name in (
            "external_media_id",
            "mime_type",
            "file_name",
            "sha256",
        ):
            _bounded_string(
                getattr(self, field_name),
                "media.%s" % field_name,
                maximum=512 if field_name == "external_media_id" else 255,
                reject_controls=field_name != "file_name",
            )
        _require_dict(self.remote_locator, "media.remote_locator")
        _validate_json_locator(self.remote_locator, "media.remote_locator")
        _validate_bounded_json(
            self.remote_locator,
            "media.remote_locator",
            maximum_bytes=32_768,
            maximum_depth=8,
        )
        for field_name in ("size_bytes", "duration_seconds", "width", "height"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise DTOValidationError(
                    "media.%s must be a nonnegative integer" % field_name
                )
        if self.sha256 and not _SHA256_PATTERN.fullmatch(self.sha256.lower()):
            raise DTOValidationError("media.sha256 must be a lowercase SHA-256 digest")
        if not isinstance(self.is_voice_note, bool):
            raise DTOValidationError("media.is_voice_note must be a boolean")
        if self.is_voice_note and self.kind != "audio":
            raise DTOValidationError("media.is_voice_note requires media.kind audio")

    @classmethod
    def from_dict(cls, values: Dict[str, Any]) -> "MediaDTO":
        try:
            return cls(**_payload_dict(values, "media"))
        except DTOValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise DTOValidationError("invalid media payload") from error

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)


@dataclasses.dataclass(frozen=True)
class MediaDownloadResult:
    """Ephemeral adapter result. ``content`` must never be serialized or persisted raw."""

    content: bytes
    mime_type: str
    file_name: str = ""
    size_bytes: int = 0
    sha256: str = ""

    def __post_init__(self):
        if not isinstance(self.content, bytes):
            raise DTOValidationError("media download content must be bytes")
        _bounded_string(
            self.mime_type,
            "media_download.mime_type",
            maximum=255,
            required=True,
            reject_controls=True,
        )
        _bounded_string(self.file_name, "media_download.file_name", maximum=255)
        if (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes < 0
        ):
            raise DTOValidationError(
                "media_download.size_bytes must be a nonnegative integer"
            )
        actual_size = len(self.content)
        if self.size_bytes not in (0, actual_size):
            raise DTOValidationError("media download size does not match content")
        digest = hashlib.sha256(self.content).hexdigest()
        if self.sha256 and self.sha256.lower() != digest:
            raise DTOValidationError("media download SHA-256 does not match content")
        object.__setattr__(self, "size_bytes", actual_size)
        object.__setattr__(self, "sha256", digest)


@dataclasses.dataclass(frozen=True)
class ExternalIdentifierDTO:
    """One bounded provider identifier attached to an attribution touchpoint."""

    namespace: str
    role: str
    value: str
    source_field: str = ""
    comparison_hash: str = ""

    def __post_init__(self):
        _attribution_key(self.namespace, "attribution_identifier.namespace")
        _attribution_key(self.role, "attribution_identifier.role")
        value = _bounded_attribution_string(
            self.value,
            "attribution_identifier.value",
            maximum=2048,
            required=True,
        )
        _bounded_attribution_string(
            self.source_field,
            "attribution_identifier.source_field",
            maximum=256,
        )
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        if self.comparison_hash:
            supplied = _bounded_attribution_string(
                self.comparison_hash,
                "attribution_identifier.comparison_hash",
                maximum=64,
                required=True,
            ).lower()
            if not _SHA256_PATTERN.fullmatch(supplied) or supplied != digest:
                raise DTOValidationError(
                    "attribution_identifier.comparison_hash does not match value"
                )
        object.__setattr__(self, "comparison_hash", digest)

    @classmethod
    def from_dict(cls, values: Dict[str, Any]) -> "ExternalIdentifierDTO":
        try:
            return cls(**_payload_dict(values, "attribution_identifier"))
        except DTOValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise DTOValidationError(
                "invalid attribution identifier payload"
            ) from error

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)


@dataclasses.dataclass(frozen=True)
class AttributionDTO:
    """Provider-neutral acquisition or entry-point evidence for one event."""

    touchpoint_type: str
    evidence_level: str
    network: str
    source_platform: str = ""
    source_type: str = ""
    source_url: str = ""
    external_identifiers: Tuple[ExternalIdentifierDTO, ...] = ()
    utm: Dict[str, Any] = dataclasses.field(default_factory=dict)
    entry_point: Dict[str, Any] = dataclasses.field(default_factory=dict)
    creative: Dict[str, Any] = dataclasses.field(default_factory=dict)
    flags: Dict[str, Any] = dataclasses.field(default_factory=dict)
    provider_extensions: Dict[str, Any] = dataclasses.field(default_factory=dict)
    schema_version: int = ATTRIBUTION_SCHEMA_VERSION

    def __post_init__(self):
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != ATTRIBUTION_SCHEMA_VERSION
        ):
            raise DTOValidationError("unsupported AttributionDTO schema_version")
        if self.touchpoint_type not in ATTRIBUTION_TOUCHPOINT_TYPES:
            raise DTOValidationError("invalid attribution.touchpoint_type")
        if self.evidence_level not in ATTRIBUTION_EVIDENCE_LEVELS:
            raise DTOValidationError("invalid attribution.evidence_level")
        _attribution_key(self.network, "attribution.network")
        _validate_attribution_source(
            self.source_platform, self.source_type, self.source_url
        )
        _validate_attribution_identifiers(self.external_identifiers)
        _validate_attribution_utm(self.utm)
        _validate_attribution_entry_point(self.entry_point)
        _validate_attribution_presentation(self.creative, self.flags)
        _validate_provider_attribution_extensions(self.provider_extensions)

    @classmethod
    def from_dict(cls, values: Dict[str, Any]) -> "AttributionDTO":
        try:
            values = _payload_dict(values, "attribution")
            values["external_identifiers"] = tuple(
                (
                    item
                    if isinstance(item, ExternalIdentifierDTO)
                    else ExternalIdentifierDTO.from_dict(item)
                )
                for item in _as_tuple(
                    values.get("external_identifiers"),
                    "attribution.external_identifiers",
                )
            )
            return cls(**values)
        except DTOValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise DTOValidationError("invalid attribution payload") from error

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)


@dataclasses.dataclass(frozen=True)
class MessageDTO:
    external_message_id: str = ""
    content_type: str = "text"
    text: str = ""
    client_message_id: str = ""
    reply_to_external_id: str = ""
    is_forwarded: bool = False
    forwarding_score: Optional[int] = None
    protocol_snapshot: Dict[str, Any] = dataclasses.field(default_factory=dict)
    media: Tuple[MediaDTO, ...] = ()
    structured_content: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self):
        try:
            validate_structured_content(self.structured_content)
        except (TypeError, ValueError) as error:
            raise DTOValidationError("invalid message structured content") from error
        for field_name in (
            "external_message_id",
            "client_message_id",
            "reply_to_external_id",
        ):
            _bounded_string(
                getattr(self, field_name),
                "message.%s" % field_name,
                maximum=512,
                reject_controls=True,
            )
        _bounded_string(self.text, "message.text", maximum=MAX_MESSAGE_TEXT)
        _bounded_string(
            self.content_type,
            "message.content_type",
            maximum=128,
            required=True,
            reject_controls=True,
        )
        if not isinstance(self.is_forwarded, bool):
            raise DTOValidationError("message.is_forwarded must be a boolean")
        if self.forwarding_score is not None and (
            not isinstance(self.forwarding_score, int)
            or isinstance(self.forwarding_score, bool)
            or not 0 <= self.forwarding_score <= MAX_FORWARDING_SCORE
        ):
            raise DTOValidationError(
                "message.forwarding_score must be a nonnegative integer or null"
            )
        _validate_bounded_json(
            self.protocol_snapshot,
            "message.protocol_snapshot",
            maximum_bytes=65_536,
        )
        if not isinstance(self.media, tuple) or any(
            not isinstance(item, MediaDTO) for item in self.media
        ):
            raise DTOValidationError("message.media must contain MediaDTO values")
        if len(self.media) > MAX_MESSAGE_MEDIA:
            raise DTOValidationError("message has too many media items")

    @classmethod
    def from_dict(cls, values: Optional[Dict[str, Any]]) -> "MessageDTO":
        try:
            values = _payload_dict(values, "message")
            values["media"] = tuple(
                item if isinstance(item, MediaDTO) else MediaDTO.from_dict(item)
                for item in _as_tuple(values.get("media"), "message.media")
            )
            return cls(**values)
        except DTOValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise DTOValidationError("invalid message payload") from error

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)


def _validate_event_attributions(attributions):
    if not isinstance(attributions, tuple) or any(
        not isinstance(item, AttributionDTO) for item in attributions
    ):
        raise DTOValidationError("attribution must contain AttributionDTO values")
    if len(attributions) > MAX_EVENT_ATTRIBUTIONS:
        raise DTOValidationError("event has too many attribution touchpoints")
    fingerprints = {
        hashlib.sha256(
            json.dumps(
                item.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        for item in attributions
    }
    if len(fingerprints) != len(attributions):
        raise DTOValidationError("event attribution touchpoints must be unique")
    touchpoint_types = {item.touchpoint_type for item in attributions}
    if len(touchpoint_types) != len(attributions):
        raise DTOValidationError(
            "one event cannot contain duplicate attribution touchpoint types"
        )


@dataclasses.dataclass(frozen=True)
class EventDTO:
    provider_schema_version: str
    event_id: str
    event_type: str
    occurred_at: datetime.datetime
    account_ref: str
    connection_ref: str
    conversation_ref: str
    platform: str
    direction: str
    is_from_me: bool
    origin: str
    actor: ActorDTO
    conversation: ConversationDTO
    message: Optional[MessageDTO] = None
    reply_to: Dict[str, Any] = dataclasses.field(default_factory=dict)
    delivery: Dict[str, Any] = dataclasses.field(default_factory=dict)
    attribution: Tuple[AttributionDTO, ...] = ()
    extensions: Dict[str, Any] = dataclasses.field(default_factory=dict)
    mutation: Dict[str, Any] = dataclasses.field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != SCHEMA_VERSION
        ):
            raise DTOValidationError("unsupported EventDTO schema_version")
        for field_name in (
            "provider_schema_version",
            "event_id",
            "event_type",
            "account_ref",
            "connection_ref",
            "conversation_ref",
            "platform",
            "direction",
            "origin",
        ):
            _bounded_string(
                getattr(self, field_name),
                field_name,
                maximum=(
                    2048
                    if field_name
                    in ("account_ref", "connection_ref", "conversation_ref")
                    else 512
                ),
                required=True,
                reject_controls=True,
            )
        if self.direction not in EVENT_DIRECTIONS:
            raise DTOValidationError("direction must be inbound or outbound")
        if not isinstance(self.is_from_me, bool):
            raise DTOValidationError("is_from_me must be a boolean")
        if self.origin not in EVENT_ORIGINS:
            raise DTOValidationError("invalid event origin")
        if not isinstance(self.actor, ActorDTO):
            raise DTOValidationError("actor must be an ActorDTO")
        if not isinstance(self.conversation, ConversationDTO):
            raise DTOValidationError("conversation must be a ConversationDTO")
        if self.message is not None and not isinstance(self.message, MessageDTO):
            raise DTOValidationError("message must be a MessageDTO")
        _validate_bounded_json(self.reply_to, "reply_to", maximum_bytes=32_768)
        _validate_bounded_json(self.delivery, "delivery", maximum_bytes=32_768)
        _validate_event_attributions(self.attribution)
        _validate_bounded_json(self.extensions, "extensions", maximum_bytes=65_536)
        _validate_bounded_json(self.mutation, "mutation", maximum_bytes=65_536)
        if self.mutation:
            mutation_type = self.mutation.get("type")
            if mutation_type not in MUTATION_TYPES:
                raise DTOValidationError("invalid mutation.type")
            _bounded_string(
                self.mutation.get("target_external_message_id"),
                "mutation.target_external_message_id",
                maximum=512,
                required=True,
                reject_controls=True,
            )
            if mutation_type == "react":
                operation = self.mutation.get("operation") or "add"
                if operation not in REACTION_OPERATIONS:
                    raise DTOValidationError("invalid mutation.operation")
                emoji = self.mutation.get("emoji") or ""
                _bounded_string(emoji, "mutation.emoji", maximum=64)
                if operation == "add" and not emoji:
                    raise DTOValidationError("mutation.emoji is required when adding")
            if mutation_type == "edit":
                _bounded_string(
                    self.mutation.get("new_text"),
                    "mutation.new_text",
                    maximum=MAX_MESSAGE_TEXT,
                )
            _validate_mutation_provider_revision(self.mutation, mutation_type)
            _validate_event_mutation_scope(
                self.mutation, self.conversation.conversation_type
            )
        object.__setattr__(self, "occurred_at", _parse_utc(self.occurred_at))

    @classmethod
    def from_dict(cls, values: Dict[str, Any]) -> "EventDTO":
        try:
            values = _payload_dict(values, "event")
            values["actor"] = ActorDTO.from_dict(values.get("actor"))
            values["conversation"] = ConversationDTO.from_dict(
                values.get("conversation")
            )
            if values.get("message") is not None:
                values["message"] = MessageDTO.from_dict(values["message"])
            if "attributions" in values and "attribution" not in values:
                values["attribution"] = values.pop("attributions")
            values["attribution"] = tuple(
                (
                    item
                    if isinstance(item, AttributionDTO)
                    else AttributionDTO.from_dict(item)
                )
                for item in _as_tuple(values.get("attribution"), "attribution")
            )
            values["occurred_at"] = _parse_utc(values.get("occurred_at"))
            return cls(**values)
        except DTOValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise DTOValidationError("invalid EventDTO payload") from error

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)


@dataclasses.dataclass(frozen=True)
class CommandDTO:
    command_id: str
    command_type: str
    account_ref: str
    connection_ref: str
    conversation_ref: str
    conversation: ConversationDTO
    target_address: Optional[AddressDTO] = None
    own_protocol_participant: Optional[AddressDTO] = None
    target_protocol_participant: Optional[AddressDTO] = None
    message: Optional[MessageDTO] = None
    reply_to: Dict[str, Any] = dataclasses.field(default_factory=dict)
    options: Dict[str, Any] = dataclasses.field(default_factory=dict)
    extensions: Dict[str, Any] = dataclasses.field(default_factory=dict)
    client_message_id: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != SCHEMA_VERSION
        ):
            raise DTOValidationError("unsupported CommandDTO schema_version")
        for field_name in (
            "command_id",
            "command_type",
            "account_ref",
            "connection_ref",
            "conversation_ref",
        ):
            _bounded_string(
                getattr(self, field_name),
                field_name,
                maximum=(
                    2048
                    if field_name
                    in ("account_ref", "connection_ref", "conversation_ref")
                    else 512
                ),
                required=True,
                reject_controls=True,
            )
        if self.command_type not in COMMAND_TYPES:
            raise DTOValidationError("invalid command_type")
        if not isinstance(self.conversation, ConversationDTO):
            raise DTOValidationError("conversation must be a ConversationDTO")
        if self.target_address is not None and not isinstance(
            self.target_address, AddressDTO
        ):
            raise DTOValidationError("target_address must be an AddressDTO")
        _validate_command_protocol_participants(
            self.command_type,
            self.conversation.conversation_type,
            self.options,
            self.own_protocol_participant,
            self.target_protocol_participant,
        )
        if self.message is not None and not isinstance(self.message, MessageDTO):
            raise DTOValidationError("message must be a MessageDTO")
        _validate_bounded_json(self.reply_to, "reply_to", maximum_bytes=32_768)
        _validate_bounded_json(self.options, "options", maximum_bytes=65_536)
        _validate_bounded_json(self.extensions, "extensions", maximum_bytes=65_536)
        _bounded_string(
            self.client_message_id,
            "client_message_id",
            maximum=512,
            reject_controls=True,
        )

    @classmethod
    def from_dict(cls, values: Dict[str, Any]) -> "CommandDTO":
        try:
            values = _payload_dict(values, "command")
            values["conversation"] = ConversationDTO.from_dict(
                values.get("conversation")
            )
            if values.get("target_address"):
                values["target_address"] = AddressDTO.from_dict(
                    values["target_address"]
                )
            if values.get("own_protocol_participant") is not None:
                values["own_protocol_participant"] = AddressDTO.from_dict(
                    values["own_protocol_participant"]
                )
            if values.get("target_protocol_participant") is not None:
                values["target_protocol_participant"] = AddressDTO.from_dict(
                    values["target_protocol_participant"]
                )
            if values.get("message") is not None:
                values["message"] = MessageDTO.from_dict(values["message"])
            return cls(**values)
        except DTOValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise DTOValidationError("invalid CommandDTO payload") from error

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)


@dataclasses.dataclass(frozen=True)
class AdapterResult:
    status: str
    external_message_id: str = ""
    provider_response: Dict[str, Any] = dataclasses.field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    retry_after_seconds: int = 0

    def __post_init__(self):
        allowed = {"success", "permanent", "transient", "uncertain", "paused"}
        if self.status not in allowed:
            raise DTOValidationError("invalid adapter result status")
        for field_name in ("external_message_id", "error_code", "error_message"):
            _bounded_string(
                getattr(self, field_name),
                field_name,
                maximum=4096 if field_name == "error_message" else 512,
                reject_controls=field_name != "error_message",
            )
        _validate_bounded_json(
            self.provider_response,
            "provider_response",
            maximum_bytes=65_536,
        )
        if (
            not isinstance(self.retry_after_seconds, int)
            or isinstance(self.retry_after_seconds, bool)
            or self.retry_after_seconds < 0
        ):
            raise DTOValidationError(
                "retry_after_seconds must be a nonnegative integer"
            )

    @classmethod
    def success(
        cls,
        external_message_id: str = "",
        provider_response: Optional[Dict[str, Any]] = None,
    ) -> "AdapterResult":
        return cls(
            status="success",
            external_message_id=external_message_id,
            provider_response=provider_response or {},
        )

    def to_dict(self) -> Dict[str, Any]:
        return _serialize(self)
