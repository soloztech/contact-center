import datetime
import hashlib
import ipaddress
import json
import re
import socket
from urllib.parse import urlsplit

import requests

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    ProviderPausedError,
    ProviderRateLimitError,
    TransientAdapterError,
    UnsupportedEventError,
)
from odoo.addons.contact_center_base.services.dto import (
    ActorDTO,
    AddressDTO,
    AvatarResult,
    ConversationDTO,
    DTOValidationError,
    EventDTO,
    GroupMetadataDTO,
    GroupParticipantDTO,
    IdentityProfileResult,
)

_GROUP_METADATA_TIMEOUT = (5, 30)
_AVATAR_TIMEOUT = (5, 30)
_MAX_GROUP_METADATA_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_IDENTITY_CHECK_RESPONSE_BYTES = 256 * 1024
_MAX_AVATAR_BYTES = 2 * 1024 * 1024
_STRICT_GROUP_JID_PATTERN = re.compile(r"^[0-9]+(?:-[0-9]+)?@g\.us$")
_STRICT_PARTICIPANT_JID_PATTERN = re.compile(
    r"^[0-9]+(?::[0-9]+)?@(s\.whatsapp\.net|lid)$"
)
_STRICT_LEGACY_USER_JID_PATTERN = re.compile(r"^[0-9]+(?::[0-9]+)?@c\.us$")
_STRICT_PHONE_PATTERN = re.compile(r"^\+?[0-9]{8,20}$")
_AVATAR_HOST_SUFFIXES = (
    "whatsapp.net",
    "fbcdn.net",
    "fbsbx.com",
    "cdninstagram.com",
)
_GROUP_TERMINAL_ERROR_MARKERS = (
    "that group does not exist",
    "not participating in that group",
)
_AVATAR_ABSENT_MARKERS = (
    "no avatar found",
    "does not have a profile picture",
)
_AVATAR_UNAVAILABLE_MARKERS = (
    "has hidden their profile picture",
    "profile picture is hidden",
)


