import base64
import binascii
import copy
import datetime
import hashlib
import hmac
import json
import math
import mimetypes
import re
import uuid
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import NewConnectionError

from odoo.exceptions import ValidationError

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    ProviderAdapter,
    ProviderPausedError,
    ProviderRateLimitError,
    TransientAdapterError,
    UnsupportedEventError,
    adapter_registry,
)
from odoo.addons.contact_center_base.services.dto import (
    MAX_ATTRIBUTION_DELAY_SECONDS,
    ActorDTO,
    AdapterResult,
    AddressDTO,
    AttributionDTO,
    CommandDTO,
    ConversationDTO,
    DTOValidationError,
    EventDTO,
    ExternalIdentifierDTO,
    MediaDownloadResult,
    MediaDTO,
    MessageDTO,
)
from odoo.addons.contact_center_base.services.media import (
    canonical_recorded_audio_duration_seconds,
    is_iso_bmff_audio_only,
    is_ogg_opus,
)

from .group import WuzapiGroupMetadataMixin
from .structured_content import (
    OUTBOUND_STRUCTURED_CONTENT,
    build_outbound_content,
    normalize_structured_content,
    validate_outbound_content,
)

WUZAPI_VERSION = "v1.0.8"
WUZAPI_COMMIT = "9487eca"
MAX_PROVIDER_RETRY_AFTER_SECONDS = 60 * 60

# These are the lifecycle event names exposed by the pinned WuzAPI revision.
# WuzAPI serializes the underlying whatsmeow event in ``event``; some events are
# empty objects and QRTimeout/one ConnectFailure path use a scalar instead.
WUZAPI_LIFECYCLE_EVENT_STATES = {
    "Connected": "connected",
    "KeepAliveRestored": "connected",
    "Disconnected": "disconnected",
    "ConnectFailure": "disconnected",
    "StreamReplaced": "disconnected",
    "KeepAliveTimeout": "degraded",
    "StreamError": "degraded",
    "LoggedOut": "authentication_required",
    "QRTimeout": "authentication_required",
    "ClientOutdated": "error",
    "TemporaryBan": "paused",
}
WUZAPI_LIFECYCLE_EVENT_TYPES = frozenset(WUZAPI_LIFECYCLE_EVENT_STATES)

# Exact event catalog exposed by the pinned WuzAPI revision. Keep this list tied
# to ``WUZAPI_COMMIT``: a provider upgrade requires fixtures and adapter review
# before a new event can be selected from Odoo.
WUZAPI_WEBHOOK_EVENT_TYPES = frozenset(
    {
        "All",
        "AppState",
        "AppStateSyncComplete",
        "Blocklist",
        "BlocklistChange",
        "CallAccept",
        "CallOffer",
        "CallOfferNotice",
        "CallRelayLatency",
        "CallTerminate",
        "CATRefreshError",
        "ChatPresence",
        "ClientOutdated",
        "Connected",
        "ConnectFailure",
        "Disconnected",
        "FBMessage",
        "GroupInfo",
        "HistorySync",
        "IdentityChange",
        "JoinedGroup",
        "KeepAliveRestored",
        "KeepAliveTimeout",
        "LoggedOut",
        "MediaRetry",
        "Message",
        "NewsletterJoin",
        "NewsletterLeave",
        "NewsletterLiveUpdate",
        "NewsletterMuteChange",
        "OfflineSyncCompleted",
        "OfflineSyncPreview",
        "PairError",
        "PairSuccess",
        "Picture",
        "Presence",
        "PrivacySettings",
        "PushNameSetting",
        "QR",
        "QRScannedWithoutMultidevice",
        "QRTimeout",
        "ReadReceipt",
        "Receipt",
        "StreamError",
        "StreamReplaced",
        "TemporaryBan",
        "UndecryptableMessage",
        "UserAbout",
    }
)
WUZAPI_DEFAULT_WEBHOOK_EVENTS = frozenset(
    {
        "CallAccept",
        "CallOffer",
        "CallTerminate",
        "ClientOutdated",
        "ConnectFailure",
        "Connected",
        "Disconnected",
        "GroupInfo",
        "IdentityChange",
        "JoinedGroup",
        "KeepAliveRestored",
        "KeepAliveTimeout",
        "LoggedOut",
        "Message",
        "Picture",
        "QRTimeout",
        "ReadReceipt",
        "StreamError",
        "StreamReplaced",
        "TemporaryBan",
    }
)
WUZAPI_HUMAN_MESSAGE_WRAPPER_FIELDS = (
    "associatedChildMessage",
    "botInvokeMessage",
    "deviceSentMessage",
    "documentWithCaptionMessage",
    "ephemeralMessage",
    "lottieStickerMessage",
    "pollCreationMessageV4",
    "viewOnceMessage",
    "viewOnceMessageV2",
    "viewOnceMessageV2Extension",
)
_CALL_EVENT_STATES = {
    "CallOffer": "offered",
    "CallAccept": "accepted",
    "CallTerminate": "terminated",
}

_HTTP_TIMEOUT = (5, 30)
_HEALTH_TIMEOUT = (3, 10)
_MEDIA_DOWNLOAD_TIMEOUT = (5, 180)
_MAX_MEDIA_ERROR_RESPONSE_BYTES = 16 * 1024
_WEBHOOK_CONFIGURATION_TIMEOUT = (3, 15)
_MAX_WEBHOOK_CONFIGURATION_RESPONSE_BYTES = 64 * 1024
_HMAC_CONFIGURATION_TIMEOUT = (3, 15)
_MAX_HMAC_CONFIGURATION_RESPONSE_BYTES = 16 * 1024
_HMAC_CONFIGURATION_SUCCESS_DETAIL = "HMAC configuration saved successfully"
_MAX_COMMAND_RESPONSE_BYTES = 64 * 1024
_MAX_HEALTH_RESPONSE_BYTES = 64 * 1024
_JID_PATTERN = re.compile(r"^([^@\s]+)@([^@\s]+)$")
_STRICT_PARTICIPANT_JID_PATTERN = re.compile(
    r"^[0-9]+(?::[0-9]+)?@(s\.whatsapp\.net|lid)$"
)
_STRICT_NORMALIZED_PARTICIPANT_JID_PATTERN = re.compile(
    r"^[0-9]+@(s\.whatsapp\.net|lid)$"
)
_DEVICE_SUFFIX_PATTERN = re.compile(r":\d+$")
_HMAC_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_MESSAGE_ID_PATTERN = re.compile(r"^[A-F0-9]{32}$")
_TARGET_MESSAGE_ID_MAX_CHARS = 256
_MAX_JID_CHARS = 255
_MAX_RECEIPT_MESSAGE_IDS = 100
_MAX_TEXT_CHARS = 65536
_MAX_MARK_READ_MESSAGE_IDS = 100
_MAX_MARK_READ_MESSAGE_ID_CHARS = 512
_MAX_HUMAN_SUMMARY_ITEMS = 20
_UNSUPPORTED_HUMAN_CONTENT_TEXT = "[Conteúdo do WhatsApp não suportado]"
_PROTOCOL_ONLY_MESSAGE_FIELDS = frozenset(
    {
        "appstatesynckeyshare",
        "enccommentmessage",
        "encreactionmessage",
        "fastratchetkeysenderkeydistributionmessage",
        "messagedistributionmessage",
        "messagecontextinfo",
        "placeholdermessage",
        "protocolmessage",
        "secretencryptedmessage",
        "senderkeydistributionmessage",
        "stickersyncrmrmessage",
    }
)
_HUMAN_FALLBACK_MESSAGE_FIELDS = frozenset(
    {
        "bcallmessage",
        "call",
        "calllogmesssage",
        "eventinvitemessage",
        "eventmessage",
        "groupinvitemessage",
        "highlystructuredmessage",
        "invoicemessage",
        "ordermessage",
        "paymentinvitemessage",
        "paymentremindermessage",
        "productmessage",
        "questionmessage",
        "requestpaymentmessage",
        "requestphonenumbermessage",
        "richresponsemessage",
        "scheduledcallcreationmessage",
        "scheduledcalleditmessage",
        "sendpaymentmessage",
        "splitpaymentmessage",
        "splitpaymentupdatemessage",
    }
)
_KNOWN_NON_PAID_ENTRY_POINTS = frozenset(
    {
        "click_to_chat_link",
        "global_search_new_chat",
        "phone_number_hyperlink",
        "status",
    }
)
_META_PAID_CONVERSION_SOURCES = frozenset(
    {
        "fb_ads",
        "facebook_ads",
        "meta_ads",
    }
)
_ATTRIBUTION_CONTEXT_SKIP_KEYS = frozenset(
    {
        "quotedmessage",
        "rawmessage",
        "sourcewebmsg",
    }
)
_ATTRIBUTION_CONTEXT_FIELDS = (
    ("conversionSource", ("conversionSource",)),
    ("conversionData", ("conversionData",)),
    ("ctwaPayload", ("ctwaPayload",)),
    ("ctwaSignals", ("ctwaSignals",)),
    ("entryPointConversionSource", ("entryPointConversionSource",)),
    ("entryPointConversionApp", ("entryPointConversionApp",)),
    ("entryPointConversionDelaySeconds", ("entryPointConversionDelaySeconds",)),
    ("entryPointConversionExternalSource", ("entryPointConversionExternalSource",)),
    ("entryPointConversionExternalMedium", ("entryPointConversionExternalMedium",)),
    ("conversionDelaySeconds", ("conversionDelaySeconds",)),
    ("alwaysShowAdAttribution", ("alwaysShowAdAttribution",)),
)
_ATTRIBUTION_EXTERNAL_FIELDS = (
    ("sourceType", ("sourceType",)),
    ("sourceApp", ("sourceApp",)),
    ("sourceID", ("sourceID", "sourceId")),
    ("ctwaClid", ("ctwaClid", "ctwaCLID")),
    ("sourceURL", ("sourceURL", "sourceUrl")),
    ("mediaType", ("mediaType",)),
    ("showAdAttribution", ("showAdAttribution",)),
    ("conversionData", ("conversionData",)),
    ("ctwaPayload", ("ctwaPayload",)),
    ("ctwaSignals", ("ctwaSignals",)),
)
_ATTRIBUTION_UTM_FIELDS = (
    ("source", ("source", "utmSource")),
    ("medium", ("medium", "utmMedium")),
    ("campaign", ("campaign", "utmCampaign")),
    ("content", ("content", "utmContent")),
    ("term", ("term", "utmTerm")),
)
_MAX_MEDIA_BYTES = {
    "image": 16 * 1024 * 1024,
    "audio": 16 * 1024 * 1024,
    "video": 50 * 1024 * 1024,
    "document": 50 * 1024 * 1024,
}
_MEDIA_FIELDS = {
    "image": ("imageMessage", "Image", "/chat/send/image", "/chat/downloadimage"),
    "audio": ("audioMessage", "Audio", "/chat/send/audio", "/chat/downloadaudio"),
    "video": ("videoMessage", "Video", "/chat/send/video", "/chat/downloadvideo"),
    "document": (
        "documentMessage",
        "Document",
        "/chat/send/document",
        "/chat/downloaddocument",
    ),
}
_ASSOCIATED_CHILD_MEDIA_ASSOCIATION_TYPES = {
    "1": "media_album",
    "5": "hd_video_dual_upload",
    "10": "hd_image_dual_upload",
    "11": "sticker_annotation",
    "12": "motion_photo",
    "19": "hevc_video_dual_upload",
    "mediaalbum": "media_album",
    "hdvideodualupload": "hd_video_dual_upload",
    "hdimagedualupload": "hd_image_dual_upload",
    "stickerannotation": "sticker_annotation",
    "motionphoto": "motion_photo",
    "hevcvideodualupload": "hevc_video_dual_upload",
}
_MAX_ASSOCIATED_CHILD_INDEX = 10000
_MIME_BY_KIND = {
    "image": {"image/jpeg", "image/png"},
    "audio": None,
    "video": {"video/mp4", "video/3gpp"},
    "document": None,
}


class _InvalidMediaHmacError(TransientAdapterError):
    """Identify WuzAPI's deterministic media-key derivation mismatch."""


def _is_invalid_media_hmac_response(status, payload):
    if (
        status != 500
        or not isinstance(payload, dict)
        or payload.get("code") != 500
        or payload.get("success") is not False
    ):
        return False
    provider_error = payload.get("error")
    if not isinstance(provider_error, str):
        return False
    terminal_cause = provider_error.strip().lower().rsplit(":", 1)[-1].strip()
    return terminal_cause == "invalid media hmac"


def _media_capabilities():
    capabilities = {}
    for kind in _MEDIA_FIELDS:
        values = {
            "enabled": True,
            "max_bytes": _MAX_MEDIA_BYTES[kind],
        }
        if kind == "audio":
            # WuzAPI v1.0.8 accepts Caption in the request schema but drops it
            # while constructing AudioMessage. Advertise the effective contract.
            values["caption"] = False
            values.update(
                {
                    "recording_mimetypes": [
                        "audio/ogg;codecs=opus",
                        "audio/mp4",
                    ],
                    "voice_note_mimetypes": ["audio/ogg;codecs=opus"],
                    "max_duration_seconds": 15 * 60,
                }
            )
        if _MIME_BY_KIND[kind]:
            values["mimetypes"] = sorted(_MIME_BY_KIND[kind])
        capabilities[kind] = values
    return capabilities


def _normalized_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _lookup(values, *field_names):
    if not isinstance(values, dict):
        return None
    for field_name in field_names:
        if field_name in values:
            return values[field_name]
    expected = {_normalized_key(field_name) for field_name in field_names}
    for key, value in values.items():
        if _normalized_key(key) in expected:
            return value
    return None


def _is_hmac_configuration_ack(payload):
    """Accept only the documented wrapper or the pinned handler's bare ACK."""

    success = _lookup(payload, "success")
    code = _lookup(payload, "code")
    data = _lookup(payload, "data")
    if success is True and code == 200 and isinstance(data, dict):
        details = _lookup(data, "Details")
        return isinstance(details, str) and bool(details.strip())

    normalized_keys = {_normalized_key(key) for key in payload}
    details = _lookup(payload, "Details")
    return bool(
        normalized_keys == {"details"}
        and isinstance(details, str)
        and details.strip().casefold() == _HMAC_CONFIGURATION_SUCCESS_DETAIL.casefold()
    )


def _source_value(values, *field_names):
    value = _lookup(values, *field_names)
    if value is not None:
        return value
    nested = _lookup(values, "MessageSource", "Source")
    return _lookup(nested, *field_names)