class WuzapiGroupMetadataMixin:
    """Pinned WuzAPI v1.0.8 group metadata and avatar read boundary.

    The concrete adapter provides the small generic parsing helpers as static
    callables. Keeping those shared primitives in ``adapter.py`` avoids two subtly
    different implementations of WuzAPI's case-insensitive JSON and HTTP rules.
    """

    _provider_lookup = None
    _provider_header = None
    _provider_response_json = None
    _provider_retry_after = None
    _provider_event_metadata = None
    _provider_jid_string = None
    _provider_normalize_jid = None
    _provider_normalize_session_identity = None
    _provider_jid_namespace = None
    _provider_parse_timestamp = None
    _provider_boolean = None

    def _group_strict_address(self, value, source_field):
        raw = self._provider_jid_string(value)
        normalized = self._provider_normalize_jid(value)
        if not raw or not _STRICT_GROUP_JID_PATTERN.fullmatch(normalized):
            raise AdapterError("WuzAPI group JID is invalid")
        return AddressDTO(
            namespace="whatsapp.group",
            value=raw,
            value_normalized=normalized,
            role="group",
            source_field=source_field,
            confidence="protocol",
        )

    def _group_participant_address(self, value, role, source_field):
        raw = self._provider_jid_string(value)
        if not raw:
            return None
        normalized = self._provider_normalize_jid(value)
        if not _STRICT_PARTICIPANT_JID_PATTERN.fullmatch(normalized):
            raise AdapterError("WuzAPI group participant JID is invalid")
        return AddressDTO(
            namespace=self._provider_jid_namespace(normalized),
            value=raw,
            value_normalized=normalized,
            role=role,
            source_field=source_field,
            confidence="protocol",
        )

    def _identity_avatar_address(self, value, source_field):
        raw = self._provider_jid_string(value)
        normalized = self._provider_normalize_jid(value)
        if not raw or not _STRICT_PARTICIPANT_JID_PATTERN.fullmatch(normalized):
            raise AdapterError("WuzAPI identity avatar JID is invalid")
        namespace = self._provider_jid_namespace(normalized)
        if namespace not in ("whatsapp.pn", "whatsapp.lid"):
            raise AdapterError("WuzAPI identity avatar JID is invalid")
        return AddressDTO(
            namespace=namespace,
            value=raw,
            value_normalized=normalized,
            role="primary",
            source_field=source_field,
            confidence="protocol",
        )

    def _validated_identity_avatar_address(self, address):
        if not isinstance(address, AddressDTO):
            raise AdapterError("WuzAPI identity avatar requires an AddressDTO")
        normalized = self._provider_normalize_jid(address.value)
        if address.namespace in ("whatsapp.pn", "whatsapp.lid"):
            valid = (
                address.namespace == self._provider_jid_namespace(normalized)
                and address.value_normalized == normalized
                and bool(_STRICT_PARTICIPANT_JID_PATTERN.fullmatch(normalized))
            )
        elif address.namespace == "whatsapp.jid":
            valid = address.value_normalized == normalized and bool(
                _STRICT_LEGACY_USER_JID_PATTERN.fullmatch(normalized)
            )
        elif address.namespace == "phone":
            raw_digits = "".join(
                character for character in address.value if character.isdigit()
            )
            normalized_digits = "".join(
                character
                for character in address.value_normalized
                if character.isdigit()
            )
            valid = (
                bool(_STRICT_PHONE_PATTERN.fullmatch(address.value_normalized))
                and raw_digits == normalized_digits
            )
        else:
            valid = False
        if not valid:
            raise AdapterError("WuzAPI identity avatar address is invalid")
        return address

    @staticmethod
    def _group_safe_display_name(value):
        if not isinstance(value, str):
            return ""
        value = value.strip()
        if (
            not value
            or len(value) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            return ""
        return value

    @staticmethod
    def _group_safe_provider_revision(value):
        if isinstance(value, bool) or value is None:
            return ""
        if isinstance(value, (str, int)):
            normalized = str(value).strip()
            if (
                normalized
                and len(normalized) <= 512
                and not any(
                    ord(character) < 32 or ord(character) == 127
                    for character in normalized
                )
            ):
                return normalized
        return ""

    def _group_enforce_content_length(self, response, maximum_bytes, label):
        content_length = self._provider_header(
            getattr(response, "headers", {}), "Content-Length"
        )
        if not content_length:
            return
        try:
            oversized = int(content_length) > maximum_bytes
        except ValueError:
            return
        if not oversized:
            return
        close = getattr(response, "close", None)
        if callable(close):
            close()
        raise AdapterError("%s response exceeds the size limit" % label)

    def _group_limited_json_object(self, response, maximum_bytes, label):
        close = getattr(response, "close", None)
        self._group_enforce_content_length(response, maximum_bytes, label)
        iterator = getattr(response, "iter_content", None)
        if not callable(iterator):
            try:
                payload = self._provider_response_json(response)
                if payload is None:
                    raise AdapterError("%s response is not valid JSON" % label)
                serialized = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                if len(serialized) > maximum_bytes:
                    raise AdapterError("%s response exceeds the size limit" % label)
                return payload
            finally:
                if callable(close):
                    close()

        chunks = []
        total = 0
        try:
            for chunk in iterator(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > maximum_bytes:
                    raise AdapterError("%s response exceeds the size limit" % label)
                chunks.append(chunk)
            payload = json.loads(b"".join(chunks).decode("utf-8"))
        except requests.RequestException as error:
            raise TransientAdapterError(
                "%s response was interrupted" % label
            ) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AdapterError("%s response is not valid JSON" % label) from error
        finally:
            if callable(close):
                close()
        if not isinstance(payload, dict):
            raise AdapterError("%s response is not a JSON object" % label)
        return payload

    def _group_bounded_provider_error(self, response):
        try:
            payload = self._group_limited_json_object(
                response, 64 * 1024, "WuzAPI provider error"
            )
        except AdapterError:
            return ""
        return self._group_provider_error(payload)

    def _group_provider_error(self, payload):
        value = self._provider_lookup(payload, "error")
        if not isinstance(value, str):
            return ""
        return value.strip().lower()[:2048]

    def _group_participant_from_provider(self, values):
        if not isinstance(values, dict):
            raise AdapterError("WuzAPI group participant must be an object")
        addresses = []
        seen = set()
        for field_name, role in (
            ("JID", "primary"),
            ("PhoneNumber", "alternate"),
            ("LID", "alternate"),
        ):
            address = self._group_participant_address(
                self._provider_lookup(values, field_name),
                role,
                "data.Participants.%s" % field_name,
            )
            if not address:
                continue
            key = (address.namespace, address.value_normalized)
            if key in seen:
                continue
            if any(
                existing.namespace == address.namespace
                and existing.value_normalized != address.value_normalized
                for existing in addresses
            ):
                raise AdapterError(
                    "WuzAPI group participant has conflicting identifiers"
                )
            seen.add(key)
            addresses.append(address)
        if not addresses:
            raise AdapterError("WuzAPI group participant has no valid identifier")

        raw_is_admin = self._provider_lookup(values, "IsAdmin")
        raw_is_superadmin = self._provider_lookup(values, "IsSuperAdmin")
        if raw_is_admin is not None and not isinstance(raw_is_admin, bool):
            raise AdapterError("WuzAPI group participant admin flag is invalid")
        if raw_is_superadmin is not None and not isinstance(raw_is_superadmin, bool):
            raise AdapterError("WuzAPI group participant superadmin flag is invalid")
        if raw_is_superadmin is True:
            role = "superadmin"
        elif raw_is_admin is True:
            role = "admin"
        else:
            role = "member"

        preferred = next(
            (address for address in addresses if address.namespace == "whatsapp.lid"),
            None,
        ) or next(
            (address for address in addresses if address.namespace == "whatsapp.pn"),
            addresses[0],
        )
        raw_display_name = self._provider_lookup(values, "DisplayName")
        if raw_display_name not in (None, "") and not isinstance(raw_display_name, str):
            raise AdapterError("WuzAPI group participant display name is invalid")
        return GroupParticipantDTO(
            participant_ref=preferred.value_normalized,
            addresses=tuple(addresses),
            display_name=self._group_safe_display_name(raw_display_name),
            role=role,
        )

    def _group_own_observation(self, connection, participants, addressing_mode):
        configured = str(connection.account_id.own_external_identity or "").strip()
        if not configured:
            return "unknown", None
        configured_normalized = self._provider_normalize_session_identity(configured)
        matches = tuple(
            participant
            for participant in participants
            if any(
                address.value_normalized == configured_normalized
                for address in participant.addresses
            )
        )
        if len(matches) != 1:
            return "unknown", None
        own_participant = matches[0]
        expected_namespace = {
            "lid": "whatsapp.lid",
            "pn": "whatsapp.pn",
        }[addressing_mode]
        primary = tuple(
            address
            for address in own_participant.addresses
            if address.role == "primary" and address.namespace == expected_namespace
        )
        if len(primary) != 1:
            raise AdapterError(
                "WuzAPI own group participant contradicts AddressingMode"
            )
        observed = primary[0]
        return own_participant.role, AddressDTO(
            namespace=observed.namespace,
            value=observed.value_normalized,
            value_normalized=observed.value_normalized,
            role="sender",
            source_field=observed.source_field,
            confidence="protocol",
        )

    def _group_participant_count(self, values, participants):
        raw_count = self._provider_lookup(values, "ParticipantCount")
        if raw_count is None:
            # Absence of the provider's total is not proof that the returned list
            # is complete. Preserve existing roster entries until a counted pull.
            return len(participants), False
        if (
            isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count < len(participants)
        ):
            raise AdapterError("WuzAPI group participant count is invalid")
        return raw_count, raw_count == len(participants)

    @staticmethod
    def _validated_avatar_url(value, label):
        if not isinstance(value, str):
            raise AdapterError("WuzAPI %s URL is invalid" % label)
        value = value.strip()
        if (
            not value
            or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise AdapterError("WuzAPI %s URL is invalid" % label)
        try:
            parsed = urlsplit(value)
            hostname = (parsed.hostname or "").encode("idna").decode("ascii").lower()
            port = parsed.port
        except ValueError as error:
            raise AdapterError("WuzAPI %s URL is invalid" % label) from error
        normalized_hostname = hostname.rstrip(".")
        if (
            parsed.scheme != "https"
            or not normalized_hostname
            or parsed.username
            or parsed.password
            or "@" in parsed.netloc
            or parsed.fragment
            or port not in (None, 443)
            or not any(
                normalized_hostname == suffix
                or normalized_hostname.endswith(".%s" % suffix)
                for suffix in _AVATAR_HOST_SUFFIXES
            )
        ):
            raise AdapterError("WuzAPI %s URL is not allowed" % label)
        try:
            resolved = socket.getaddrinfo(
                normalized_hostname,
                port or 443,
                type=socket.SOCK_STREAM,
            )
        except OSError as error:
            raise TransientAdapterError(
                "WuzAPI %s hostname could not be resolved" % label
            ) from error
        if not resolved:
            raise TransientAdapterError(
                "WuzAPI %s hostname could not be resolved" % label
            )
        for result in resolved:
            try:
                address = ipaddress.ip_address(result[4][0])
            except (IndexError, ValueError) as error:
                raise AdapterError("WuzAPI %s address is invalid" % label) from error
            if not address.is_global:
                raise AdapterError("WuzAPI %s address is not public" % label)
        return value

    @staticmethod
    def _sniff_avatar(content, label):
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", ".jpg"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", ".png"
        if (
            len(content) >= 12
            and content.startswith(b"RIFF")
            and content[8:12] == b"WEBP"
        ):
            return "image/webp", ".webp"
        raise AdapterError("WuzAPI %s content is not a supported image" % label)

    def _group_hint_values(self, provider_event_type, raw_event):
        if provider_event_type == "GroupInfo":
            group_value = self._provider_lookup(raw_event, "JID")
            occurred_at = self._provider_parse_timestamp(
                self._provider_lookup(raw_event, "Timestamp")
            )
            name_change = self._provider_lookup(raw_event, "Name")
            display_name = (
                self._group_safe_display_name(
                    self._provider_lookup(name_change, "Name")
                )
                if isinstance(name_change, dict)
                else ""
            )
            kind = "metadata"
            provider_revision = self._group_safe_provider_revision(
                self._provider_lookup(raw_event, "ParticipantVersionID")
            )
            avatar_changed = False
            avatar_removed = False
        elif provider_event_type == "JoinedGroup":
            group_value = self._provider_lookup(raw_event, "JID")
            # JoinedGroup has no event timestamp. GroupCreated is the age of the
            # group, not the time this session joined.
            occurred_at = datetime.datetime.now(datetime.timezone.utc)
            display_name = self._group_safe_display_name(
                self._provider_lookup(raw_event, "Name")
            )
            kind = "joined"
            provider_revision = self._group_safe_provider_revision(
                self._provider_lookup(raw_event, "ParticipantVersionID")
            )
            avatar_changed = True
            avatar_removed = False
        else:
            group_value = self._provider_lookup(raw_event, "JID")
            occurred_at = self._provider_parse_timestamp(
                self._provider_lookup(raw_event, "Timestamp")
            )
            display_name = ""
            kind = "picture"
            provider_revision = self._group_safe_provider_revision(
                self._provider_lookup(raw_event, "PictureID")
            )
            avatar_changed = True
            avatar_removed = self._provider_boolean(
                self._provider_lookup(raw_event, "Remove")
            )

        group_address = self._group_strict_address(
            group_value, "event.%s.JID" % provider_event_type
        )
        hint = {"kind": kind}
        if display_name:
            hint["display_name"] = display_name
        if provider_revision:
            hint["provider_revision"] = provider_revision
        if avatar_changed:
            hint["avatar_changed"] = True
        if avatar_removed:
            hint["avatar_removed"] = True
        return group_address, occurred_at, hint

    @staticmethod
    def _group_hint_event_id(
        connection, provider_event_type, group_address, occurred_at, hint
    ):
        evidence = {
            "connection_ref": connection.external_ref,
            "provider_event_type": provider_event_type,
            "conversation_ref": group_address.value_normalized,
            "hint": hint,
        }
        if provider_event_type != "JoinedGroup":
            evidence["occurred_at"] = occurred_at.isoformat()
        digest = hashlib.sha256(
            json.dumps(
                evidence,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return "%s:%s" % (provider_event_type, digest)

    def _normalize_group_metadata_hint(
        self, connection, envelope, provider_event_type, raw_event
    ):
        if provider_event_type == "Picture":
            picture_jid = self._provider_normalize_jid(
                self._provider_lookup(raw_event, "JID")
            )
            if not picture_jid.endswith("@g.us"):
                return self._normalize_identity_avatar_hint(
                    connection, envelope, raw_event
                )
        group_address, occurred_at, hint = self._group_hint_values(
            provider_event_type, raw_event
        )
        event_id = self._group_hint_event_id(
            connection,
            provider_event_type,
            group_address,
            occurred_at,
            hint,
        )
        return EventDTO(
            provider_schema_version=self.provider_version,
            event_id=event_id,
            event_type="group.metadata.changed",
            occurred_at=occurred_at,
            account_ref=connection.account_id.external_ref,
            connection_ref=connection.external_ref,
            conversation_ref=group_address.value_normalized,
            platform=connection.account_id.platform,
            direction="inbound",
            is_from_me=False,
            origin="provider",
            actor=ActorDTO(),
            conversation=ConversationDTO(
                addresses=(group_address,),
                conversation_type="group",
            ),
            extensions={
                "group_metadata_hint": hint,
                "provider.wuzapi": dict(
                    self._provider_event_metadata(envelope),
                    event_type=provider_event_type,
                    occurred_at_source=(
                        "odoo_received"
                        if provider_event_type == "JoinedGroup"
                        else "provider"
                    ),
                ),
            },
        )

    def _normalize_identity_avatar_hint(self, connection, envelope, raw_event):
        # ``Picture.JID`` is the profile whose picture changed. ``Picture.Author``
        # is the actor who performed the change and must not be treated as a PN/LID
        # alias of the profile (notably, group events commonly have a different
        # author). The pinned whatsmeow event has no alternate target JID.
        address = self._identity_avatar_address(
            self._provider_lookup(raw_event, "JID"), "event.Picture.JID"
        )
        occurred_at = self._provider_parse_timestamp(
            self._provider_lookup(raw_event, "Timestamp")
        )
        raw_revision = self._provider_lookup(raw_event, "PictureID")
        provider_revision = self._group_safe_provider_revision(raw_revision)
        if raw_revision not in (None, "") and not provider_revision:
            raise AdapterError("WuzAPI identity avatar revision is invalid")
        hint = {"kind": "picture", "avatar_changed": True}
        if provider_revision:
            hint["provider_revision"] = provider_revision
        if self._provider_boolean(self._provider_lookup(raw_event, "Remove")):
            hint["avatar_removed"] = True
        event_id = self._group_hint_event_id(
            connection, "Picture", address, occurred_at, hint
        )
        return EventDTO(
            provider_schema_version=self.provider_version,
            event_id=event_id,
            event_type="identity.avatar.changed",
            occurred_at=occurred_at,
            account_ref=connection.account_id.external_ref,
            connection_ref=connection.external_ref,
            conversation_ref=address.value_normalized,
            platform=connection.account_id.platform,
            direction="inbound",
            is_from_me=False,
            origin="provider",
            actor=ActorDTO(addresses=(address,)),
            conversation=ConversationDTO(
                addresses=(address,),
                conversation_type="direct",
            ),
            extensions={
                "identity_avatar_hint": hint,
                "provider.wuzapi": dict(
                    self._provider_event_metadata(envelope),
                    event_type="Picture",
                    occurred_at_source="provider",
                ),
            },
        )

    @staticmethod
    def _group_raise_read_error(status, label, retry_after_seconds=0):
        if status in (401, 403):
            raise ProviderPausedError(
                "WuzAPI rejected the configured API token while reading %s" % label
            )
        if status == 429:
            error = ProviderRateLimitError("WuzAPI %s request was rate limited" % label)
            error.retry_after_seconds = retry_after_seconds
            raise error
        if status >= 500:
            error = TransientAdapterError(
                "WuzAPI %s request failed with HTTP %s" % (label, status)
            )
            error.retry_after_seconds = retry_after_seconds
            raise error
        raise AdapterError("WuzAPI %s request failed with HTTP %s" % (label, status))

    def _group_success_data(self, payload, label):
        if not isinstance(payload, dict):
            raise AdapterError("WuzAPI %s response is invalid" % label)
        code = self._provider_lookup(payload, "code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise AdapterError("WuzAPI %s response code is invalid" % label)
        if self._provider_lookup(payload, "success") is not True:
            self._group_raise_read_error(code, label)
        if not 200 <= code < 300:
            self._group_raise_read_error(code, label)
        data = self._provider_lookup(payload, "data")
        if not isinstance(data, dict):
            raise AdapterError("WuzAPI %s response has no data object" % label)
        return data

    def _group_metadata_participants(self, data):
        raw_participants = self._provider_lookup(data, "Participants")
        if not isinstance(raw_participants, list):
            raise AdapterError("WuzAPI group metadata participants are invalid")
        try:
            participants = tuple(
                sorted(
                    (
                        self._group_participant_from_provider(values)
                        for values in raw_participants
                    ),
                    key=lambda participant: participant.participant_ref,
                )
            )
        except DTOValidationError as error:
            raise AdapterError("WuzAPI group metadata roster is invalid") from error
        participant_refs = [participant.participant_ref for participant in participants]
        address_keys = [
            (address.namespace, address.value_normalized)
            for participant in participants
            for address in participant.addresses
        ]
        if len(set(participant_refs)) != len(participant_refs) or len(
            set(address_keys)
        ) != len(address_keys):
            raise AdapterError("WuzAPI group metadata roster is invalid")
        return participants

    def fetch_group_metadata(self, connection, conversation_ref):
        group_address = self._group_strict_address(
            conversation_ref, "group_metadata.conversation_ref"
        )
        headers = dict(self._provider_headers(connection))
        headers.pop("Content-Type", None)
        try:
            response = requests.request(
                "GET",
                "%s/group/info" % connection.wuzapi_base_url,
                headers=headers,
                params={"groupJID": group_address.value_normalized},
                timeout=_GROUP_METADATA_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as error:
            raise TransientAdapterError(
                "WuzAPI group metadata request did not return a response"
            ) from error
        if not 200 <= response.status_code < 300:
            status = response.status_code
            retry_after_seconds = self._provider_retry_after(response)
            provider_error = self._group_bounded_provider_error(response)
            response.close()
            if any(
                marker in provider_error for marker in _GROUP_TERMINAL_ERROR_MARKERS
            ):
                raise UnsupportedEventError("WuzAPI group is no longer available")
            self._group_raise_read_error(status, "group metadata", retry_after_seconds)
        payload = self._group_limited_json_object(
            response,
            _MAX_GROUP_METADATA_RESPONSE_BYTES,
            "WuzAPI group metadata",
        )
        provider_error = self._group_provider_error(payload)
        if self._provider_lookup(payload, "success") is not True and any(
            marker in provider_error for marker in _GROUP_TERMINAL_ERROR_MARKERS
        ):
            raise UnsupportedEventError("WuzAPI group is no longer available")
        data = self._group_success_data(payload, "group metadata")

        response_group = self._group_strict_address(
            self._provider_lookup(data, "JID"), "data.JID"
        )
        if response_group.value_normalized != group_address.value_normalized:
            raise AdapterError("WuzAPI group metadata response JID does not match")

        raw_name = self._provider_lookup(data, "Name")
        if not isinstance(raw_name, str):
            raise AdapterError("WuzAPI group metadata name is invalid")
        display_name = self._group_safe_display_name(raw_name)
        if raw_name.strip() and not display_name:
            raise AdapterError("WuzAPI group metadata name is unsafe")

        participants = self._group_metadata_participants(data)
        addressing_mode = self._provider_lookup(data, "AddressingMode")
        if addressing_mode not in ("pn", "lid"):
            raise AdapterError("WuzAPI group AddressingMode is invalid")
        own_role, own_protocol_participant = self._group_own_observation(
            connection, participants, addressing_mode
        )
        participant_count, is_complete = self._group_participant_count(
            data, participants
        )
        raw_participant_version = self._provider_lookup(data, "ParticipantVersionID")
        participant_version = self._group_safe_provider_revision(
            raw_participant_version
        )
        if raw_participant_version not in (None, "") and not participant_version:
            raise AdapterError("WuzAPI group participant revision is invalid")
        try:
            return GroupMetadataDTO(
                conversation_ref=group_address.value_normalized,
                observed_at=datetime.datetime.now(datetime.timezone.utc),
                participants=participants,
                display_name=display_name,
                participant_count=participant_count,
                own_role=own_role,
                own_protocol_participant=own_protocol_participant,
                # ParticipantVersionID is opaque. Equality is meaningful; numeric
                # or lexical ordering is not guaranteed by WuzAPI/whatsmeow.
                provider_revision=participant_version,
                is_complete=is_complete,
            )
        except DTOValidationError as error:
            raise AdapterError("WuzAPI group metadata response is invalid") from error

    def _read_avatar_response(self, response, label):
        self._group_enforce_content_length(
            response, _MAX_AVATAR_BYTES, "WuzAPI %s" % label
        )
        chunks = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MAX_AVATAR_BYTES:
                    raise AdapterError("WuzAPI %s exceeds the size limit" % label)
                chunks.append(chunk)
        except requests.RequestException as error:
            raise TransientAdapterError(
                "WuzAPI %s response was interrupted" % label
            ) from error
        finally:
            response.close()
        content = b"".join(chunks)
        if not content:
            raise AdapterError("WuzAPI %s response is empty" % label)
        return content

    def _fetch_avatar(self, connection, address, *, label, file_stem):
        try:
            response = requests.request(
                "POST",
                "%s/user/avatar" % connection.wuzapi_base_url,
                headers=self._provider_headers(connection),
                json={"Phone": address.value_normalized, "Preview": True},
                timeout=_AVATAR_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as error:
            raise TransientAdapterError(
                "WuzAPI %s lookup did not return a response" % label
            ) from error
        if not 200 <= response.status_code < 300:
            error_message = self._group_bounded_provider_error(response)
            status = response.status_code
            retry_after_seconds = self._provider_retry_after(response)
            response.close()
            if any(marker in error_message for marker in _AVATAR_ABSENT_MARKERS):
                return AvatarResult(state="absent")
            if any(marker in error_message for marker in _AVATAR_UNAVAILABLE_MARKERS):
                return AvatarResult(state="unavailable")
            self._group_raise_read_error(status, label, retry_after_seconds)
        payload = self._group_limited_json_object(
            response, 64 * 1024, "WuzAPI %s" % label
        )
        provider_error = self._group_provider_error(payload)
        if self._provider_lookup(payload, "success") is not True:
            if any(marker in provider_error for marker in _AVATAR_ABSENT_MARKERS):
                return AvatarResult(state="absent")
            if any(marker in provider_error for marker in _AVATAR_UNAVAILABLE_MARKERS):
                return AvatarResult(state="unavailable")
        data = self._group_success_data(payload, label)
        raw_revision = self._provider_lookup(data, "ID")
        provider_revision = self._group_safe_provider_revision(raw_revision)
        if raw_revision not in (None, "") and not provider_revision:
            raise AdapterError("WuzAPI %s revision is invalid" % label)
        avatar_url = self._provider_lookup(data, "URL")
        if not avatar_url:
            return AvatarResult(
                state="unavailable" if provider_revision else "absent",
                provider_revision=provider_revision,
            )
        avatar_url = self._validated_avatar_url(avatar_url, label)

        try:
            image_response = requests.request(
                "GET",
                avatar_url,
                headers={"Accept": "image/*"},
                timeout=_AVATAR_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as error:
            raise TransientAdapterError(
                "WuzAPI %s download did not return a response" % label
            ) from error
        if image_response.status_code in (404, 410):
            image_response.close()
            return AvatarResult(
                state="unavailable", provider_revision=provider_revision
            )
        if not 200 <= image_response.status_code < 300:
            status = image_response.status_code
            retry_after_seconds = self._provider_retry_after(image_response)
            image_response.close()
            self._group_raise_read_error(
                status, "%s download" % label, retry_after_seconds
            )
        advertised_mime = self._provider_header(image_response.headers, "Content-Type")
        advertised_mime = advertised_mime.split(";", 1)[0].strip().lower()
        content = self._read_avatar_response(image_response, label)
        mime_type, extension = self._sniff_avatar(content, label)
        if advertised_mime and advertised_mime != mime_type:
            raise AdapterError("WuzAPI %s MIME type does not match content" % label)
        return AvatarResult(
            state="ready",
            provider_revision=provider_revision,
            content=content,
            mime_type=mime_type,
            file_name="%s%s" % (file_stem, extension),
        )

    def fetch_group_avatar(self, connection, conversation_ref):
        group_address = self._group_strict_address(
            conversation_ref, "group_avatar.conversation_ref"
        )
        return self._fetch_avatar(
            connection,
            group_address,
            label="group avatar",
            file_stem="group-avatar",
        )

    def fetch_identity_avatar(self, connection, address):
        address = self._validated_identity_avatar_address(address)
        return self._fetch_avatar(
            connection,
            address,
            label="identity avatar",
            file_stem="identity-avatar",
        )

    def _identity_profile_request(
        self, connection, method, endpoint, label, *, payload=None, maximum_bytes
    ):
        headers = dict(self._provider_headers(connection))
        if method == "GET":
            headers.pop("Content-Type", None)
        try:
            response = requests.request(
                method,
                "%s%s" % (connection.wuzapi_base_url, endpoint),
                headers=headers,
                json=payload,
                timeout=_GROUP_METADATA_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as error:
            raise TransientAdapterError(
                "WuzAPI %s request did not return a response" % label
            ) from error
        if not 200 <= response.status_code < 300:
            status = response.status_code
            retry_after_seconds = self._provider_retry_after(response)
            self._group_bounded_provider_error(response)
            self._group_raise_read_error(status, label, retry_after_seconds)
        response_payload = self._group_limited_json_object(
            response, maximum_bytes, "WuzAPI %s" % label
        )
        return self._group_success_data(response_payload, label)

    @staticmethod
    def _identity_profile_phone_query(address):
        if address.namespace == "whatsapp.lid":
            # IsOnWhatsApp accepts phone numbers, not opaque LIDs. A contacts
            # cache must be pulled once per connection, never once per binding.
            return ""
        return "".join(
            character
            for character in address.value_normalized.split("@", 1)[0]
            if character.isdigit()
        )

    def _identity_profile_verified_name(self, connection, address):
        query = self._identity_profile_phone_query(address)
        if not query:
            return ""
        data = self._identity_profile_request(
            connection,
            "POST",
            "/user/check",
            "identity check",
            payload={"Phone": [query]},
            maximum_bytes=_MAX_IDENTITY_CHECK_RESPONSE_BYTES,
        )
        users = self._provider_lookup(data, "Users")
        if not isinstance(users, list) or len(users) > 4:
            raise AdapterError("WuzAPI identity check users are invalid")
        verified_name = ""
        for user in users:
            if not isinstance(user, dict):
                raise AdapterError("WuzAPI identity check user is invalid")
            response_query = self._provider_lookup(user, "Query")
            if (
                not isinstance(response_query, str)
                or response_query.lstrip("+") != query
            ):
                raise AdapterError("WuzAPI identity check query does not match")
            is_in_whatsapp = self._provider_lookup(user, "IsInWhatsapp")
            if not isinstance(is_in_whatsapp, bool):
                raise AdapterError("WuzAPI identity check registration flag is invalid")
            raw_jid = self._provider_jid_string(self._provider_lookup(user, "JID"))
            normalized_jid = self._provider_normalize_jid(raw_jid)
            if raw_jid:
                if not _STRICT_PARTICIPANT_JID_PATTERN.fullmatch(normalized_jid):
                    raise AdapterError("WuzAPI identity check JID is invalid")
                if (
                    normalized_jid.endswith("@s.whatsapp.net")
                    and normalized_jid.split("@", 1)[0] != query
                ):
                    raise AdapterError("WuzAPI identity check JID does not match")
            raw_verified_name = self._provider_lookup(user, "VerifiedName")
            if raw_verified_name not in (None, "") and not isinstance(
                raw_verified_name, str
            ):
                raise AdapterError("WuzAPI identity verified name is invalid")
            safe_verified_name = self._group_safe_display_name(raw_verified_name)
            if len(safe_verified_name) > 255:
                safe_verified_name = ""
            # VerifiedName is optional enrichment. A provider-side value with
            # controls or excessive length must never be projected, but it must
            # not prevent the independent avatar/fallback-phone projection.
            if raw_verified_name and not safe_verified_name:
                continue
            if safe_verified_name and is_in_whatsapp:
                if verified_name and verified_name != safe_verified_name:
                    raise AdapterError("WuzAPI identity verified names conflict")
                verified_name = safe_verified_name
        return verified_name

    def fetch_identity_profile(self, connection, address):
        address = self._validated_identity_avatar_address(address)
        verified_name = self._identity_profile_verified_name(connection, address)
        avatar = self._fetch_avatar(
            connection,
            address,
            label="identity avatar",
            file_stem="identity-avatar",
        )
        try:
            return IdentityProfileResult(
                display_name=verified_name,
                avatar=avatar,
            )
        except DTOValidationError as error:
            raise AdapterError("WuzAPI identity profile response is invalid") from error