def _boolean(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes"):
            return True
        if normalized in ("false", "0", "no", ""):
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return False


def _optional_boolean(value):
    """Parse provider booleans without treating a missing field as false."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes"):
            return True
        if normalized in ("false", "0", "no"):
            return False
    if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
        return bool(value)
    return None


def _required_is_from_me(source, field_name):
    value = _optional_boolean(_source_value(source, "IsFromMe"))
    if value is None:
        raise AdapterError("%s is missing or invalid" % field_name)
    return value


def _provider_message_id(
    value,
    field_name,
    *,
    error_class=AdapterError,
    maximum=_TARGET_MESSAGE_ID_MAX_CHARS,
):
    if not isinstance(value, str) or not value.strip():
        raise error_class("%s is missing" % field_name)
    value = value.strip()
    if len(value) > maximum or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise error_class("%s is invalid" % field_name)
    return value


def _signed_whatsapp_text(text, options):
    """Render the provider-neutral sender signature for WhatsApp exactly once."""

    signature = (options or {}).get("sender_signature")
    if not signature:
        return text
    if not isinstance(signature, dict) or set(signature) != {"display_name"}:
        raise AdapterError("WuzAPI sender signature is invalid")
    display_name = signature.get("display_name")
    if not isinstance(display_name, str):
        raise AdapterError("WuzAPI sender signature is invalid")
    display_name = "".join(
        " " if ord(character) < 32 or ord(character) == 127 else character
        for character in display_name
    )
    # An asterisk inside the label would close WhatsApp's bold span early.
    display_name = " ".join(display_name.replace("*", "").split())[:120]
    if not display_name:
        raise AdapterError("WuzAPI sender signature is invalid")
    rendered = "*%s:*\n%s" % (display_name, text)
    if len(rendered) > _MAX_TEXT_CHARS:
        raise AdapterError("WuzAPI signed text exceeds the text limit")
    return rendered


def _jid_string(value):
    if isinstance(value, str):
        raw = value
    elif isinstance(value, dict):
        user = _lookup(value, "User")
        server = _lookup(value, "Server")
        if user is None or not server:
            return ""
        local = str(user).strip()
        device = _lookup(value, "Device")
        if device not in (None, "", 0, "0"):
            local = "%s:%s" % (local, device)
        raw = "%s@%s" % (local, str(server).strip())
    else:
        return ""
    normalized = raw.strip()
    if (
        raw != normalized
        or not normalized
        or len(normalized) > _MAX_JID_CHARS
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in normalized
        )
    ):
        return ""
    return normalized


def _normalize_jid(value):
    raw = _jid_string(value)
    match = _JID_PATTERN.fullmatch(raw)
    if not match:
        return raw.lower()
    local, server = match.groups()
    local = _DEVICE_SUFFIX_PATTERN.sub("", local)
    if re.fullmatch(r"\d+\.0", local):
        local = local[:-2]
    return "%s@%s" % (local, server.lower())


def _normalize_session_identity(value):
    """Normalize equivalent phone-number forms used for the own session JID."""

    normalized = _normalize_jid(value)
    plain = normalized[1:] if normalized.startswith("+") else normalized
    if plain.isdigit():
        return "%s@s.whatsapp.net" % plain
    if normalized.endswith("@c.us"):
        return normalized[: -len("@c.us")] + "@s.whatsapp.net"
    return normalized


def _jid_namespace(value):
    normalized = _normalize_jid(value)
    if normalized.endswith("@s.whatsapp.net") and re.fullmatch(
        r"[0-9]+@s\.whatsapp\.net", normalized
    ):
        return "whatsapp.pn"
    if normalized.endswith("@lid"):
        return "whatsapp.lid"
    if normalized.endswith("@g.us"):
        return "whatsapp.group"
    return "whatsapp.jid"


def _address(value, role, source_field):
    raw = _jid_string(value)
    normalized = _normalize_jid(value)
    if not raw or not normalized or not _JID_PATTERN.fullmatch(raw):
        return None
    namespace = _jid_namespace(value)
    return AddressDTO(
        namespace=namespace,
        value=raw,
        value_normalized=normalized,
        role=role,
        source_field=source_field,
        confidence="protocol",
        # A WhatsApp phone-number JID is portable between the company's boxes.
        # LIDs, groups and unknown JIDs remain scoped to the account that
        # observed them because their provider semantics are not portable.
        resolution_scope="company" if namespace == "whatsapp.pn" else "account",
    )


def _addresses(address_specs):
    result = []
    seen = set()
    for value, role, source_field in address_specs:
        address = _address(value, role, source_field)
        if not address:
            continue
        key = (address.namespace, address.value_normalized)
        if key in seen:
            continue
        seen.add(key)
        result.append(address)
    return tuple(result)


def _parse_timestamp(value):
    if isinstance(value, bool):
        raise AdapterError("WuzAPI event timestamp is invalid")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if abs(seconds) >= 100000000000:
            seconds /= 1000
        try:
            return datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc)
        except (OverflowError, OSError, ValueError) as error:
            raise AdapterError("WuzAPI event timestamp is invalid") from error
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", stripped):
            return _parse_timestamp(float(stripped))
        try:
            parsed = datetime.datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError as error:
            raise AdapterError("WuzAPI event timestamp is invalid") from error
        if parsed.tzinfo is None:
            raise AdapterError("WuzAPI event timestamp has no timezone")
        return parsed.astimezone(datetime.timezone.utc)
    raise AdapterError("WuzAPI event timestamp is missing")


def _event_metadata(envelope):
    return {
        "instance_name": str(_lookup(envelope, "instanceName") or ""),
        "user_id": str(_lookup(envelope, "userID") or ""),
    }


def _source_snapshot(source):
    return {
        "chat": _jid_string(_source_value(source, "Chat")),
        "chat_normalized": _normalize_jid(_source_value(source, "Chat")),
        "sender": _jid_string(_source_value(source, "Sender")),
        "sender_normalized": _normalize_jid(_source_value(source, "Sender")),
        "sender_alt": _jid_string(_source_value(source, "SenderAlt")),
        "sender_alt_normalized": _normalize_jid(_source_value(source, "SenderAlt")),
        "recipient_alt": _jid_string(_source_value(source, "RecipientAlt")),
        "recipient_alt_normalized": _normalize_jid(
            _source_value(source, "RecipientAlt")
        ),
        "addressing_mode": str(_source_value(source, "AddressingMode") or ""),
    }


def _conversation_values(source, prefix, is_from_me):
    if not isinstance(is_from_me, bool):
        raise AdapterError("WuzAPI conversation direction is invalid")
    chat = _source_value(source, "Chat")
    chat_normalized = _normalize_jid(chat)
    if chat_normalized.endswith("@broadcast"):
        raise UnsupportedEventError(
            "WuzAPI broadcast conversations are not implemented"
        )
    if chat_normalized.endswith("@g.us"):
        group_address = _address(chat, "group", "%s.Chat" % prefix)
        if not group_address:
            raise AdapterError("WuzAPI group event has no conversation address")
        return (group_address,), group_address.value_normalized, "group"

    chat_raw = _jid_string(chat)
    if not _STRICT_PARTICIPANT_JID_PATTERN.fullmatch(
        chat_raw
    ) or not _STRICT_NORMALIZED_PARTICIPANT_JID_PATTERN.fullmatch(chat_normalized):
        # Newsletter, status/broadcast variants and future opaque JID classes
        # must remain in the durable unsupported ledger until an explicit
        # conversation contract exists for them.
        raise UnsupportedEventError(
            "WuzAPI direct conversation JID class is not implemented"
        )

    alternate_name = "RecipientAlt" if is_from_me else "SenderAlt"
    addresses = _addresses(
        (
            (chat, "primary", "%s.Chat" % prefix),
            (
                _source_value(source, alternate_name),
                "alternate",
                "%s.%s" % (prefix, alternate_name),
            ),
        )
    )
    if not addresses:
        raise AdapterError("WuzAPI direct event has no conversation address")
    return addresses, addresses[0].value_normalized, "direct"


def _actor_values(source, prefix):
    return _addresses(
        (
            (_source_value(source, "Sender"), "sender", "%s.Sender" % prefix),
            (
                _source_value(source, "SenderAlt"),
                "alternate",
                "%s.SenderAlt" % prefix,
            ),
        )
    )


def _strict_direct_participant(value, role, source_field):
    raw = _jid_string(value)
    normalized = _normalize_jid(value)
    if not raw or not normalized:
        raise AdapterError("WuzAPI control event has no remote participant")
    if raw.endswith("@g.us") or normalized.endswith("@g.us"):
        raise UnsupportedEventError("WuzAPI group control events are not implemented")
    if not _STRICT_PARTICIPANT_JID_PATTERN.fullmatch(
        raw
    ) or not _STRICT_NORMALIZED_PARTICIPANT_JID_PATTERN.fullmatch(normalized):
        raise UnsupportedEventError(
            "WuzAPI control event participant JID class is not implemented"
        )
    address = _address(value, role, source_field)
    if not address or address.namespace not in ("whatsapp.lid", "whatsapp.pn"):
        raise UnsupportedEventError(
            "WuzAPI control event participant JID class is not implemented"
        )
    return address


def _same_participant(left, right):
    left_normalized = _normalize_session_identity(left)
    right_normalized = _normalize_session_identity(right)
    return bool(
        left_normalized and right_normalized and left_normalized == right_normalized
    )


def _call_ref(connection, call_id):
    if not isinstance(call_id, str):
        raise AdapterError("WuzAPI call event has no call ID")
    call_id = call_id.strip()
    if (
        not call_id
        or len(call_id) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in call_id)
    ):
        raise AdapterError("WuzAPI call event has an invalid call ID")
    # The provider identifier is correlation evidence, not an identifier that
    # consumers or UI should learn. A NUL separator makes the two inputs
    # unambiguous while keeping a deterministic reference across call states.
    return hashlib.sha256(
        ("%s\0%s" % (connection.external_ref, call_id)).encode("utf-8")
    ).hexdigest()


def _configured_own_identity(connection):
    return _normalize_session_identity(connection.account_id.own_external_identity)


def _call_direction(connection, remote, creators):
    own_identity = _configured_own_identity(connection)
    if own_identity and any(
        _same_participant(candidate, own_identity) for candidate in creators
    ):
        return "outbound"
    if any(_same_participant(candidate, remote) for candidate in creators):
        return "inbound"
    return "unknown"


def _call_remote_addresses(connection, raw_event):
    group = _jid_string(_lookup(raw_event, "GroupJID", "GroupJid"))
    if group:
        raise UnsupportedEventError("WuzAPI group calls are not implemented")

    remote = _lookup(raw_event, "From")
    primary = _strict_direct_participant(remote, "primary", "event.From")
    own_identity = _configured_own_identity(connection)
    if own_identity and _same_participant(remote, own_identity):
        raise UnsupportedEventError(
            "WuzAPI call event remote participant resolves to the account identity"
        )

    creator_specs = (
        (_lookup(raw_event, "CallCreator"), "event.CallCreator"),
        (_lookup(raw_event, "CallCreatorAlt"), "event.CallCreatorAlt"),
    )
    creators = tuple(
        value for value, _source_field in creator_specs if _jid_string(value)
    )
    logical_direction = _call_direction(connection, remote, creators)

    # CallCreator/CallCreatorAlt are safe alternate-identity evidence only when
    # one side anchors the pair to From. For outbound calls they describe the
    # account itself and must never become guest identifiers.
    pair_is_anchored = any(_same_participant(value, remote) for value in creators)
    alternate_spec = None
    if pair_is_anchored:
        for value, source_field in creator_specs:
            if not _jid_string(value) or _same_participant(value, remote):
                continue
            if own_identity and _same_participant(value, own_identity):
                continue
            candidate = _strict_direct_participant(value, "alternate", source_field)
            if candidate.namespace == primary.namespace:
                continue
            alternate_spec = (value, source_field)
            break

    conversation_specs = [(remote, "primary", "event.From")]
    actor_specs = [(remote, "sender", "event.From")]
    if alternate_spec:
        alternate, source_field = alternate_spec
        conversation_specs.append((alternate, "alternate", source_field))
        actor_specs.append((alternate, "alternate", source_field))
    return (
        _addresses(conversation_specs),
        _addresses(actor_specs),
        primary.value_normalized,
        logical_direction,
    )


def _group_actor_values(source, prefix, conversation_ref):
    specs = (
        (_source_value(source, "Sender"), "sender", "%s.Sender" % prefix),
        (
            _source_value(source, "SenderAlt"),
            "alternate",
            "%s.SenderAlt" % prefix,
        ),
    )
    result = []
    seen = set()
    for index, (value, role, source_field) in enumerate(specs):
        raw = _jid_string(value)
        if not raw:
            if index == 0:
                raise AdapterError("WuzAPI group message has no participant sender")
            continue
        normalized = _normalize_jid(value)
        if normalized == conversation_ref:
            raise AdapterError(
                "WuzAPI group participant cannot be the group conversation JID"
            )
        if not _STRICT_PARTICIPANT_JID_PATTERN.fullmatch(
            raw
        ) or not _STRICT_NORMALIZED_PARTICIPANT_JID_PATTERN.fullmatch(normalized):
            raise AdapterError("WuzAPI group participant JID is invalid")
        address = _address(value, role, source_field)
        if address and address.value_normalized == conversation_ref:
            raise AdapterError(
                "WuzAPI group participant cannot be the group conversation JID"
            )
        if not address or address.namespace not in ("whatsapp.lid", "whatsapp.pn"):
            raise AdapterError("WuzAPI group participant JID is invalid")
        key = (address.namespace, address.value_normalized)
        if key in seen:
            continue
        seen.add(key)
        result.append(address)
    return tuple(result)


def _optional_nonnegative_int(value):
    if isinstance(value, bool) or value in (None, ""):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _forwarding_values(context):
    """Return explicit forwarding evidence from the active message context only."""

    if not isinstance(context, dict):
        return False, None
    raw_score = _lookup(context, "forwardingScore")
    forwarding_score = (
        raw_score
        if isinstance(raw_score, int)
        and not isinstance(raw_score, bool)
        and 0 <= raw_score <= 127
        else None
    )
    # Real WuzAPI traffic contains positive scores without the explicit flag.
    # Keep that observation, but never invent the user-facing classification.
    return _lookup(context, "isForwarded") is True, forwarding_score


def _optional_attribution_delay(value):
    """Return a provider delay without inventing zero for missing evidence."""

    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= MAX_ATTRIBUTION_DELAY_SECONDS else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed <= MAX_ATTRIBUTION_DELAY_SECONDS else None
    return None


def _provider_string(value, maximum=2048):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or len(value) > maximum:
        return ""
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return ""
    return value


def _canonical_attribution_token(value):
    value = _provider_string(value, maximum=256).lower()
    if not value:
        return ""
    token = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    compact = token.replace("_", "")
    known = {
        "clicktochatlink": "click_to_chat_link",
        "ctwaad": "ctwa_ad",
        "facebookads": "facebook_ads",
        "fbads": "fb_ads",
        "globalsearchnewchat": "global_search_new_chat",
        "metaads": "meta_ads",
        "phonenumberhyperlink": "phone_number_hyperlink",
    }
    token = known.get(compact, token)
    if not token or not token[0].isalpha():
        return ""
    return token[:128]


def _safe_attribution_url(value):
    value = _provider_string(value, maximum=2048)
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ""
    return value


def _attribution_context_candidates(message):
    """Collect current-message contexts without descending into quoted content."""

    result = []
    seen = set()

    def _visit(value, depth):
        if depth > 8 or not isinstance(value, (dict, list, tuple)):
            return
        if isinstance(value, dict):
            for field_name in ("contextInfo", "messageContextInfo"):
                context = _lookup(value, field_name)
                if (
                    isinstance(context, dict)
                    and id(context) not in seen
                    and _has_attribution_signal(context)
                ):
                    seen.add(id(context))
                    result.append(context)
            for key, nested in value.items():
                if _normalized_key(key) in _ATTRIBUTION_CONTEXT_SKIP_KEYS:
                    continue
                _visit(nested, depth + 1)
            return
        for nested in value:
            _visit(nested, depth + 1)

    _visit(message, 0)
    return result


def _first_context_value(contexts, field_names):
    for context in contexts:
        value = _lookup(context, *field_names)
        if value not in (None, "", {}, []):
            return value
    return None


def _merged_context_mapping(contexts, container_name, field_contract):
    mappings = []
    for context in contexts:
        mapping = _lookup(context, container_name)
        if isinstance(mapping, dict):
            mappings.append(mapping)
    result = {}
    for canonical_name, field_names in field_contract:
        value = _first_context_value(mappings, field_names)
        if value is not None:
            result[canonical_name] = value
    return result


def _attribution_context(message, preferred_context=None):
    """Merge complementary provider contexts, preferring the active wrapper."""

    contexts = []
    if isinstance(preferred_context, dict):
        contexts.append(preferred_context)
    contexts.extend(
        context
        for context in _attribution_context_candidates(message)
        if all(context is not existing for existing in contexts)
    )
    if not contexts:
        return {}
    result = {}
    for canonical_name, field_names in _ATTRIBUTION_CONTEXT_FIELDS:
        value = _first_context_value(contexts, field_names)
        if value is not None:
            result[canonical_name] = value
    external = _merged_context_mapping(
        contexts, "externalAdReply", _ATTRIBUTION_EXTERNAL_FIELDS
    )
    if external:
        result["externalAdReply"] = external
    utm = _merged_context_mapping(contexts, "utm", _ATTRIBUTION_UTM_FIELDS)
    if utm:
        result["utm"] = utm
    for canonical_name, field_names in (
        ("utmSource", ("utmSource",)),
        ("utmMedium", ("utmMedium",)),
        ("utmCampaign", ("utmCampaign",)),
        ("utmContent", ("utmContent",)),
        ("utmTerm", ("utmTerm",)),
    ):
        value = _first_context_value(contexts, field_names)
        if value is not None:
            result[canonical_name] = value
    return result


def _has_attribution_signal(context):
    external = _lookup(context, "externalAdReply")
    if isinstance(external, dict) and any(
        value not in (None, "", {}, []) for value in external.values()
    ):
        return True
    return any(
        _lookup(context, field_name) not in (None, "", {}, [])
        for field_name in (
            "conversionSource",
            "conversionData",
            "ctwaPayload",
            "ctwaSignals",
            "entryPointConversionSource",
            "entryPointConversionApp",
            "entryPointConversionDelaySeconds",
            "utm",
            "utmSource",
            "utmMedium",
            "utmCampaign",
            "utmContent",
            "utmTerm",
        )
    )


def _opaque_attribution_fingerprint(value):
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, str):
        encoded = value.encode("utf-8")
    elif isinstance(value, bytes):
        encoded = value
    else:
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError):
            return ""
    return hashlib.sha256(encoded).hexdigest()


def _attribution_utm(context):
    raw_utm = _lookup(context, "utm")
    raw_utm = raw_utm if isinstance(raw_utm, dict) else {}
    result = {}
    for canonical, nested_names, context_names in (
        ("source", ("source", "utmSource"), ("utmSource",)),
        ("medium", ("medium", "utmMedium"), ("utmMedium",)),
        ("campaign", ("campaign", "utmCampaign"), ("utmCampaign",)),
        ("content", ("content", "utmContent"), ("utmContent",)),
        ("term", ("term", "utmTerm"), ("utmTerm",)),
    ):
        value = _lookup(raw_utm, *nested_names)
        if value is None:
            value = _lookup(context, *context_names)
        value = _provider_string(value, maximum=512)
        if value:
            result[canonical] = value
    return result


def _attribution_extensions(context, external_ad_reply):
    fingerprints = {}
    for canonical, field_names in (
        ("conversion_data", ("conversionData",)),
        ("ctwa_payload", ("ctwaPayload",)),
        ("ctwa_signals", ("ctwaSignals",)),
    ):
        value = _lookup(context, *field_names)
        if value is None:
            value = _lookup(external_ad_reply, *field_names)
        fingerprint = _opaque_attribution_fingerprint(value)
        if fingerprint:
            fingerprints[canonical] = fingerprint
    if not fingerprints:
        return {}
    return {
        "provider.wuzapi": {
            "opaque_fingerprints": fingerprints,
        }
    }


def _attribution_values(message, is_from_me, preferred_context=None):
    """Normalize WuzAPI/whatsmeow acquisition evidence into one touchpoint."""

    if is_from_me:
        return ()
    context = _attribution_context(message, preferred_context=preferred_context)
    if not context:
        return ()

    external = _lookup(context, "externalAdReply")
    external = external if isinstance(external, dict) else {}
    source_id = _provider_string(_lookup(external, "sourceID", "sourceId"))
    ctwa_clid = _provider_string(_lookup(external, "ctwaClid", "ctwaCLID"))
    source_type = _canonical_attribution_token(_lookup(external, "sourceType"))
    source_platform = _canonical_attribution_token(_lookup(external, "sourceApp"))
    entry_source = _canonical_attribution_token(
        _lookup(context, "entryPointConversionSource")
    )
    entry_app = _canonical_attribution_token(
        _lookup(context, "entryPointConversionApp")
    )
    conversion_source = _canonical_attribution_token(
        _lookup(context, "conversionSource")
    )
    utm = _attribution_utm(context)

    actionable_external_ad = source_type == "ad" and bool(source_id or ctwa_clid)
    if actionable_external_ad or entry_source == "ctwa_ad":
        touchpoint_type = "paid_ad_click"
        evidence_level = "provider_asserted"
    elif conversion_source in _META_PAID_CONVERSION_SOURCES:
        touchpoint_type = "paid_ad_signal"
        evidence_level = "provider_hint"
    elif entry_source:
        touchpoint_type = "entry_point"
        evidence_level = (
            "provider_asserted_non_paid"
            if entry_source in _KNOWN_NON_PAID_ENTRY_POINTS
            else "provider_hint"
        )
    elif utm:
        # UTM evidence alone does not prove paid media, but it is still useful
        # acquisition evidence for later CRM attribution.
        touchpoint_type = "unknown"
        evidence_level = "observed"
    else:
        # An empty or presentation-only externalAdReply is not acquisition
        # evidence. In particular, it must not turn an outbound echo into a
        # paid touchpoint.
        return ()

    identifiers = []
    if source_id:
        identifiers.append(
            ExternalIdentifierDTO(
                namespace="meta.source_id",
                role="ad_source",
                value=source_id,
                source_field="contextInfo.externalAdReply.sourceID",
            )
        )
    if ctwa_clid:
        identifiers.append(
            ExternalIdentifierDTO(
                namespace="meta.ctwa_clid",
                role="click",
                value=ctwa_clid,
                source_field="contextInfo.externalAdReply.ctwaClid",
            )
        )

    entry_point = {}
    for key, value in (
        ("source", entry_source),
        ("app", entry_app),
        ("conversion_source", conversion_source),
        (
            "external_source",
            _canonical_attribution_token(
                _lookup(context, "entryPointConversionExternalSource")
            ),
        ),
        (
            "external_medium",
            _canonical_attribution_token(
                _lookup(context, "entryPointConversionExternalMedium")
            ),
        ),
    ):
        if value:
            entry_point[key] = value
    for key, value in (
        (
            "delay_seconds",
            _optional_attribution_delay(
                _lookup(context, "entryPointConversionDelaySeconds")
            ),
        ),
        (
            "conversion_delay_seconds",
            _optional_attribution_delay(_lookup(context, "conversionDelaySeconds")),
        ),
    ):
        if value is not None:
            entry_point[key] = value

    creative = {}
    media_type = _canonical_attribution_token(_lookup(external, "mediaType"))
    if media_type:
        creative["media_type"] = media_type

    flags = {}
    for canonical, raw_value in (
        ("show_ad_attribution", _lookup(external, "showAdAttribution")),
        (
            "always_show_ad_attribution",
            _lookup(context, "alwaysShowAdAttribution"),
        ),
    ):
        value = _optional_boolean(raw_value)
        if value is not None:
            flags[canonical] = value

    return (
        AttributionDTO(
            touchpoint_type=touchpoint_type,
            evidence_level=evidence_level,
            network="meta",
            source_platform=source_platform or entry_app,
            source_type=source_type,
            source_url=_safe_attribution_url(
                _lookup(external, "sourceURL", "sourceUrl")
            ),
            external_identifiers=tuple(identifiers),
            utm=utm,
            entry_point=entry_point,
            creative=creative,
            flags=flags,
            provider_extensions=_attribution_extensions(context, external),
        ),
    )


def _sha256_hex(value):
    if not isinstance(value, str) or not value.strip():
        return ""
    value = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return value.lower()
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return ""
    return decoded.hex() if len(decoded) == hashlib.sha256().digest_size else ""


def _safe_filename(value, external_message_id, mime_type):
    value = value if isinstance(value, str) else ""
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    value = "".join(
        character
        for character in value
        if ord(character) >= 32 and ord(character) != 127
    ).strip()
    if value in ("", ".", ".."):
        extension = mimetypes.guess_extension((mime_type or "").split(";", 1)[0])
        value = "%s%s" % (external_message_id, extension or ".bin")
    return value[:255]


def _media_descriptor(
    kind, provider_media, external_message_id, *, provider_media_kind=""
):
    mime_type = str(_lookup(provider_media, "mimetype", "mimeType") or "").strip()
    size_bytes = _optional_nonnegative_int(
        _lookup(provider_media, "fileLength", "size")
    )
    provider_sha256 = str(_lookup(provider_media, "fileSHA256") or "").strip()
    file_name = _safe_filename(
        _lookup(provider_media, "fileName", "title"),
        external_message_id,
        mime_type,
    )
    remote_locator = {}
    for output_name, provider_names in (
        ("url", ("URL", "url")),
        ("direct_path", ("directPath",)),
        ("media_key", ("mediaKey",)),
        ("file_sha256", ("fileSHA256",)),
        ("file_enc_sha256", ("fileEncSHA256",)),
    ):
        raw_value = _lookup(provider_media, *provider_names)
        if isinstance(raw_value, str) and raw_value.strip():
            remote_locator[output_name] = raw_value.strip()
    if size_bytes:
        remote_locator["file_length"] = size_bytes
    if provider_media_kind:
        remote_locator["provider_media_kind"] = provider_media_kind
    return MediaDTO(
        kind=kind,
        external_media_id=external_message_id,
        remote_locator=remote_locator,
        mime_type=mime_type,
        file_name=file_name,
        size_bytes=size_bytes,
        sha256=_sha256_hex(provider_sha256),
        is_voice_note=(
            _boolean(_lookup(provider_media, "PTT", "ptt"))
            if kind == "audio"
            else False
        ),
        duration_seconds=(
            _optional_nonnegative_int(_lookup(provider_media, "seconds"))
            if kind in ("audio", "video")
            else 0
        ),
        width=(
            _optional_nonnegative_int(_lookup(provider_media, "width"))
            if kind in ("image", "video")
            else 0
        ),
        height=(
            _optional_nonnegative_int(_lookup(provider_media, "height"))
            if kind in ("image", "video")
            else 0
        ),
    )


def _human_fragment(value, maximum=512):
    """Return one bounded, single-line provider label without opaque payloads."""

    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    value = "".join(
        character
        for character in value
        if ord(character) >= 32 and ord(character) != 127
    )
    return value[:maximum].strip()


def _human_lines(values):
    lines = []
    for value in values:
        value = _human_fragment(value)
        if value and value not in lines:
            lines.append(value)
        if len(lines) >= _MAX_HUMAN_SUMMARY_ITEMS:
            break
    return "\n".join(lines)[:_MAX_TEXT_CHARS]


def _valid_coordinate(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not minimum <= value <= maximum:
        return None
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        return None
    return value


def _location_content(provider_location, *, live=False):
    label = "Localização em tempo real" if live else "Localização"
    details = []
    if live:
        details.append(_lookup(provider_location, "caption"))
    else:
        details.extend(
            (
                _lookup(provider_location, "name"),
                _lookup(provider_location, "address"),
                _lookup(provider_location, "comment"),
            )
        )
    details = [_human_fragment(value) for value in details]
    details = [value for value in details if value]
    latitude = _valid_coordinate(_lookup(provider_location, "degreesLatitude"), -90, 90)
    longitude = _valid_coordinate(
        _lookup(provider_location, "degreesLongitude"), -180, 180
    )
    lines = []
    if details:
        lines.append("%s: %s" % (label, details.pop(0)))
        lines.extend(details)
    else:
        lines.append("[%s]" % label)
    if latitude is not None and longitude is not None:
        lines.append("%.6f, %.6f" % (latitude, longitude))
    return _human_lines(lines)


def _contact_content(provider_contact):
    display_name = _human_fragment(_lookup(provider_contact, "displayName"))
    return "Contato: %s" % display_name if display_name else "[Contato]"


def _contacts_content(provider_contacts):
    title = _human_fragment(_lookup(provider_contacts, "displayName"))
    contacts = _lookup(provider_contacts, "contacts")
    contacts = contacts if isinstance(contacts, (list, tuple)) else ()
    names = [
        _human_fragment(_lookup(item, "displayName"))
        for item in contacts[:_MAX_HUMAN_SUMMARY_ITEMS]
        if isinstance(item, dict)
    ]
    names = [name for name in names if name]
    lines = ["Contatos: %s" % title if title else "[Contatos]"]
    lines.extend("• %s" % name for name in names)
    return _human_lines(lines)


def _template_content(provider_template):
    containers = [provider_template]
    format_container = _lookup(provider_template, "format")
    if isinstance(format_container, dict):
        containers.append(format_container)
    for field_name in (
        "hydratedTemplate",
        "hydratedFourRowTemplate",
        "fourRowTemplate",
        "interactiveMessageTemplate",
    ):
        for source in tuple(containers):
            nested = _lookup(source, field_name)
            if isinstance(nested, dict) and nested not in containers:
                containers.append(nested)
    for container in containers:
        for field_name in ("hydratedContentText", "contentText", "text", "title"):
            text = _human_fragment(_lookup(container, field_name), maximum=4096)
            if text:
                return text
        body = _lookup(container, "body")
        text = _human_fragment(_lookup(body, "text"), maximum=4096)
        if text:
            return text
    return "[Mensagem de modelo]"


def _poll_content(provider_poll):
    question = _human_fragment(_lookup(provider_poll, "name"), maximum=2048)
    options = _lookup(provider_poll, "options")
    options = options if isinstance(options, (list, tuple)) else ()
    option_names = []
    for option in options[:_MAX_HUMAN_SUMMARY_ITEMS]:
        if not isinstance(option, dict):
            continue
        name = _human_fragment(_lookup(option, "optionName", "name"))
        if name:
            option_names.append(name)
    lines = ["Enquete: %s" % question if question else "[Enquete]"]
    lines.extend("• %s" % option for option in option_names)
    return _human_lines(lines)


def _interactive_content(provider_interactive, kind):
    if kind in ("buttonsResponseMessage", "templateButtonReplyMessage"):
        # WuzAPI uses encoding/json on the pinned Go protobuf structs. The
        # buttons response is a oneof (Response.SelectedDisplayText), while a
        # template button reply exposes selectedDisplayText directly.
        response = (
            _lookup(provider_interactive, "response")
            if kind == "buttonsResponseMessage"
            else provider_interactive
        )
        text = _human_fragment(
            _lookup(response, "selectedDisplayText"),
            maximum=4096,
        )
        return text or "[Resposta interativa]"
    if kind == "listResponseMessage":
        text = _human_fragment(
            _lookup(provider_interactive, "title", "description"), maximum=4096
        )
        return text or "[Resposta interativa]"
    lines = []
    for container_name in ("header", "body", "footer"):
        container = _lookup(provider_interactive, container_name)
        if isinstance(container, dict):
            lines.append(_lookup(container, "title", "subtitle", "text"))
    for field_name in (
        "headerText",
        "contentText",
        "title",
        "description",
        "footerText",
    ):
        lines.append(_lookup(provider_interactive, field_name))
    return _human_lines(lines) or "[Conteúdo interativo]"


def _safe_association_message_id(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if (
        not value
        or len(value) > _TARGET_MESSAGE_ID_MAX_CHARS
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        return ""
    return value


def _associated_child_metadata(message):
    """Return bounded correlation metadata, never WhatsApp routing/secret data."""

    metadata = {"kind": "associated_child"}
    context = _lookup(message, "messageContextInfo")
    association = _lookup(context, "messageAssociation")
    if not isinstance(association, dict):
        return metadata
    raw_type = _lookup(association, "associationType")
    if isinstance(raw_type, int) and not isinstance(raw_type, bool):
        type_key = str(raw_type)
    elif isinstance(raw_type, str) and len(raw_type) <= 64:
        type_key = _normalized_key(raw_type)
    else:
        type_key = ""
    association_type = _ASSOCIATED_CHILD_MEDIA_ASSOCIATION_TYPES.get(type_key)
    if association_type:
        metadata["type"] = association_type
    parent_key = _lookup(association, "parentMessageKey")
    parent_message_id = _safe_association_message_id(_lookup(parent_key, "ID", "Id"))
    if parent_message_id:
        metadata["parent_external_message_id"] = parent_message_id
    raw_index = _lookup(association, "messageIndex")
    if isinstance(raw_index, int) and not isinstance(raw_index, bool):
        index = raw_index
    elif (
        isinstance(raw_index, str)
        and len(raw_index.strip()) <= 5
        and raw_index.strip().isdigit()
    ):
        index = int(raw_index.strip())
    else:
        index = -1
    if 0 <= index <= _MAX_ASSOCIATED_CHILD_INDEX:
        metadata["message_index"] = index
    return metadata


def _unwrap_human_message(message):
    current = message
    associated_child = {}
    for depth in range(9):
        nested_message = None
        selected_wrapper = ""
        for field_name in WUZAPI_HUMAN_MESSAGE_WRAPPER_FIELDS:
            wrapper = _lookup(current, field_name)
            nested = _lookup(wrapper, "message")
            if isinstance(nested, dict):
                nested_message = nested
                selected_wrapper = field_name
                break
        if nested_message is None:
            return current, associated_child
        if depth == 8:
            break
        if selected_wrapper == "associatedChildMessage":
            if associated_child:
                raise UnsupportedEventError(
                    "WuzAPI nested associated child messages are invalid"
                )
            associated_child = _associated_child_metadata(current)
        current = nested_message
    raise UnsupportedEventError("WuzAPI message wrapper nesting is invalid")


def _unsupported_human_content(message):
    fields = {
        _normalized_key(key)
        for key, value in message.items()
        if value not in (None, "", {}, [])
    }
    if not fields or fields <= _PROTOCOL_ONLY_MESSAGE_FIELDS:
        raise UnsupportedEventError("WuzAPI protocol-only message has no human content")
    if fields & _HUMAN_FALLBACK_MESSAGE_FIELDS:
        return _UNSUPPORTED_HUMAN_CONTENT_TEXT, {}, (), "unsupported"
    raise UnsupportedEventError("WuzAPI message content is not implemented")


def _sticker_media_kind(sticker):
    mime_type = str(_lookup(sticker, "mimetype", "mimeType") or "").strip()
    base_mime = mime_type.split(";", 1)[0].strip().lower()
    raw_is_animated = _lookup(sticker, "isAnimated")
    is_animated = _optional_boolean(raw_is_animated)
    if raw_is_animated is not None and is_animated is None:
        raise AdapterError("WuzAPI sticker animation metadata is invalid")
    if base_mime == "image/webp":
        return "image"
    raise AdapterError("WuzAPI inbound stickers must use image/webp")


def _structured_message_content(message, external_message_id):
    circular_video = _lookup(message, "ptvMessage")
    if isinstance(circular_video, dict):
        return (
            "",
            _lookup(circular_video, "contextInfo") or {},
            (_media_descriptor("video", circular_video, external_message_id),),
            "video",
        )
    sticker = _lookup(message, "stickerMessage")
    if isinstance(sticker, dict):
        media_kind = _sticker_media_kind(sticker)
        return (
            "[Figurinha]",
            _lookup(sticker, "contextInfo") or {},
            (
                _media_descriptor(
                    media_kind,
                    sticker,
                    external_message_id,
                    provider_media_kind="sticker",
                ),
            ),
            "sticker",
        )
    location = _lookup(message, "locationMessage")
    if isinstance(location, dict):
        return (
            _location_content(location),
            _lookup(location, "contextInfo") or {},
            (),
            "location",
        )
    live_location = _lookup(message, "liveLocationMessage")
    if isinstance(live_location, dict):
        return (
            _location_content(live_location, live=True),
            _lookup(live_location, "contextInfo") or {},
            (),
            "live_location",
        )
    contact = _lookup(message, "contactMessage")
    if isinstance(contact, dict):
        return (
            _contact_content(contact),
            _lookup(contact, "contextInfo") or {},
            (),
            "contact",
        )
    contacts = _lookup(message, "contactsArrayMessage")
    if isinstance(contacts, dict):
        return (
            _contacts_content(contacts),
            _lookup(contacts, "contextInfo") or {},
            (),
            "contacts",
        )
    album = _lookup(message, "albumMessage")
    if isinstance(album, dict):
        raise UnsupportedEventError(
            "WuzAPI album coordination envelope has no standalone human content"
        )
    template = _lookup(message, "templateMessage")
    if isinstance(template, dict):
        return (
            _template_content(template),
            _lookup(template, "contextInfo") or {},
            (),
            "template",
        )
    for poll_field in (
        "pollCreationMessage",
        "pollCreationMessageV2",
        "pollCreationMessageV3",
        "pollCreationMessageV5",
        "pollCreationMessageV6",
    ):
        poll = _lookup(message, poll_field)
        if isinstance(poll, dict):
            return (
                _poll_content(poll),
                _lookup(poll, "contextInfo") or {},
                (),
                "poll",
            )
    poll_update = _lookup(message, "pollUpdateMessage")
    if isinstance(poll_update, dict):
        raise UnsupportedEventError(
            "WuzAPI poll vote requires a structured poll projection"
        )
    for interactive_field in (
        "buttonsMessage",
        "buttonsResponseMessage",
        "interactiveMessage",
        "interactiveResponseMessage",
        "listMessage",
        "listResponseMessage",
        "templateButtonReplyMessage",
    ):
        interactive = _lookup(message, interactive_field)
        if isinstance(interactive, dict):
            context = _lookup(interactive, "contextInfo") or {}
            return (
                _interactive_content(interactive, interactive_field),
                context,
                (),
                (
                    "interactive_response"
                    if interactive_field.endswith("ResponseMessage")
                    or interactive_field == "templateButtonReplyMessage"
                    else "interactive"
                ),
            )
    return _unsupported_human_content(message)


def _message_content(message, external_message_id):
    message, associated_child = _unwrap_human_message(message)
    conversation = _lookup(message, "conversation")
    if isinstance(conversation, str):
        return conversation, {}, (), "text", associated_child
    extended = _lookup(message, "extendedTextMessage")
    text = _lookup(extended, "text")
    if isinstance(text, str):
        return (
            text,
            _lookup(extended, "contextInfo") or {},
            (),
            "text",
            associated_child,
        )
    for kind, (
        provider_field,
        _payload_field,
        _send_path,
        _download_path,
    ) in _MEDIA_FIELDS.items():
        provider_media = _lookup(message, provider_field)
        if not isinstance(provider_media, dict):
            continue
        caption = _lookup(provider_media, "caption")
        return (
            caption if isinstance(caption, str) else "",
            _lookup(provider_media, "contextInfo") or {},
            (_media_descriptor(kind, provider_media, external_message_id),),
            kind,
            associated_child,
        )
    return (
        *_structured_message_content(message, external_message_id),
        associated_child,
    )


def _protocol_type_is_revoke(value):
    if value == 0:
        return True
    if isinstance(value, str):
        return _normalized_key(value) in ("0", "revoke")
    return False


def _protocol_type_is_edit(value):
    if value == 14:
        return True
    if isinstance(value, str):
        return _normalized_key(value) in ("14", "messageedit")
    return False


def _protocol_edit_values(protocol):
    key = _lookup(protocol, "key")
    if not isinstance(key, dict):
        key = {}
    target = _provider_message_id(
        _lookup(key, "ID", "Id"),
        "WuzAPI edit target message ID",
        error_class=UnsupportedEventError,
    )
    edited_message = _lookup(protocol, "editedMessage")
    wrapper_message = _lookup(edited_message, "message")
    if isinstance(wrapper_message, dict):
        edited_message = wrapper_message
    if not isinstance(edited_message, dict):
        raise UnsupportedEventError("WuzAPI edit has no replacement content")
    new_text, context, media, content_type, _associated_child = _message_content(
        edited_message, target
    )
    if media or content_type != "text":
        raise UnsupportedEventError("WuzAPI media edits are not implemented")
    if not isinstance(new_text, str) or not new_text:
        raise UnsupportedEventError("WuzAPI edit has no replacement text")
    mutation = {
        "type": "edit",
        "target_external_message_id": target,
        "operation": "replace",
        "new_text": new_text,
        "target_participant": _jid_string(_lookup(key, "participant")),
    }
    target_from_me = _lookup(key, "fromMe")
    if isinstance(target_from_me, bool):
        mutation["target_from_me"] = target_from_me
    return mutation, context


def _mutation_values(raw_event, message, info):
    reaction = _lookup(message, "reactionMessage")
    if isinstance(reaction, dict):
        key = _lookup(reaction, "key") or {}
        target = _provider_message_id(
            _lookup(key, "ID", "Id"), "WuzAPI reaction target message ID"
        )
        emoji = _lookup(reaction, "text")
        emoji = emoji if isinstance(emoji, str) else ""
        mutation = {
            "type": "react",
            "target_external_message_id": target,
            "emoji": emoji,
            "operation": "add" if emoji else "remove",
            "target_participant": _jid_string(_lookup(key, "participant")),
        }
        target_from_me = _lookup(key, "fromMe")
        if isinstance(target_from_me, bool):
            mutation["target_from_me"] = target_from_me
        return "message.reaction", mutation, None

    protocol = _lookup(message, "protocolMessage")
    if isinstance(protocol, dict) and _protocol_type_is_edit(_lookup(protocol, "type")):
        mutation, context = _protocol_edit_values(protocol)
        if not mutation.get("target_participant"):
            mutation["target_participant"] = _jid_string(_source_value(info, "Sender"))
        return "message.updated", mutation, context
    if isinstance(protocol, dict) and _protocol_type_is_revoke(
        _lookup(protocol, "type")
    ):
        key = _lookup(protocol, "key") or {}
        target = _provider_message_id(
            _lookup(key, "ID", "Id"), "WuzAPI delete target message ID"
        )
        mutation = {
            "type": "delete",
            "target_external_message_id": target,
            "operation": "delete",
            "target_participant": _jid_string(_lookup(key, "participant")),
        }
        target_from_me = _lookup(key, "fromMe")
        if isinstance(target_from_me, bool):
            mutation["target_from_me"] = target_from_me
        return "message.deleted", mutation, None

    if _boolean(_lookup(raw_event, "IsEdit")) or str(_lookup(info, "Edit") or "") in (
        "1",
        "3",
    ):
        target = _provider_message_id(
            _lookup(info, "ID", "Id"), "WuzAPI edit target message ID"
        )
        new_text, context, media, content_type, _associated_child = _message_content(
            message, target
        )
        if media or content_type != "text":
            raise UnsupportedEventError("WuzAPI media edits are not implemented")
        mutation = {
            "type": "edit",
            "target_external_message_id": target,
            "operation": "replace",
            "new_text": new_text,
            "target_participant": _jid_string(_source_value(info, "Sender")),
        }
        target_from_me = _source_value(info, "IsFromMe")
        if isinstance(target_from_me, bool):
            mutation["target_from_me"] = target_from_me
        return "message.updated", mutation, context
    return "", {}, None


def _group_mutation_participant(value, source_field, conversation_ref):
    raw = _jid_string(value)
    normalized = _normalize_jid(value)
    if (
        not raw
        or normalized == conversation_ref
        or not _STRICT_PARTICIPANT_JID_PATTERN.fullmatch(raw)
        or not _STRICT_NORMALIZED_PARTICIPANT_JID_PATTERN.fullmatch(normalized)
    ):
        raise AdapterError("WuzAPI group mutation target participant is invalid")
    address = _address(value, "sender", source_field)
    if not address or address.namespace not in ("whatsapp.lid", "whatsapp.pn"):
        raise AdapterError("WuzAPI group mutation target participant is invalid")
    return address


def _normalize_group_mutation(mutation, conversation_ref):
    # Baileys/Whatsmeow can expose the nested key.fromMe from the mutation
    # actor's perspective.  In a group, blindly inverting it for a remote actor
    # would mislabel a target authored by a third participant.  Preserve the
    # bounded provider observation; after exact message correlation the core
    # always takes the persisted target binding as the canonical account lane.
    participant_value = mutation.pop("target_participant", "")
    if not participant_value:
        # Some valid group reaction/revoke payloads omit key.participant.  The
        # persisted target binding is the canonical participant proof; when an
        # observation is present below, the core additionally verifies it.
        return mutation
    source_field = (
        "event.Info.Sender"
        if mutation.get("type") == "edit"
        else "event.Message.key.participant"
    )
    participant = _group_mutation_participant(
        participant_value,
        source_field,
        conversation_ref,
    )
    mutation["target_protocol_participant"] = participant.to_dict()
    return mutation


def _mutation_event_id(connection, event_type, mutation, info, occurred_at):
    evidence = {
        "connection_ref": connection.external_ref,
        "event_type": event_type,
        "provider_event_id": str(_lookup(info, "ID", "Id") or ""),
        "occurred_at": occurred_at.isoformat(),
        "chat": _normalize_jid(_source_value(info, "Chat")),
        "sender": _normalize_jid(_source_value(info, "Sender")),
        "mutation": mutation,
    }
    digest = hashlib.sha256(
        json.dumps(
            evidence, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    return "%s:%s" % (event_type, digest)


def _reply_values(context):
    stanza_id = _lookup(context, "StanzaID", "StanzaId")
    participant = _jid_string(_lookup(context, "Participant"))
    if stanza_id is None or (isinstance(stanza_id, str) and not stanza_id.strip()):
        return "", {}
    stanza_id = _provider_message_id(
        stanza_id,
        "WuzAPI reply target message ID",
    )
    reply_to = {"external_message_id": stanza_id}
    if participant:
        reply_to["participant"] = participant
        reply_to["participant_normalized"] = _normalize_jid(participant)
    return stanza_id, reply_to


def _receipt_event_id(connection, state, message_ids, occurred_at, source, is_from_me):
    participants = sorted(
        {
            value
            for value in (
                _normalize_jid(_source_value(source, "Sender")),
                _normalize_jid(_source_value(source, "SenderAlt")),
            )
            if value
        }
    )
    evidence = {
        "connection_ref": connection.external_ref,
        "state": state,
        "message_ids": sorted(message_ids),
        "occurred_at": occurred_at.isoformat(),
        "chat": _normalize_jid(_source_value(source, "Chat")),
        "participants": participants,
        "is_from_me": is_from_me,
    }
    serialized = json.dumps(
        evidence, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return "ReadReceipt:%s" % hashlib.sha256(serialized).hexdigest()


def _header(headers, name):
    if not headers:
        return ""
    getter = getattr(headers, "get", None)
    if getter:
        value = getter(name)
        if value is not None:
            return str(value).strip()
    normalized_name = name.lower()
    try:
        items = headers.items()
    except AttributeError:
        return ""
    for key, value in items:
        if str(key).lower() == normalized_name:
            return str(value).strip()
    return ""


def _retry_after(response):
    raw_value = _header(getattr(response, "headers", {}), "Retry-After")
    if not raw_value:
        return 0
    try:
        return min(MAX_PROVIDER_RETRY_AFTER_SECONDS, max(0, int(raw_value)))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw_value)
        except (TypeError, ValueError, OverflowError):
            return 0
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)
        delay = (
            retry_at.astimezone(datetime.timezone.utc)
            - datetime.datetime.now(datetime.timezone.utc)
        ).total_seconds()
        return min(
            MAX_PROVIDER_RETRY_AFTER_SECONDS,
            max(0, int(math.ceil(delay))),
        )


def _response_json(response):
    try:
        payload = response.json()
    except (TypeError, ValueError, RecursionError, requests.RequestException):
        return None
    return payload if isinstance(payload, dict) else None


def _is_pre_dispatch_connection_failure(error):
    """Return whether Requests proves that no provider connection was made.

    A generic ``ConnectionError`` can also represent a reset while a request is
    already in flight and must remain ambiguous.  Requests explicitly documents
    ``ConnectTimeout`` as safe to retry, while urllib3's ``NewConnectionError``
    identifies failure to establish the connection itself.
    """

    if isinstance(error, requests.ConnectTimeout):
        return True
    pending = [error]
    visited = set()
    while pending:
        candidate = pending.pop()
        if id(candidate) in visited:
            continue
        visited.add(id(candidate))
        if isinstance(candidate, NewConnectionError):
            return True
        for nested in (
            getattr(candidate, "reason", None),
            getattr(candidate, "__cause__", None),
            getattr(candidate, "__context__", None),
            *getattr(candidate, "args", ()),
        ):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


def _check_configuration_content_length(response, maximum_bytes, error_prefix):
    content_length = _header(getattr(response, "headers", {}), "Content-Length")
    if not content_length:
        return
    try:
        declared_length = int(content_length)
    except ValueError:
        # A malformed optional header is not authoritative; the bounded reader
        # remains the enforcement boundary.
        return
    if declared_length > maximum_bytes:
        raise AdapterError("%s response is too large" % error_prefix)


def _configuration_stream_json(response, maximum_bytes, error_prefix):
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=16 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > maximum_bytes:
            raise AdapterError("%s response is too large" % error_prefix)
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (ValueError, RecursionError):
        raise AdapterError("%s response is not valid JSON" % error_prefix) from None


def _configuration_direct_json(response, maximum_bytes, error_prefix):
    try:
        payload = response.json()
    except (TypeError, ValueError, RecursionError):
        raise AdapterError("%s response is not valid JSON" % error_prefix) from None
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > maximum_bytes:
        raise AdapterError("%s response is too large" % error_prefix)
    return payload


def _limited_json_object(response, maximum_bytes, error_prefix):
    """Read a bounded provider response and always close its HTTP stream."""

    try:
        _check_configuration_content_length(response, maximum_bytes, error_prefix)
        iterator = getattr(response, "iter_content", None)
        payload = (
            _configuration_stream_json(response, maximum_bytes, error_prefix)
            if callable(iterator)
            else _configuration_direct_json(response, maximum_bytes, error_prefix)
        )
        if not isinstance(payload, dict):
            raise AdapterError("%s response is not a JSON object" % error_prefix)
        return payload
    except requests.RequestException:
        raise TransientAdapterError(
            "%s response stream failed" % error_prefix
        ) from None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _safe_send_response(response, payload):
    data = _lookup(payload, "data") or {}
    safe_data = {}
    for output_name, provider_name in (
        ("details", "Details"),
        ("timestamp", "Timestamp"),
        ("id", "Id"),
    ):
        value = _lookup(data, provider_name)
        if isinstance(value, (str, int, float, bool)):
            safe_data[output_name] = value
    return {
        "http_status": response.status_code,
        "code": _lookup(payload, "code"),
        "success": _lookup(payload, "success") is True,
        "data": safe_data,
    }


def _error_result(status, retry_after_seconds=0):
    if status in (401, 403):
        return AdapterResult(
            status="paused",
            error_code="http_%s" % status,
            error_message="WuzAPI rejected the configured API token",
            retry_after_seconds=retry_after_seconds,
        )
    if status == 429:
        return AdapterResult(
            status="transient",
            error_code="http_429",
            error_message="WuzAPI rate limit was reached",
            retry_after_seconds=retry_after_seconds or 60,
        )
    if status in (408, 425) or status >= 500:
        return AdapterResult(
            status="uncertain",
            error_code="http_%s" % status,
            error_message=(
                "WuzAPI failed after dispatch may have reached the provider"
            ),
        )
    return AdapterResult(
        status="permanent",
        error_code="http_%s" % status,
        error_message="WuzAPI rejected the outbound command",
    )


def _reply_target_message_id(command):
    external_message_id = ""
    if isinstance(command.reply_to, dict):
        external_message_id = command.reply_to.get("external_message_id") or ""
    if not external_message_id and command.message:
        external_message_id = command.message.reply_to_external_id
    return external_message_id


def _validated_reply_target(command):
    external_message_id = _reply_target_message_id(command)
    if not isinstance(external_message_id, str) or not external_message_id.strip():
        if command.conversation.conversation_type == "group" and (
            command.reply_to
            or (command.message and command.message.reply_to_external_id)
        ):
            raise AdapterError("WuzAPI group reply requires a target message ID")
        return {}
    return _provider_message_id(
        external_message_id,
        "WuzAPI reply target message ID",
    )


def _group_reply_context(command, external_message_id):
    participant_values = (
        command.reply_to.get("protocol_participant")
        if isinstance(command.reply_to, dict)
        else None
    )
    try:
        participant_address = AddressDTO.from_dict(participant_values)
    except DTOValidationError as error:
        raise AdapterError(
            "WuzAPI group reply requires a protocol participant"
        ) from error
    participant = _normalize_jid(participant_address.value_normalized)
    expected_namespace = _jid_namespace(participant)
    if (
        participant_address.role != "sender"
        or participant_address.confidence != "protocol"
        or participant_address.namespace not in ("whatsapp.lid", "whatsapp.pn")
        or participant_address.namespace != expected_namespace
        or participant != participant_address.value_normalized
        or len(participant) > _TARGET_MESSAGE_ID_MAX_CHARS
        or not _STRICT_NORMALIZED_PARTICIPANT_JID_PATTERN.fullmatch(participant)
    ):
        raise AdapterError("WuzAPI group reply participant is invalid")
    return {
        "StanzaID": external_message_id,
        "Participant": participant,
    }


def _direct_reply_participant(command):
    snapshots = []
    if isinstance(command.reply_to, dict):
        snapshots.append(command.reply_to)
        protocol_snapshot = command.reply_to.get("protocol_snapshot")
        if isinstance(protocol_snapshot, dict):
            snapshots.append(protocol_snapshot)
    if command.message and isinstance(command.message.protocol_snapshot, dict):
        snapshots.append(command.message.protocol_snapshot)

    participant = ""
    for snapshot in snapshots:
        participant = (
            snapshot.get("participant_normalized")
            or snapshot.get("participant")
            or snapshot.get("reply_participant")
            or ""
        )
        source = snapshot.get("source")
        if not participant and isinstance(source, dict):
            participant = source.get("sender_normalized") or source.get("sender") or ""
        if participant:
            break
    return _normalize_jid(participant)


def _reply_context(command):
    external_message_id = _validated_reply_target(command)
    if not external_message_id:
        return {}
    if command.conversation.conversation_type == "group":
        return _group_reply_context(command, external_message_id)
    participant = _direct_reply_participant(command)
    if not _JID_PATTERN.fullmatch(participant):
        return {}
    return {
        "StanzaID": external_message_id,
        "Participant": participant,
    }


@adapter_registry.register("wuzapi", module="contact_center_wuzapi")
class WuzapiAdapter(WuzapiGroupMetadataMixin, ProviderAdapter):
    """WuzAPI v1.0.8 translation and transport boundary."""

    key = "wuzapi"
    display_name = "WuzAPI"
    provider_version = WUZAPI_VERSION
    provider_commit = WUZAPI_COMMIT

    # Generic WuzAPI primitives consumed by the cohesive group boundary mixin.
    _provider_lookup = staticmethod(_lookup)
    _provider_header = staticmethod(_header)
    _provider_response_json = staticmethod(_response_json)
    _provider_retry_after = staticmethod(_retry_after)
    _provider_event_metadata = staticmethod(_event_metadata)
    _provider_jid_string = staticmethod(_jid_string)
    _provider_normalize_jid = staticmethod(_normalize_jid)
    _provider_normalize_session_identity = staticmethod(_normalize_session_identity)
    _provider_jid_namespace = staticmethod(_jid_namespace)
    _provider_parse_timestamp = staticmethod(_parse_timestamp)
    _provider_boolean = staticmethod(_boolean)

    def outbound_min_interval_seconds(self, connection):
        """Conservatively pace one dispatch start per connected session/second."""

        return 1

    def supports_identity_profile(self, connection):
        return True

    def authenticate_webhook(self, connection, headers, body):
        return self.authenticate_webhook_secrets(
            (connection.wuzapi_hmac_secret,), headers, body
        )

    def authenticate_webhook_secrets(self, secrets, headers, body):
        """Authenticate against a short rotation keyring without leaking matches."""

        signature = _header(headers, "x-hmac-signature")
        if not isinstance(body, (bytes, bytearray)) or not _HMAC_PATTERN.fullmatch(
            signature
        ):
            return False
        authenticated = False
        for secret in tuple(secrets or ())[:3]:
            if not isinstance(secret, str) or not secret:
                continue
            expected = hmac.new(
                secret.encode("utf-8"), bytes(body), hashlib.sha256
            ).hexdigest()
            # Do not short-circuit: the observable work is independent of which
            # key matched while a rotation briefly accepts current and pending.
            authenticated |= hmac.compare_digest(expected, signature.lower())
        return authenticated

    def conversation_route(self, connection, envelope):
        """Classify only routing headers, including unsupported message bodies."""
        if not isinstance(envelope, dict):
            return None
        event_type = _lookup(envelope, "type")
        raw = _lookup(envelope, "event")
        if not isinstance(raw, dict) or event_type in WUZAPI_LIFECYCLE_EVENT_STATES:
            return None
        # Control event normalizers read routing headers without persistence.
        # Account lifecycle events above deliberately remain admitted.
        if event_type in _CALL_EVENT_STATES or event_type in (
            "IdentityChange",
            "GroupInfo",
            "JoinedGroup",
            "Picture",
        ):
            try:
                event = self.normalize_event(connection, envelope)
            except AdapterError:
                return None
            return {
                "conversation_type": event.conversation.conversation_type,
                "conversation_ref": event.conversation_ref,
                "addresses": [
                    (address.namespace, address.value_normalized)
                    for address in event.conversation.addresses
                ],
            }
        if event_type not in ("Message", "ReadReceipt"):
            return None
        source = _lookup(raw, "Info") if event_type == "Message" else raw
        if not isinstance(source, dict):
            return None
        try:
            addresses, reference, kind = _conversation_values(
                source, "event.Info", _source_value(source, "IsFromMe") is True
            )
        except AdapterError:
            return None
        return {
            "conversation_type": kind,
            "conversation_ref": reference,
            "addresses": [
                (address.namespace, address.value_normalized) for address in addresses
            ],
        }

    def normalize_event(self, connection, envelope):
        if not isinstance(envelope, dict):
            raise AdapterError("WuzAPI webhook envelope must be a JSON object")
        event_type = _lookup(envelope, "type")
        raw_event = _lookup(envelope, "event")
        if event_type in WUZAPI_LIFECYCLE_EVENT_STATES:
            return self._normalize_connection_event(
                connection, envelope, event_type, raw_event
            )
        if not isinstance(raw_event, dict):
            raise AdapterError("WuzAPI webhook event must be a JSON object")
        if event_type == "Message":
            return self._normalize_message(connection, envelope, raw_event)
        if event_type == "ReadReceipt":
            return self._normalize_receipt(connection, envelope, raw_event)
        if event_type in _CALL_EVENT_STATES:
            return self._normalize_call_event(
                connection, envelope, event_type, raw_event
            )
        if event_type == "IdentityChange":
            return self._normalize_identity_security_change(
                connection, envelope, raw_event
            )
        if event_type in ("GroupInfo", "JoinedGroup", "Picture"):
            return self._normalize_group_metadata_hint(
                connection, envelope, event_type, raw_event
            )
        raise UnsupportedEventError(
            "WuzAPI event type is not implemented: %s" % (event_type or "missing")
        )

    def _normalize_connection_event(
        self, connection, envelope, provider_event_type, raw_event
    ):
        if not isinstance(raw_event, (dict, str)):
            raise AdapterError("WuzAPI lifecycle event must be an object or string")
        evidence = {
            "connection_ref": connection.external_ref,
            "provider_event_type": provider_event_type,
            "event": raw_event,
            "instance_name": str(_lookup(envelope, "instanceName") or ""),
            "user_id": str(_lookup(envelope, "userID") or ""),
        }
        digest = hashlib.sha256(
            json.dumps(
                evidence, ensure_ascii=True, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        ).hexdigest()
        provider_metadata = dict(
            _event_metadata(envelope),
            event_type=provider_event_type,
            occurred_at_source="odoo_received",
        )
        extensions = {
            "state": WUZAPI_LIFECYCLE_EVENT_STATES[provider_event_type],
            "provider.wuzapi": provider_metadata,
        }
        if extensions["state"] == "connected":
            # WuzAPI lifecycle webhooks have no monotonic sequence and do not
            # prove which WhatsApp identity connected. Treat them as an
            # immediate health hint; only /session/status may reopen outbound.
            extensions["health_confirmation_required"] = True
        return EventDTO(
            provider_schema_version=WUZAPI_VERSION,
            event_id="Lifecycle:%s:%s" % (provider_event_type, digest),
            event_type="connection.updated",
            occurred_at=datetime.datetime.now(datetime.timezone.utc),
            account_ref=connection.account_id.external_ref,
            connection_ref=connection.external_ref,
            conversation_ref=connection.external_ref,
            platform=connection.account_id.platform,
            direction="inbound",
            is_from_me=False,
            origin="provider",
            actor=ActorDTO(),
            conversation=ConversationDTO(
                addresses=(),
                conversation_type="other",
            ),
            extensions=extensions,
        )

    def _normalize_call_event(
        self, connection, envelope, provider_event_type, raw_event
    ):
        occurred_at = _parse_timestamp(_lookup(raw_event, "Timestamp"))
        (
            conversation_addresses,
            actor_addresses,
            conversation_ref,
            logical_direction,
        ) = _call_remote_addresses(connection, raw_event)
        call_ref = _call_ref(connection, _lookup(raw_event, "CallID", "CallId"))
        call_state = _CALL_EVENT_STATES[provider_event_type]
        return EventDTO(
            provider_schema_version=WUZAPI_VERSION,
            event_id="Call:%s:%s" % (call_ref, call_state),
            event_type="conversation.call.updated",
            occurred_at=occurred_at,
            account_ref=connection.account_id.external_ref,
            connection_ref=connection.external_ref,
            conversation_ref=conversation_ref,
            platform=connection.account_id.platform,
            # This is the direction of the provider callback crossing the
            # adapter boundary. Call direction is independent and lives in the
            # provider-neutral call extension below.
            direction="inbound",
            is_from_me=False,
            origin="provider",
            actor=ActorDTO(addresses=actor_addresses, display_name=""),
            conversation=ConversationDTO(
                addresses=conversation_addresses,
                conversation_type="direct",
            ),
            extensions={
                "call": {
                    "ref": call_ref,
                    "state": call_state,
                    "direction": logical_direction,
                },
                "provider.wuzapi": _event_metadata(envelope),
            },
        )

    def _normalize_identity_security_change(self, connection, envelope, raw_event):
        remote = _lookup(raw_event, "JID", "Jid")
        primary = _strict_direct_participant(remote, "primary", "event.JID")
        own_identity = _configured_own_identity(connection)
        if own_identity and _same_participant(remote, own_identity):
            raise UnsupportedEventError(
                "WuzAPI identity event participant resolves to the account identity"
            )
        implicit = _optional_boolean(_lookup(raw_event, "Implicit"))
        if implicit is None:
            raise AdapterError("WuzAPI IdentityChange event has invalid Implicit")
        occurred_at = _parse_timestamp(_lookup(raw_event, "Timestamp"))
        evidence = {
            "connection_ref": connection.external_ref,
            "conversation_ref": primary.value_normalized,
            "occurred_at": occurred_at.isoformat(),
            "implicit": implicit,
        }
        digest = hashlib.sha256(
            json.dumps(
                evidence, ensure_ascii=True, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        ).hexdigest()
        return EventDTO(
            provider_schema_version=WUZAPI_VERSION,
            event_id="IdentitySecurity:%s" % digest,
            event_type="identity.security.changed",
            occurred_at=occurred_at,
            account_ref=connection.account_id.external_ref,
            connection_ref=connection.external_ref,
            conversation_ref=primary.value_normalized,
            platform=connection.account_id.platform,
            direction="inbound",
            is_from_me=False,
            origin="provider",
            actor=ActorDTO(
                addresses=_addresses(((remote, "sender", "event.JID"),)),
                display_name="",
            ),
            conversation=ConversationDTO(
                addresses=(primary,),
                conversation_type="direct",
            ),
            extensions={
                "identity_security": {
                    "change_kind": "primary_device",
                    "implicit": implicit,
                },
                "provider.wuzapi": _event_metadata(envelope),
            },
        )

    def _normalize_message(self, connection, envelope, raw_event):
        info = _lookup(raw_event, "Info")
        message = _lookup(raw_event, "Message")
        if not isinstance(info, dict) or not isinstance(message, dict):
            raise AdapterError("WuzAPI Message event is missing Info or Message")
        external_message_id = _provider_message_id(
            _lookup(info, "ID", "Id"), "WuzAPI Message event message ID"
        )
        occurred_at = _parse_timestamp(_lookup(info, "Timestamp"))
        is_from_me = _required_is_from_me(info, "WuzAPI Message event IsFromMe")
        (
            conversation_addresses,
            conversation_ref,
            conversation_type,
        ) = _conversation_values(info, "event.Info", is_from_me)
        mutation_event_type, mutation, mutation_context = _mutation_values(
            raw_event, message, info
        )
        actor_addresses = (
            (
                _group_actor_values(info, "event.Info", conversation_ref)
                if mutation_event_type or not is_from_me
                else _actor_values(info, "event.Info")
            )
            if conversation_type == "group"
            else _actor_values(info, "event.Info")
        )
        if conversation_type == "group" and mutation_event_type:
            mutation = _normalize_group_mutation(mutation, conversation_ref)
        attribution = ()
        structured_content = {}
        if mutation_event_type == "message.updated":
            text = mutation.get("new_text") or ""
            context = mutation_context or {}
            media = ()
            content_type = "text"
            associated_child = {}
        elif mutation_event_type:
            text = ""
            context = {}
            media = ()
            content_type = "text"
            associated_child = {}
        else:
            try:
                (
                    text,
                    context,
                    media,
                    content_type,
                    associated_child,
                ) = _message_content(message, external_message_id)
            except UnsupportedEventError:
                attribution = _attribution_values(message, is_from_me)
                if not attribution:
                    raise
                # Keep the normalized acquisition evidence even when the core
                # cannot project the content wrapper (for example a sticker).
                # The application will mark the message projection unsupported
                # after the independent attribution ledger has captured it.
                text = ""
                context = _attribution_context(message)
                media = ()
                content_type = "unsupported"
                associated_child = {}
            else:
                attribution = _attribution_values(
                    message,
                    is_from_me,
                    preferred_context=context,
                )
                if not media:
                    human_message, _association = _unwrap_human_message(message)
                    structured_content, structured_body = normalize_structured_content(
                        human_message
                    )
                    if structured_content:
                        content_type = structured_content["type"]
                        if structured_body is not None:
                            text = structured_body
                        elif content_type == "selection" and _lookup(
                            human_message, "interactiveResponseMessage"
                        ):
                            text = structured_content["title"]
        reply_to_external_id, reply_to = _reply_values(context)
        is_forwarded, forwarding_score = _forwarding_values(context)
        normalized_message_id = (
            mutation.get("target_external_message_id")
            if mutation_event_type == "message.updated"
            else external_message_id
        )
        snapshot = {
            "provider": "wuzapi",
            "message_id": normalized_message_id,
            "timestamp": occurred_at.isoformat().replace("+00:00", "Z"),
            "source": _source_snapshot(info),
            "reply_to": dict(reply_to),
        }
        if associated_child:
            snapshot["association"] = associated_child
        push_name = _lookup(info, "PushName")
        normalized_message = None
        if not mutation_event_type or mutation_event_type == "message.updated":
            normalized_message = MessageDTO(
                external_message_id=normalized_message_id,
                client_message_id=(
                    external_message_id
                    if is_from_me and not mutation_event_type
                    else ""
                ),
                content_type=content_type,
                text=text,
                reply_to_external_id=reply_to_external_id,
                is_forwarded=is_forwarded,
                forwarding_score=forwarding_score,
                protocol_snapshot=snapshot,
                structured_content=structured_content,
                media=media,
            )
        normalized_event_type = mutation_event_type or "message.created"
        event_id = (
            _mutation_event_id(
                connection,
                normalized_event_type,
                mutation,
                info,
                occurred_at,
            )
            if mutation_event_type
            else "Message:%s" % external_message_id
        )
        return EventDTO(
            provider_schema_version=WUZAPI_VERSION,
            event_id=event_id,
            event_type=normalized_event_type,
            occurred_at=occurred_at,
            account_ref=connection.account_id.external_ref,
            connection_ref=connection.external_ref,
            conversation_ref=conversation_ref,
            platform=connection.account_id.platform,
            direction="outbound" if is_from_me else "inbound",
            is_from_me=is_from_me,
            origin="external_device" if is_from_me else "provider",
            actor=ActorDTO(
                addresses=actor_addresses,
                display_name=push_name if isinstance(push_name, str) else "",
            ),
            conversation=ConversationDTO(
                addresses=conversation_addresses,
                conversation_type=conversation_type,
            ),
            message=normalized_message,
            mutation=mutation,
            reply_to=reply_to,
            attribution=attribution,
            extensions={"provider.wuzapi": _event_metadata(envelope)},
        )

    def _normalize_receipt(self, connection, envelope, raw_event):
        raw_state = _lookup(envelope, "state")
        normalized_state = {
            "delivered": "delivered",
            "read": "read",
        }.get(_normalized_key(raw_state or ""))
        if not normalized_state:
            raise UnsupportedEventError(
                "WuzAPI ReadReceipt state is not implemented: %s"
                % (raw_state or "missing")
            )
        raw_ids = _lookup(raw_event, "MessageIDs", "MessageIds")
        if not isinstance(raw_ids, (list, tuple)):
            raise AdapterError("WuzAPI ReadReceipt has no MessageIDs array")
        if len(raw_ids) > _MAX_RECEIPT_MESSAGE_IDS:
            raise AdapterError("WuzAPI ReadReceipt has too many message IDs")
        message_ids = []
        seen_message_ids = set()
        for item in raw_ids:
            if isinstance(item, str) and not item.strip():
                continue
            message_id = _provider_message_id(
                item,
                "WuzAPI ReadReceipt message ID",
            )
            if message_id in seen_message_ids:
                continue
            seen_message_ids.add(message_id)
            message_ids.append(message_id)
        if not message_ids:
            raise UnsupportedEventError(
                "WuzAPI ReadReceipt has no non-empty message IDs"
            )
        occurred_at = _parse_timestamp(_lookup(raw_event, "Timestamp"))
        is_from_me = _required_is_from_me(raw_event, "WuzAPI ReadReceipt IsFromMe")
        (
            conversation_addresses,
            conversation_ref,
            conversation_type,
        ) = _conversation_values(raw_event, "event", is_from_me)
        event_id = _receipt_event_id(
            connection,
            normalized_state,
            message_ids,
            occurred_at,
            raw_event,
            is_from_me,
        )
        actor_addresses = (
            _group_actor_values(raw_event, "event", conversation_ref)
            if conversation_type == "group"
            else _actor_values(raw_event, "event")
        )
        return EventDTO(
            provider_schema_version=WUZAPI_VERSION,
            event_id=event_id,
            event_type="delivery.updated",
            occurred_at=occurred_at,
            account_ref=connection.account_id.external_ref,
            connection_ref=connection.external_ref,
            conversation_ref=conversation_ref,
            platform=connection.account_id.platform,
            # In whatsmeow/WuzAPI receipt events, ``IsFromMe`` describes the
            # receipt side.  False is remote evidence for one of our outbound
            # messages; true is our own device reading an inbound message.
            direction="inbound" if is_from_me else "outbound",
            is_from_me=is_from_me,
            origin="provider",
            actor=ActorDTO(
                addresses=actor_addresses,
                display_name="",
            ),
            conversation=ConversationDTO(
                addresses=conversation_addresses,
                conversation_type=conversation_type,
            ),
            delivery={
                "state": normalized_state,
                "external_message_ids": message_ids,
                "external_event_id": event_id,
            },
            extensions={
                "provider.wuzapi": dict(
                    _event_metadata(envelope),
                    source=_source_snapshot(raw_event),
                )
            },
        )

    def derive_client_message_id(self, command_id):
        try:
            return uuid.UUID(str(command_id)).hex.upper()
        except (AttributeError, TypeError, ValueError) as error:
            raise AdapterError("WuzAPI command ID must be a UUID") from error

    def prepare_reply_reference(
        self,
        connection,
        *,
        conversation_type,
        external_message_id,
        protocol_snapshot,
        protocol_participant,
    ):
        if conversation_type != "group":
            return super().prepare_reply_reference(
                connection,
                conversation_type=conversation_type,
                external_message_id=external_message_id,
                protocol_snapshot=protocol_snapshot,
                protocol_participant=protocol_participant,
            )
        reference = {
            "external_message_id": str(external_message_id or "").strip(),
            "protocol_participant": dict(protocol_participant or {}),
        }
        probe = CommandDTO(
            command_id="reply-reference-validation",
            command_type="send_message",
            account_ref="reply-reference-validation",
            connection_ref="reply-reference-validation",
            conversation_ref="reply-reference-validation",
            conversation=ConversationDTO(conversation_type="group"),
            message=MessageDTO(
                text="reply-reference-validation",
                reply_to_external_id=reference["external_message_id"],
            ),
            reply_to=reference,
        )
        _reply_context(probe)
        return reference

    @staticmethod
    def _provider_headers(connection):
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Token": connection.wuzapi_api_token,
        }

    @staticmethod
    def _webhook_configuration_http_error(response):
        status = response.status_code
        if status in (401, 403):
            raise ProviderPausedError("WuzAPI rejected the configured API token")
        if status == 429:
            error = ProviderRateLimitError(
                "WuzAPI webhook configuration was rate limited"
            )
            error.retry_after_seconds = _retry_after(response)
            raise error
        if status in (408, 425) or status >= 500:
            error = TransientAdapterError(
                "WuzAPI webhook configuration failed with HTTP %s" % status
            )
            error.retry_after_seconds = _retry_after(response)
            raise error
        raise AdapterError("WuzAPI webhook configuration failed with HTTP %s" % status)

    def _webhook_configuration_request(self, connection, method, payload=None):
        connection.ensure_one()
        connection.invalidate_recordset(["wuzapi_base_url", "wuzapi_api_token"])
        try:
            response = requests.request(
                method,
                "%s/webhook" % connection.wuzapi_base_url,
                headers=self._provider_headers(connection),
                json=payload,
                timeout=_WEBHOOK_CONFIGURATION_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException:
            raise TransientAdapterError(
                "WuzAPI webhook configuration endpoint did not respond"
            ) from None
        if not 200 <= response.status_code < 300:
            try:
                self._webhook_configuration_http_error(response)
            finally:
                response.close()
        response_payload = _limited_json_object(
            response,
            _MAX_WEBHOOK_CONFIGURATION_RESPONSE_BYTES,
            "WuzAPI webhook configuration",
        )
        if _lookup(response_payload, "success") is not True:
            raise AdapterError("WuzAPI rejected the webhook configuration request")
        data = _lookup(response_payload, "data")
        if not isinstance(data, dict):
            raise AdapterError("WuzAPI webhook configuration data is invalid")
        return data

    def get_webhook_configuration(self, connection):
        """Return the bounded, validated webhook state observed at WuzAPI."""

        data = self._webhook_configuration_request(connection, "GET")
        webhook_url = _lookup(data, "webhook")
        raw_events = _lookup(data, "subscribe")
        if not isinstance(webhook_url, str):
            raise AdapterError("WuzAPI webhook URL state is invalid")
        if isinstance(raw_events, str):
            raw_events = raw_events.split(",")
        if not isinstance(raw_events, (list, tuple)):
            raise AdapterError("WuzAPI webhook event state is invalid")
        events = []
        for raw_event in raw_events:
            if not isinstance(raw_event, str):
                raise AdapterError("WuzAPI webhook event state is invalid")
            event = raw_event.strip()
            if not event:
                continue
            if event not in WUZAPI_WEBHOOK_EVENT_TYPES:
                raise AdapterError(
                    "WuzAPI returned an event outside the pinned provider catalog"
                )
            events.append(event)
        if len(events) != len(set(events)):
            raise AdapterError("WuzAPI returned duplicate webhook events")
        return {
            "webhook_url": webhook_url.strip(),
            "events": tuple(sorted(events)),
        }

    def set_webhook_configuration(self, connection, webhook_url, events):
        """Replace the selected WuzAPI subscriptions and read them back."""

        if not isinstance(webhook_url, str):
            raise AdapterError("WuzAPI webhook URL is invalid")
        webhook_url = webhook_url.strip()
        try:
            parsed = urlsplit(webhook_url)
        except ValueError as error:
            raise AdapterError("WuzAPI webhook URL is invalid") from error
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or any(ord(character) < 32 for character in webhook_url)
        ):
            raise AdapterError("WuzAPI webhook URL is invalid")
        normalized_events = tuple(sorted(set(events or ())))
        if not normalized_events:
            raise AdapterError("Select at least one WuzAPI webhook event")
        if any(event not in WUZAPI_WEBHOOK_EVENT_TYPES for event in normalized_events):
            raise AdapterError("WuzAPI webhook event selection is invalid")
        if "All" in normalized_events and len(normalized_events) != 1:
            raise AdapterError("The WuzAPI All event must be selected alone")
        self._webhook_configuration_request(
            connection,
            "PUT",
            {
                "webhook": webhook_url,
                "events": list(normalized_events),
                "active": True,
            },
        )
        return self.get_webhook_configuration(connection)

    def set_hmac_configuration(self, connection, hmac_secret):
        """Replace WuzAPI's HMAC key through its pinned session endpoint."""

        connection.ensure_one()
        if (
            not isinstance(hmac_secret, str)
            or len(hmac_secret) < 32
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in hmac_secret
            )
        ):
            raise AdapterError("The WuzAPI HMAC key is invalid")
        connection.invalidate_recordset(["wuzapi_base_url", "wuzapi_api_token"])
        try:
            response = requests.request(
                "POST",
                "%s/session/hmac/config" % connection.wuzapi_base_url,
                headers=self._provider_headers(connection),
                json={"hmac_key": hmac_secret},
                timeout=_HMAC_CONFIGURATION_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException:
            raise TransientAdapterError(
                "WuzAPI HMAC configuration endpoint did not respond"
            ) from None
        if not 200 <= response.status_code < 300:
            try:
                self._webhook_configuration_http_error(response)
            finally:
                response.close()
        response_payload = _limited_json_object(
            response,
            _MAX_HMAC_CONFIGURATION_RESPONSE_BYTES,
            "WuzAPI HMAC configuration",
        )
        # API.md documents the standard wrapper, while the handler at pinned
        # commit 9487eca emits the exact Details object directly. Correlate only
        # those two known shapes; a generic 2xx remains an uncertain mutation.
        if not _is_hmac_configuration_ack(response_payload):
            raise AdapterError("WuzAPI HMAC configuration acknowledgement is invalid")
        return True

    def matches_outbound_mutation_echo(self, connection, command, event_mutation):
        """Compare an edit echo with the exact WhatsApp wire representation."""

        if (
            command.command_type != "edit_message"
            or event_mutation.get("type") != "edit"
        ):
            return False
        clean_text = (command.options or {}).get("new_text")
        signature = (command.options or {}).get("sender_signature")
        if not isinstance(clean_text, str) or not clean_text or not signature:
            return False
        expected_text = _signed_whatsapp_text(clean_text, command.options)
        return event_mutation.get("new_text") == expected_text

    def validate_outbound_signature(self, connection, text, sender_signature):
        """Validate the exact WuzAPI/WhatsApp wire framing before persistence."""

        super().validate_outbound_signature(connection, text, sender_signature)
        _signed_whatsapp_text(text, {"sender_signature": sender_signature})
        return True

    def validate_outbound_structured_content(self, connection, content, text):
        validate_outbound_content(content, text)
        return True

    def validate_recorded_audio_upload(
        self,
        connection,
        *,
        content,
        mimetype,
        is_voice_note,
        duration_seconds,
    ):
        """Keep WhatsApp PTT/container rules inside the WuzAPI adapter."""

        self._validate_recorded_audio_content(
            content=content,
            mimetype=mimetype,
            is_voice_note=is_voice_note,
            duration_seconds=duration_seconds,
        )
        return True

    @staticmethod
    def _validate_recorded_audio_content(
        *, content, mimetype, is_voice_note, duration_seconds
    ):
        """Validate a recorder container without provider I/O or mutable state."""

        normalized_mimetype = (mimetype or "").split(";", 1)[0].strip().lower()
        if is_voice_note and (
            normalized_mimetype != "audio/ogg" or not is_ogg_opus(content)
        ):
            raise AdapterError("WuzAPI voice notes require an OGG Opus attachment")
        if (
            not is_voice_note
            and duration_seconds
            and normalized_mimetype == "audio/ogg"
            and not is_ogg_opus(content)
        ):
            raise AdapterError("WuzAPI recorded OGG audio must use Opus")
        if duration_seconds and normalized_mimetype == "audio/mp4":
            if not is_iso_bmff_audio_only(content):
                raise AdapterError(
                    "WuzAPI recorded MP4 audio must not contain a video track"
                )
        if duration_seconds:
            try:
                canonical_duration = canonical_recorded_audio_duration_seconds(
                    content, normalized_mimetype
                )
            except ValidationError as error:
                raise AdapterError(str(error)) from error
            if canonical_duration != duration_seconds:
                raise AdapterError(
                    "WuzAPI recorded audio duration does not match its container"
                )
        return True

    def _target_phone(self, command):
        if not command.target_address:
            raise AdapterError("WuzAPI outbound command requires a target")
        phone = command.target_address.value_normalized.strip()
        if (
            not phone
            or any(character.isspace() for character in phone)
            or any(ord(character) < 32 for character in phone)
        ):
            raise AdapterError("WuzAPI outbound target is invalid")
        if command.conversation.conversation_type == "group":
            if (
                command.target_address.role != "group"
                or command.target_address.namespace != "whatsapp.group"
            ):
                raise AdapterError("WuzAPI group target must use a group address")
            normalized = self._group_strict_address(
                phone, "command.target_address"
            ).value_normalized
            if normalized != phone:
                raise AdapterError("WuzAPI group target is not canonical")
        return phone

    @staticmethod
    def _target_message_id(command):
        return _provider_message_id(
            command.options.get("target_external_message_id"),
            "WuzAPI mutation target message ID",
        )

    def _client_message_id(self, command):
        value = (
            command.client_message_id
            or (command.message.client_message_id if command.message else "")
            or self.derive_client_message_id(command.command_id)
        )
        if not _MESSAGE_ID_PATTERN.fullmatch(value):
            raise AdapterError(
                "WuzAPI client message ID must contain 32 uppercase hex characters"
            )
        return value

    @staticmethod
    def _validate_mime(kind, mime_type, *, provider_media_kind=""):
        if not isinstance(mime_type, str):
            raise AdapterError("WuzAPI media MIME type is missing")
        mime_type = mime_type.strip()
        base_mime = mime_type.split(";", 1)[0].strip().lower()
        if (
            not base_mime
            or "/" not in base_mime
            or any(ord(character) < 32 for character in mime_type)
        ):
            raise AdapterError("WuzAPI media MIME type is invalid")
        if provider_media_kind == "sticker":
            allowed = {"image/webp"} if kind == "image" else set()
        else:
            allowed = _MIME_BY_KIND[kind]
        if allowed is not None and base_mime not in allowed:
            raise AdapterError(
                "WuzAPI %s MIME type is not supported: %s" % (kind, base_mime)
            )
        if kind == "audio" and not base_mime.startswith("audio/"):
            raise AdapterError("WuzAPI audio MIME type is not supported")
        return mime_type

    def _outbound_media_content(self, media):
        locator = media.remote_locator
        attachment_id = locator.get("attachment_id") if isinstance(locator, dict) else 0
        if isinstance(attachment_id, str) and attachment_id.isdigit():
            attachment_id = int(attachment_id)
        if isinstance(attachment_id, bool) or not isinstance(attachment_id, int):
            raise AdapterError("WuzAPI outbound media requires an attachment_id")
        attachment = self.env["ir.attachment"].sudo().browse(attachment_id).exists()
        if not attachment or len(attachment) != 1 or attachment.type != "binary":
            raise AdapterError("WuzAPI outbound attachment does not exist")
        kind = media.kind
        if kind not in _MEDIA_FIELDS:
            raise AdapterError("WuzAPI outbound media kind is not implemented")
        maximum = _MAX_MEDIA_BYTES[kind]
        attachment_size = attachment.file_size or 0
        if media.size_bytes and media.size_bytes > maximum:
            raise AdapterError(
                "WuzAPI %s attachment exceeds the %s byte limit" % (kind, maximum)
            )
        if attachment_size > maximum:
            raise AdapterError(
                "WuzAPI %s attachment exceeds the %s byte limit" % (kind, maximum)
            )
        if media.size_bytes and attachment_size and media.size_bytes != attachment_size:
            raise AdapterError(
                "WuzAPI outbound attachment size does not match metadata"
            )
        mime_type = self._validate_mime(
            kind, media.mime_type or attachment.mimetype or "application/octet-stream"
        )
        file_name = _safe_filename(
            media.file_name or attachment.name,
            media.external_media_id or "attachment",
            mime_type,
        )

        # ``datas`` is a computed base64 representation in Odoo. Reading it would
        # keep both ``raw`` and base64 in the ORM cache and decoding it would create
        # a second full raw copy. ``raw`` honors both filestore and database-backed
        # attachments, while leaving the only required base64 encoding to the final
        # provider request builder.
        content = attachment.raw or b""
        if isinstance(content, memoryview):
            content = content.tobytes()
        elif isinstance(content, bytearray):
            content = bytes(content)
        elif not isinstance(content, bytes):
            raise AdapterError("WuzAPI outbound attachment is not binary")
        if not content or len(content) > maximum:
            raise AdapterError(
                "WuzAPI %s attachment exceeds the %s byte limit" % (kind, maximum)
            )
        if media.size_bytes and media.size_bytes != len(content):
            raise AdapterError(
                "WuzAPI outbound attachment size does not match metadata"
            )
        content_sha256 = hashlib.sha256(content).hexdigest()
        if media.sha256 and media.sha256.lower() != content_sha256:
            raise AdapterError(
                "WuzAPI outbound attachment hash does not match metadata"
            )
        return content, mime_type, file_name

    def _post_command(
        self,
        connection,
        endpoint,
        payload,
        timeout=_HTTP_TIMEOUT,
        *,
        require_external_message_id=True,
    ):
        try:
            response = requests.request(
                "POST",
                "%s%s" % (connection.wuzapi_base_url, endpoint),
                headers=self._provider_headers(connection),
                json=payload,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as error:
            if _is_pre_dispatch_connection_failure(error):
                return AdapterResult(
                    status="transient",
                    error_code="transport_not_connected",
                    error_message=(
                        "WuzAPI connection could not be established before dispatch"
                    ),
                )
            return AdapterResult(
                status="uncertain",
                error_code="transport_no_response",
                error_message=(
                    "WuzAPI did not return a response after dispatch may have begun"
                ),
            )
        if not 200 <= response.status_code < 300:
            try:
                return _error_result(response.status_code, _retry_after(response))
            finally:
                response.close()
        try:
            response_payload = _limited_json_object(
                response,
                _MAX_COMMAND_RESPONSE_BYTES,
                "WuzAPI command",
            )
        except (AdapterError, TransientAdapterError):
            return AdapterResult(
                status="uncertain",
                error_code="invalid_success_response",
                error_message=(
                    "WuzAPI returned an unreadable success response after dispatch"
                ),
            )
        if not response_payload:
            return AdapterResult(
                status="uncertain",
                error_code="invalid_success_response",
                error_message="WuzAPI success response could not be correlated",
            )
        if _lookup(response_payload, "success") is not True:
            provider_code = _lookup(response_payload, "code")
            status = provider_code if isinstance(provider_code, int) else 400
            return _error_result(status, _retry_after(response))
        if not require_external_message_id:
            data = _lookup(response_payload, "data")
            details = _lookup(data, "Details")
            if (
                not isinstance(data, dict)
                or not isinstance(details, str)
                or not details
            ):
                return AdapterResult(
                    status="uncertain",
                    error_code="invalid_success_response",
                    error_message=(
                        "WuzAPI returned an uncorrelated acknowledgement after dispatch"
                    ),
                    provider_response=_safe_send_response(response, response_payload),
                )
            return AdapterResult.success(
                provider_response=_safe_send_response(response, response_payload)
            )
        data = _lookup(response_payload, "data")
        external_message_id = _lookup(data, "Id", "ID")
        if not isinstance(external_message_id, str) or not external_message_id.strip():
            return AdapterResult(
                status="uncertain",
                error_code="missing_provider_message_id",
                error_message="WuzAPI accepted the command without a message ID",
                provider_response=_safe_send_response(response, response_payload),
            )
        return AdapterResult.success(
            external_message_id=external_message_id.strip(),
            provider_response=_safe_send_response(response, response_payload),
        )

    def _build_media_send_payload(self, media_items, message_text, audit_only):
        if len(media_items) != 1:
            raise AdapterError("WuzAPI sends exactly one media attachment per command")
        media = media_items[0]
        kind = media.kind
        if kind not in _MEDIA_FIELDS:
            raise AdapterError("WuzAPI outbound media kind is not implemented")
        if kind == "audio" and message_text:
            raise AdapterError("WuzAPI audio messages do not support captions")
        content, mime_type, file_name = self._outbound_media_content(media)
        if kind == "audio" and (media.is_voice_note or media.duration_seconds):
            self._validate_recorded_audio_content(
                content=content,
                mimetype=mime_type,
                is_voice_note=bool(media.is_voice_note),
                duration_seconds=media.duration_seconds or 0,
            )
        if kind == "audio" and media.is_voice_note:
            mime_type = "audio/ogg; codecs=opus"
        _provider_field, payload_field, endpoint, _download_path = _MEDIA_FIELDS[kind]
        data_mime = mime_type.replace(" ", "")
        payload = {"MimeType": mime_type}
        if audit_only:
            payload[payload_field] = {
                "content_omitted": True,
                "kind": kind,
                "mime_type": mime_type,
                "file_name": file_name,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        else:
            payload[payload_field] = "data:%s;base64,%s" % (
                data_mime,
                base64.b64encode(content).decode("ascii"),
            )
        if message_text:
            if len(message_text) > _MAX_TEXT_CHARS:
                raise AdapterError("WuzAPI media caption exceeds the text limit")
            payload["Caption"] = message_text
        if kind == "document":
            payload["FileName"] = file_name
        if kind == "audio":
            payload["ptt"] = bool(media.is_voice_note)
            payload["mimetype"] = mime_type
            payload.pop("MimeType", None)
            if media.duration_seconds:
                payload["Seconds"] = media.duration_seconds
        return endpoint, payload

    def _build_send_request(self, command, audit_only=False):
        if not command.message:
            raise AdapterError("WuzAPI send_message requires a message")
        phone = self._target_phone(command)
        client_message_id = self._client_message_id(command)
        message_text = _signed_whatsapp_text(command.message.text, command.options)
        media_items = command.message.media
        structured_content = command.message.structured_content
        if structured_content:
            if command.options.get("sender_signature"):
                raise AdapterError(
                    "WuzAPI structured messages cannot use a sender signature"
                )
            if media_items:
                raise AdapterError("WuzAPI structured messages cannot contain media")
            if command.message.content_type != structured_content.get("type"):
                raise AdapterError("WuzAPI structured message type is inconsistent")
            if command.conversation.conversation_type not in ("direct", "group"):
                raise AdapterError("WuzAPI structured conversation is unsupported")
            endpoint, payload = build_outbound_content(structured_content, message_text)
        elif media_items:
            endpoint, payload = self._build_media_send_payload(
                media_items, message_text, audit_only
            )
        else:
            if (
                command.message.content_type != "text"
                or not message_text
                or len(message_text) > _MAX_TEXT_CHARS
            ):
                raise AdapterError("WuzAPI send_message requires text or one media")
            endpoint = "/chat/send/text"
            payload = {"Body": message_text}
        payload.update(Phone=phone, Id=client_message_id)
        context_info = _reply_context(command)
        if context_info:
            payload["ContextInfo"] = context_info
        return (
            endpoint,
            payload,
            _MEDIA_DOWNLOAD_TIMEOUT if media_items else _HTTP_TIMEOUT,
        )

    def _send_message(self, connection, command):
        endpoint, payload, timeout = self._build_send_request(command)
        result = self._post_command(
            connection,
            endpoint,
            payload,
            timeout=timeout,
        )
        expected_message_id = self._client_message_id(command)
        if (
            result.status == "success"
            and result.external_message_id != expected_message_id
        ):
            return AdapterResult(
                status="uncertain",
                error_code="correlation_mismatch",
                error_message=("WuzAPI accepted the command with another message ID"),
                provider_response=result.provider_response,
            )
        return result

    def _build_mark_read_request(self, command):
        if command.conversation.conversation_type != "direct":
            raise AdapterError("WuzAPI mark_read only supports direct conversations")
        if command.message is not None:
            raise AdapterError("WuzAPI mark_read cannot contain a message")
        if set(command.options) != {"external_message_ids"}:
            raise AdapterError("WuzAPI mark_read options are invalid")
        target = command.target_address
        if not target:
            raise AdapterError("WuzAPI mark_read requires a target")
        phone = target.value_normalized.strip()
        if (
            target.namespace not in ("whatsapp.lid", "whatsapp.pn")
            or target.namespace != _jid_namespace(phone)
            or target.role not in ("primary", "alternate", "routing")
            or not _STRICT_NORMALIZED_PARTICIPANT_JID_PATTERN.fullmatch(phone)
        ):
            raise AdapterError("WuzAPI mark_read target is invalid")
        conversation_addresses = {
            (address.namespace, address.value_normalized)
            for address in command.conversation.addresses
        }
        if (target.namespace, phone) not in conversation_addresses:
            raise AdapterError("WuzAPI mark_read target is outside the conversation")
        message_ids = command.options.get("external_message_ids")
        if (
            not isinstance(message_ids, (list, tuple))
            or not message_ids
            or len(message_ids) > _MAX_MARK_READ_MESSAGE_IDS
        ):
            raise AdapterError("WuzAPI mark_read message IDs are invalid")
        normalized_ids = []
        for message_id in message_ids:
            normalized_ids.append(
                _provider_message_id(
                    message_id,
                    "WuzAPI mark_read message ID",
                    maximum=_MAX_MARK_READ_MESSAGE_ID_CHARS,
                )
            )
        if len(set(normalized_ids)) != len(normalized_ids):
            raise AdapterError("WuzAPI mark_read message IDs must be unique")
        return "/chat/markread", {"ChatPhone": phone, "Id": normalized_ids}

    def _mark_read(self, connection, command):
        endpoint, payload = self._build_mark_read_request(command)
        return self._post_command(
            connection,
            endpoint,
            payload,
            require_external_message_id=False,
        )

    def _build_mutation_request(self, command):
        phone = self._target_phone(command)
        target = self._target_message_id(command)
        is_group = command.conversation.conversation_type == "group"
        target_from_me = command.options.get("target_from_me")
        group_target_participant = ""
        if is_group:
            if not isinstance(target_from_me, bool):
                raise AdapterError(
                    "WuzAPI group mutation requires target_from_me evidence"
                )
            self._strict_command_group_participant(
                command.own_protocol_participant, "own_protocol_participant"
            )
            group_target_participant = self._strict_command_group_participant(
                command.target_protocol_participant,
                "target_protocol_participant",
            )
        if command.command_type == "react":
            operation = str(command.options.get("operation") or "add").lower()
            if operation not in ("add", "remove"):
                raise AdapterError("WuzAPI reaction operation must be add or remove")
            emoji = command.options.get("emoji") or ""
            if operation == "add" and (
                not isinstance(emoji, str)
                or not emoji
                or len(emoji) > 32
                or any(ord(character) < 32 for character in emoji)
            ):
                raise AdapterError("WuzAPI reaction emoji is invalid")
            target_from_me = (
                target_from_me
                if is_group
                else _boolean(command.options.get("target_from_me"))
            )
            payload = {
                "Phone": phone,
                "Body": emoji if operation == "add" else "remove",
                "Id": ("me:" if target_from_me else "") + target,
            }
            participant = (
                group_target_participant
                if is_group
                else _normalize_jid(command.options.get("target_participant") or "")
            )
            if participant and not target_from_me:
                if is_group:
                    if not _STRICT_NORMALIZED_PARTICIPANT_JID_PATTERN.fullmatch(
                        participant
                    ):
                        raise AdapterError("WuzAPI reaction participant is invalid")
                elif not _JID_PATTERN.fullmatch(participant):
                    raise AdapterError("WuzAPI reaction participant is invalid")
                payload["Participant"] = participant
            endpoint = "/chat/react"
        elif command.command_type == "edit_message":
            if is_group and target_from_me is not True:
                raise AdapterError(
                    "WuzAPI can only edit own messages in group conversations"
                )
            new_text = command.options.get("new_text") or (
                command.message.text if command.message else ""
            )
            new_text = _signed_whatsapp_text(new_text, command.options)
            if (
                not isinstance(new_text, str)
                or not new_text
                or len(new_text) > _MAX_TEXT_CHARS
            ):
                raise AdapterError("WuzAPI edited text is invalid")
            payload = {"Phone": phone, "Body": new_text, "Id": target}
            endpoint = "/chat/send/edit"
        elif command.command_type == "delete_message":
            if is_group and target_from_me is not True:
                raise AdapterError(
                    "WuzAPI can only delete own messages in group conversations"
                )
            payload = {"Phone": phone, "Id": target}
            endpoint = "/chat/delete"
        else:
            raise AdapterError(
                "WuzAPI command is not implemented: %s" % command.command_type
            )
        return endpoint, payload, target

    @staticmethod
    def _strict_command_group_participant(participant, field_name):
        if not isinstance(participant, AddressDTO):
            raise AdapterError("WuzAPI group %s is missing" % field_name)
        normalized = _normalize_jid(participant.value_normalized)
        if (
            participant.role != "sender"
            or participant.confidence != "protocol"
            or participant.namespace not in ("whatsapp.lid", "whatsapp.pn")
            or participant.namespace != _jid_namespace(normalized)
            or normalized != participant.value_normalized
            or not _STRICT_NORMALIZED_PARTICIPANT_JID_PATTERN.fullmatch(normalized)
        ):
            raise AdapterError("WuzAPI group %s is invalid" % field_name)
        return normalized

    def _send_mutation(self, connection, command):
        endpoint, payload, target = self._build_mutation_request(command)
        result = self._post_command(connection, endpoint, payload)
        if result.status == "success" and result.external_message_id != target:
            return AdapterResult(
                status="uncertain",
                error_code="mutation_target_mismatch",
                error_message="WuzAPI mutation response did not identify its target",
                provider_response=result.provider_response,
            )
        return result

    def prepare_request_snapshot(self, connection, command):
        if not isinstance(command, CommandDTO):
            raise AdapterError("WuzAPI requires a CommandDTO")
        if command.command_type == "send_message":
            endpoint, payload, _timeout = self._build_send_request(
                command, audit_only=True
            )
        elif command.command_type == "mark_read":
            endpoint, payload = self._build_mark_read_request(command)
        else:
            endpoint, payload, _target = self._build_mutation_request(command)
        return {
            "provider": self.key,
            "provider_version": self.provider_version,
            "method": "POST",
            "endpoint": endpoint,
            "payload": payload,
        }

    def execute_command(self, connection, command):
        if not isinstance(command, CommandDTO):
            raise AdapterError("WuzAPI requires a CommandDTO")
        if command.command_type == "send_message":
            return self._send_message(connection, command)
        if command.command_type == "mark_read":
            return self._mark_read(connection, command)
        return self._send_mutation(connection, command)

    @staticmethod
    def _download_http_error(response, response_payload=None):
        status = response.status_code
        if status in (401, 403):
            raise ProviderPausedError("WuzAPI rejected the configured API token")
        if status == 429:
            error = ProviderRateLimitError("WuzAPI media download was rate limited")
            error.retry_after_seconds = _retry_after(response)
            raise error
        if status in (408, 425) or status >= 500:
            if _is_invalid_media_hmac_response(status, response_payload):
                error = _InvalidMediaHmacError(
                    "WuzAPI media download failed integrity authentication"
                )
                error.retry_after_seconds = _retry_after(response)
                raise error
            error = TransientAdapterError(
                "WuzAPI media download failed with HTTP %s" % status
            )
            error.retry_after_seconds = _retry_after(response)
            raise error
        raise AdapterError("WuzAPI media download failed with HTTP %s" % status)

    @staticmethod
    def _limited_download_error_json(response):
        try:
            return _limited_json_object(
                response,
                _MAX_MEDIA_ERROR_RESPONSE_BYTES,
                "WuzAPI media error",
            )
        except (AdapterError, TransientAdapterError):
            # Error bodies are advisory. HTTP status remains authoritative when
            # the provider returns malformed, interrupted, or oversized JSON.
            return None

    @staticmethod
    def _limited_download_json(response, maximum_bytes):
        encoded_limit = ((maximum_bytes + 2) // 3) * 4 + 2 * 1024 * 1024
        content_length = _header(response.headers, "Content-Length")
        if content_length:
            try:
                if int(content_length) > encoded_limit:
                    raise AdapterError("WuzAPI media response exceeds the size limit")
            except ValueError:
                # A malformed optional Content-Length is not authoritative; the
                # bounded stream reader below remains the enforcement boundary.
                content_length = ""
        iterator = getattr(response, "iter_content", None)
        if callable(iterator):
            chunks = []
            total = 0
            try:
                for chunk in iterator(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > encoded_limit:
                        raise AdapterError(
                            "WuzAPI media response exceeds the size limit"
                        )
                    chunks.append(chunk)
                raw_payload = b"".join(chunks)
                payload = json.loads(raw_payload.decode("utf-8"))
            except (ValueError, RecursionError):
                raise AdapterError("WuzAPI media response is not valid JSON") from None
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        else:
            payload = _response_json(response)
            if (
                payload is not None
                and len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
                > encoded_limit
            ):
                raise AdapterError("WuzAPI media response exceeds the size limit")
        if not isinstance(payload, dict):
            raise AdapterError("WuzAPI media response is not a JSON object")
        return payload

    def _prepare_download_request(self, media):
        if not isinstance(media, MediaDTO):
            raise AdapterError("WuzAPI download requires a MediaDTO")
        kind = media.kind
        if kind not in _MEDIA_FIELDS:
            raise AdapterError("WuzAPI media kind is not downloadable")
        locator = media.remote_locator
        if not isinstance(locator, dict):
            raise AdapterError("WuzAPI media locator is invalid")
        provider_media_kind = locator.get("provider_media_kind") or ""
        if provider_media_kind not in ("", "sticker"):
            raise AdapterError("WuzAPI provider media kind is invalid")
        if provider_media_kind == "sticker":
            sticker_media = (
                kind,
                media.mime_type.split(";", 1)[0].strip().lower(),
            )
            if sticker_media not in {
                ("image", "image/webp"),
            }:
                raise AdapterError("WuzAPI sticker media metadata is invalid")
        url = locator.get("url") or ""
        direct_path = locator.get("direct_path") or ""
        media_key = locator.get("media_key") or ""
        file_sha256 = locator.get("file_sha256") or ""
        file_length = _optional_nonnegative_int(
            locator.get("file_length") or media.size_bytes
        )
        if not all(
            (
                (
                    isinstance(url, str)
                    and isinstance(direct_path, str)
                    and (url.strip() or direct_path.strip())
                ),
                isinstance(media_key, str) and media_key.strip(),
                isinstance(file_sha256, str) and file_sha256.strip(),
                file_length > 0,
            )
        ):
            raise AdapterError("WuzAPI media locator is incomplete")
        maximum = _MAX_MEDIA_BYTES[kind]
        if file_length > maximum:
            raise AdapterError(
                "WuzAPI %s media exceeds the %s byte limit" % (kind, maximum)
            )
        payload = {
            "MediaKey": media_key.strip(),
            "Mimetype": media.mime_type,
            "FileSHA256": file_sha256.strip(),
            "FileLength": file_length,
        }
        if url.strip():
            payload["Url"] = url.strip()
        if direct_path.strip():
            payload["DirectPath"] = direct_path.strip()
        for output_name, locator_name in (("FileEncSHA256", "file_enc_sha256"),):
            value = locator.get(locator_name)
            if isinstance(value, str) and value.strip():
                payload[output_name] = value.strip()
        return {
            "kind": kind,
            "maximum": maximum,
            "file_length": file_length,
            "file_sha256": file_sha256,
            "provider_media_kind": provider_media_kind,
            "endpoint": (
                "/chat/downloadsticker"
                if provider_media_kind == "sticker"
                else _MEDIA_FIELDS[kind][3]
            ),
            "payload": payload,
        }

    def _request_download_payload(self, connection, request_values):
        try:
            response = requests.request(
                "POST",
                "%s%s" % (connection.wuzapi_base_url, request_values["endpoint"]),
                headers=self._provider_headers(connection),
                json=request_values["payload"],
                timeout=_MEDIA_DOWNLOAD_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException:
            raise TransientAdapterError(
                "WuzAPI media download did not return a response"
            ) from None
        if not 200 <= response.status_code < 300:
            response_payload = (
                self._limited_download_error_json(response)
                if response.status_code == 500
                else None
            )
            response.close()
            self._download_http_error(response, response_payload)
        try:
            response_payload = self._limited_download_json(
                response, request_values["maximum"]
            )
        except requests.RequestException:
            raise TransientAdapterError(
                "WuzAPI media download response was interrupted"
            ) from None
        finally:
            response.close()
        return response, response_payload

    def _extract_download_data(self, response, response_payload):
        if _lookup(response_payload, "success") is not True:
            provider_code = _lookup(response_payload, "code")
            if isinstance(provider_code, int):
                response.status_code = provider_code
                self._download_http_error(response)
            raise AdapterError("WuzAPI rejected the media download")
        data = _lookup(response_payload, "data")
        data_uri = _lookup(data, "Data")
        response_mime = _lookup(data, "Mimetype")
        if not isinstance(data_uri, str) or not data_uri.startswith("data:"):
            raise AdapterError("WuzAPI media response has no data URI")
        return data_uri, response_mime

    def _decode_download_data_uri(
        self,
        kind,
        media,
        data_uri,
        response_mime,
        maximum,
        *,
        provider_media_kind="",
    ):
        header, separator, encoded_content = data_uri.partition(",")
        if not separator or ";base64" not in header.lower():
            raise AdapterError("WuzAPI media response is not base64")
        data_uri_mime = header[5:].split(";", 1)[0].strip()
        if data_uri_mime:
            data_uri_mime = self._validate_mime(
                kind,
                data_uri_mime,
                provider_media_kind=provider_media_kind,
            )
        mime_type = response_mime if isinstance(response_mime, str) else ""
        mime_type = self._validate_mime(
            kind,
            mime_type.strip() or data_uri_mime or media.mime_type,
            provider_media_kind=provider_media_kind,
        )
        if data_uri_mime and (
            mime_type.split(";", 1)[0].strip().lower()
            != data_uri_mime.split(";", 1)[0].strip().lower()
        ):
            raise AdapterError("WuzAPI media response MIME types do not match")
        if len(encoded_content) > ((maximum + 2) // 3) * 4 + 4:
            raise AdapterError("WuzAPI media data exceeds the size limit")
        try:
            content = base64.b64decode(encoded_content, validate=True)
        except (binascii.Error, ValueError) as error:
            raise AdapterError(
                "WuzAPI media response contains invalid base64"
            ) from error
        if not content or len(content) > maximum:
            raise AdapterError("WuzAPI media data exceeds the size limit")
        return content, mime_type

    @staticmethod
    def _validate_download_integrity(media, content, file_length, file_sha256):
        if len(content) != file_length:
            raise AdapterError("WuzAPI media size does not match provider metadata")
        actual_sha256 = hashlib.sha256(content).hexdigest()
        expected_sha256 = media.sha256 or _sha256_hex(file_sha256)
        if not expected_sha256 or actual_sha256 != expected_sha256.lower():
            raise AdapterError("WuzAPI media hash does not match provider metadata")
        return actual_sha256

    def download_media(self, connection, media):
        request_values = self._prepare_download_request(media)
        effective_kind = request_values["kind"]
        expected_fallback_mime = ""
        try:
            response, response_payload = self._request_download_payload(
                connection, request_values
            )
        except _InvalidMediaHmacError:
            base_mime = media.mime_type.split(";", 1)[0].strip().lower()
            if (
                request_values["kind"] != "document"
                or base_mime not in _MIME_BY_KIND["image"]
            ):
                raise
            image_maximum = _MAX_MEDIA_BYTES["image"]
            if request_values["file_length"] > image_maximum:
                raise AdapterError(
                    "WuzAPI image media exceeds the %s byte limit" % image_maximum
                ) from None
            fallback_request_values = dict(request_values)
            fallback_request_values["endpoint"] = "/chat/downloadimage"
            fallback_request_values["maximum"] = image_maximum
            response, response_payload = self._request_download_payload(
                connection, fallback_request_values
            )
            effective_kind = "image"
            expected_fallback_mime = base_mime
        try:
            data_uri, response_mime = self._extract_download_data(
                response, response_payload
            )
            content, mime_type = self._decode_download_data_uri(
                effective_kind,
                media,
                data_uri,
                response_mime,
                _MAX_MEDIA_BYTES[effective_kind],
                provider_media_kind=request_values["provider_media_kind"],
            )
            if (
                expected_fallback_mime
                and mime_type.split(";", 1)[0].strip().lower() != expected_fallback_mime
            ):
                raise AdapterError(
                    "WuzAPI image fallback MIME does not match document metadata"
                )
            actual_sha256 = self._validate_download_integrity(
                media,
                content,
                request_values["file_length"],
                request_values["file_sha256"],
            )
            file_name = _safe_filename(
                media.file_name,
                media.external_media_id or actual_sha256[:16],
                mime_type,
            )
            return MediaDownloadResult(
                content=content,
                mime_type=mime_type,
                file_name=file_name,
                size_bytes=len(content),
                sha256=actual_sha256,
            )
        finally:
            response.close()

    def get_capabilities(self, connection):
        return {
            "schema_version": "1.0",
            "send_message": True,
            "sender_signature": True,
            "mark_read": True,
            "react": True,
            "edit_message": True,
            "delete_message": True,
            "identity_avatar": True,
            "media": _media_capabilities(),
            "outbound_structured_content": copy.deepcopy(OUTBOUND_STRUCTURED_CONTENT),
            "conversation_types": {
                "group": {
                    "send_message": True,
                    "sender_signature": True,
                    "media": _media_capabilities(),
                    "outbound_structured_content": copy.deepcopy(
                        OUTBOUND_STRUCTURED_CONTENT
                    ),
                    "reply": True,
                    "reply_requires_participant": True,
                    "react": True,
                    "edit_message": True,
                    "delete_message": True,
                    "delivery_receipts": True,
                }
            },
            "extensions": {
                "provider.wuzapi": {
                    "baseline_version": self.provider_version,
                    "baseline_commit": self.provider_commit,
                    "transport_enabled": True,
                    "inbound_media_mode": "manual_download",
                    "media_mime_types": {
                        "image": ["image/jpeg", "image/png"],
                        "audio": ["audio/*"],
                        "video": ["video/mp4", "video/3gpp"],
                        "document": ["*/*"],
                    },
                    "max_media_bytes": dict(_MAX_MEDIA_BYTES),
                }
            },
        }

    @staticmethod
    def _health_response_payload(response, baseline):
        try:
            if response.status_code in (401, 403):
                return None, dict(baseline, state="error", reason="unauthorized")
            if response.status_code == 429:
                return None, dict(
                    baseline,
                    state="paused",
                    reason="rate_limited",
                    retry_after_seconds=_retry_after(response),
                )
            if not 200 <= response.status_code < 300:
                return None, dict(baseline, state="error", reason="provider_error")
            try:
                payload = _limited_json_object(
                    response,
                    _MAX_HEALTH_RESPONSE_BYTES,
                    "WuzAPI health",
                )
            except TransientAdapterError:
                return None, dict(baseline, state="error", reason="unreachable")
            except AdapterError:
                return None, dict(
                    baseline,
                    available=True,
                    state="degraded",
                    reason="invalid_response",
                )
            return payload, None
        finally:
            response.close()

    @staticmethod
    def _health_payload_values(baseline, payload, configured_identity):
        data = _lookup(payload, "data") if payload else None
        if _lookup(payload, "success") is not True or not isinstance(data, dict):
            return dict(
                baseline,
                available=True,
                state="degraded",
                reason="invalid_response",
            )
        connected = _optional_boolean(_lookup(data, "connected"))
        logged_in = _optional_boolean(_lookup(data, "loggedIn"))
        if connected is None or logged_in is None:
            values = dict(
                baseline,
                available=True,
                state="degraded",
                reason="invalid_response",
            )
            if connected is not None:
                values["connected"] = connected
            if logged_in is not None:
                values["logged_in"] = logged_in
            return values
        if not logged_in:
            state = "authentication_required"
            reason = "session_not_authenticated"
        elif not connected:
            state = "disconnected"
            reason = "session_disconnected"
        else:
            state = "connected"
            reason = "ready"
        values = dict(
            baseline,
            available=True,
            state=state,
            reason=reason,
            connected=connected,
            logged_in=logged_in,
        )
        if state == "connected":
            if not configured_identity:
                # With several WhatsApp sessions on one Odoo, a healthy token
                # proves only that *some* session is connected. Outbound remains
                # fail-closed until the account declares the expected identity.
                values.update(
                    state="degraded",
                    reason="identity_unverified",
                    identity_matches=False,
                )
            else:
                observed_identity = _jid_string(_lookup(data, "jid"))
                if not observed_identity:
                    values.update(
                        state="degraded",
                        reason="identity_unverified",
                        identity_matches=False,
                    )
                elif _normalize_session_identity(
                    configured_identity
                ) != _normalize_session_identity(observed_identity):
                    values.update(
                        state="degraded",
                        reason="identity_mismatch",
                        identity_matches=False,
                    )
                else:
                    values["identity_matches"] = True
        return values

    def get_health(self, connection):
        # A queue worker may have prefetched provider fields before an admin
        # rotated this connection in another transaction. Re-read every input
        # covered by health_configuration_revision before crossing the network
        # boundary; the core compares that revision again after the response.
        connection.invalidate_recordset(
            ["wuzapi_base_url", "wuzapi_api_token", "account_id"]
        )
        account = connection.account_id
        account.invalidate_recordset(["own_external_identity"])
        baseline = {
            "available": False,
            "provider": self.key,
            "baseline_version": self.provider_version,
            "baseline_commit": self.provider_commit,
        }
        try:
            response = requests.request(
                "GET",
                "%s/session/status" % connection.wuzapi_base_url,
                headers={
                    "Accept": "application/json",
                    "Token": connection.wuzapi_api_token,
                },
                timeout=_HEALTH_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException:
            return dict(baseline, state="error", reason="unreachable")
        payload, terminal = self._health_response_payload(response, baseline)
        if terminal:
            return terminal
        configured_identity = str(account.own_external_identity or "").strip()
        return self._health_payload_values(baseline, payload, configured_identity)
