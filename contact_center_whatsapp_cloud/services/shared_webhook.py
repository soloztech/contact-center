"""Atomic WhatsApp Cloud items for the shared Meta webhook ledger.

The shared sanitizer declares one claimable key per message, status and error of
a ``messages`` change and keeps only the structure in the immutable envelope.
This module builds the consumer side of that contract: one bounded,
allow-listed payload per atomic key. Nothing here is persisted directly; the
shared dispatcher validates and stores the returned specs.
"""

import hashlib
import json
import re

from odoo.addons.meta_webhook_base.services.sanitizer import (
    MAX_CHANGES_PER_ENTRY,
    MAX_WEBHOOK_ENTRIES,
    MetaWebhookSanitizationError,
    validate_sanitized_payload,
)

from .contracts import (
    WHATSAPP_CLOUD_CONSUMER_KEY,
    WHATSAPP_CLOUD_PAYLOAD_SCHEMA_VERSION,
    WHATSAPP_COLLECTIONS,
    WHATSAPP_OBJECT_TYPE,
    WHATSAPP_WEBHOOK_FIELD,
)

WHATSAPP_CLOUD_SUBSCRIPTION = {
    "object_type": WHATSAPP_OBJECT_TYPE,
    "fields": frozenset({WHATSAPP_WEBHOOK_FIELD}),
}

_ID_RE = re.compile(r"^[0-9]{1,40}$")
_PHONE_RE = re.compile(r"^[1-9][0-9]{5,19}$")
_BSUID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MIME_RE = re.compile(r"^[a-z0-9.+-]+/[a-z0-9.+_-]+$")
_ITEM_KEY_RE = re.compile(
    r"^entry:([0-9]{1,3}):changes:([0-9]{1,3}):(messages|statuses|errors):([0-9]{1,3})$"
)
_MAX_TEXT = 20_000
_MAX_LABEL = 1024
_MAX_TITLE = 256
_MAX_ERRORS = 5
_MAX_CONTACT_CARDS = 10
_MAX_CARD_VALUES = 10
_MAX_TIMESTAMP = 9_999_999_999_999
# The shared dispatcher refuses an item above 64 KiB and would then refuse the
# whole signed delivery. Stay below it, leaving room for the private references
# added at ingestion; a larger row stays an unclaimed placeholder.
_MAX_CLAIMED_ITEM_BYTES = 60 * 1024


class WhatsAppPayloadError(ValueError):
    """A provider row cannot be represented by the bounded consumer contract."""


def canonical_digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _text(value, field_name, maximum, *, required=False, single_line=False):
    if value is None:
        if required:
            raise WhatsAppPayloadError("%s is required" % field_name)
        return None
    if not isinstance(value, str):
        raise WhatsAppPayloadError("%s must be text" % field_name)
    if required and not value.strip():
        raise WhatsAppPayloadError("%s is required" % field_name)
    if len(value) > maximum:
        raise WhatsAppPayloadError("%s is too long" % field_name)
    allowed = "" if single_line else "\n\r\t"
    if any(
        (ord(character) < 32 and character not in allowed) or ord(character) == 127
        for character in value
    ):
        raise WhatsAppPayloadError("%s contains control characters" % field_name)
    return value


def _identifier(value, field_name, *, required=False, maximum=512):
    value = _text(value, field_name, maximum, required=required, single_line=True)
    if value is None:
        return None
    if not value or value != value.strip() or any(c.isspace() for c in value):
        raise WhatsAppPayloadError("%s is not a bounded identifier" % field_name)
    return value


def _pattern(value, pattern, field_name, *, required=False):
    if value is None and not required:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise WhatsAppPayloadError("%s is invalid" % field_name)
    return value


def _timestamp(value, field_name, *, required=False):
    if value is None and not required:
        return None
    if isinstance(value, bool):
        raise WhatsAppPayloadError("%s is invalid" % field_name)
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or not 0 < value <= _MAX_TIMESTAMP:
        raise WhatsAppPayloadError("%s is invalid" % field_name)
    return value


def _boolean(value):
    return value if isinstance(value, bool) else None


def _compact(values):
    return {key: value for key, value in values.items() if value is not None}


def _provider_error(value):
    """Keep only a numeric code and a one-line title (R18).

    ``message`` and ``error_data.details`` are free provider text and may repeat
    customer content; they never enter the shared ledger.
    """

    if not isinstance(value, dict):
        raise WhatsAppPayloadError("error must be an object")
    code = value.get("code")
    if isinstance(code, str) and code.strip().isdigit():
        code = int(code.strip())
    if isinstance(code, bool) or not isinstance(code, int) or not 0 <= code < 2**31:
        raise WhatsAppPayloadError("error.code is invalid")
    title = value.get("title")
    if isinstance(title, str):
        title = " ".join(
            "".join(
                character if ord(character) >= 32 and ord(character) != 127 else " "
                for character in title
            ).split()
        )[:_MAX_TITLE]
    else:
        title = ""
    return {"code": code, "title": title}


def _provider_errors(value):
    if value is None:
        return None
    if not isinstance(value, list):
        raise WhatsAppPayloadError("errors must be an array")
    return [_provider_error(item) for item in value[:_MAX_ERRORS]]


def _referral(value):
    """Keep bounded ad identity and copy; every URL stays out of the ledger."""

    if not isinstance(value, dict):
        return None
    result = {}
    for key, maximum in (
        ("source_type", 64),
        ("source_id", 256),
        ("ctwa_clid", 2048),
        ("media_type", 32),
    ):
        try:
            item = _identifier(value.get(key), "referral.%s" % key, maximum=maximum)
        except WhatsAppPayloadError:
            item = None
        if item:
            result[key] = item
    for key, maximum in (("headline", 2048), ("body", 4096)):
        try:
            item = _text(value.get(key), "referral.%s" % key, maximum)
        except WhatsAppPayloadError:
            item = None
        if item and item.strip():
            result[key] = item
    return result or None


def _context(value):
    if not isinstance(value, dict):
        return None
    result = _compact(
        {
            "id": _identifier(value.get("id"), "context.id"),
            "forwarded": _boolean(value.get("forwarded")),
            "frequently_forwarded": _boolean(value.get("frequently_forwarded")),
        }
    )
    return result or None


def _media(value, kind):
    if not isinstance(value, dict):
        raise WhatsAppPayloadError("%s must be an object" % kind)
    mime_type = value.get("mime_type")
    if isinstance(mime_type, str):
        mime_type = mime_type.split(";", 1)[0].strip().lower()
    return _compact(
        {
            "id": _pattern(value.get("id"), _ID_RE, "%s.id" % kind, required=True),
            "mime_type": _pattern(mime_type, _MIME_RE, "%s.mime_type" % kind),
            "sha256": _identifier(value.get("sha256"), "%s.sha256" % kind, maximum=128),
            "caption": _text(value.get("caption"), "%s.caption" % kind, _MAX_TEXT),
            "filename": _text(
                value.get("filename"), "%s.filename" % kind, 255, single_line=True
            ),
            "voice": _boolean(value.get("voice")) if kind == "audio" else None,
            "animated": _boolean(value.get("animated")) if kind == "sticker" else None,
        }
    )


def _location(value):
    if not isinstance(value, dict):
        raise WhatsAppPayloadError("location must be an object")
    result = {}
    for key in ("latitude", "longitude"):
        coordinate = value.get(key)
        if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
            raise WhatsAppPayloadError("location.%s is invalid" % key)
        result[key] = coordinate
    for key, maximum in (("name", _MAX_LABEL), ("address", 2000)):
        item = _text(value.get(key), "location.%s" % key, maximum)
        if item:
            result[key] = item
    return result


def _contact_card(value):
    if not isinstance(value, dict):
        raise WhatsAppPayloadError("contact card must be an object")
    name = value.get("name")
    name = name if isinstance(name, dict) else {}
    result = {
        "name": _compact(
            {
                key: _text(name.get(key), "contacts.name.%s" % key, 512)
                for key in ("formatted_name", "first_name", "last_name")
            }
        )
    }
    phones = []
    for phone in (value.get("phones") or [])[:_MAX_CARD_VALUES]:
        if isinstance(phone, dict):
            number = _text(phone.get("phone"), "contacts.phones.phone", 64)
            if number:
                phones.append(
                    _compact(
                        {
                            "phone": number,
                            "wa_id": _pattern(
                                phone.get("wa_id"), _PHONE_RE, "contacts.phones.wa_id"
                            )
                            if phone.get("wa_id")
                            else None,
                        }
                    )
                )
    emails = []
    for email in (value.get("emails") or [])[:_MAX_CARD_VALUES]:
        if isinstance(email, dict):
            address = _text(email.get("email"), "contacts.emails.email", 254)
            if address:
                emails.append({"email": address})
    if phones:
        result["phones"] = phones
    if emails:
        result["emails"] = emails
    return result


def _contact_cards(value):
    cards = []
    for item in (value if isinstance(value, list) else [])[:_MAX_CONTACT_CARDS]:
        try:
            cards.append(_contact_card(item))
        except WhatsAppPayloadError:
            # One malformed shared card must not drop the customer's message.
            continue
    return cards


def _interactive(value):
    if not isinstance(value, dict):
        raise WhatsAppPayloadError("interactive must be an object")
    kind = _pattern(value.get("type"), _TOKEN_RE, "interactive.type", required=True)
    result = {"type": kind}
    if kind in ("button_reply", "list_reply"):
        reply = value.get(kind)
        if not isinstance(reply, dict):
            raise WhatsAppPayloadError("interactive reply must be an object")
        result[kind] = _compact(
            {
                "id": _text(reply.get("id"), "interactive.id", 512, required=True),
                "title": _text(reply.get("title"), "interactive.title", 512),
                "description": _text(
                    reply.get("description"), "interactive.description", 1000
                ),
            }
        )
    return result


def _button(value):
    if not isinstance(value, dict):
        raise WhatsAppPayloadError("button must be an object")
    return _compact(
        {
            "payload": _text(value.get("payload"), "button.payload", 2048),
            "text": _text(value.get("text"), "button.text", 1024),
        }
    )


def _reaction(value):
    if not isinstance(value, dict):
        raise WhatsAppPayloadError("reaction must be an object")
    emoji = value.get("emoji")
    if emoji is None:
        emoji = ""
    return {
        "message_id": _identifier(
            value.get("message_id"), "reaction.message_id", required=True
        ),
        "emoji": _text(emoji, "reaction.emoji", 64, single_line=True),
    }


def _system(value):
    if not isinstance(value, dict):
        raise WhatsAppPayloadError("system must be an object")
    # The provider body repeats old and new phone numbers; only the event type
    # is needed for the control card.
    return _compact({"type": _pattern(value.get("type"), _TOKEN_RE, "system.type")})


_CONTENT_SANITIZERS = {
    "text": lambda value: {
        "body": _text(
            (value or {}).get("body") if isinstance(value, dict) else None,
            "text.body",
            _MAX_TEXT,
            required=True,
        )
    },
    "image": lambda value: _media(value, "image"),
    "audio": lambda value: _media(value, "audio"),
    "video": lambda value: _media(value, "video"),
    "document": lambda value: _media(value, "document"),
    "sticker": lambda value: _media(value, "sticker"),
    "location": _location,
    "contacts": _contact_cards,
    "interactive": _interactive,
    "button": _button,
    "reaction": _reaction,
    "system": _system,
}


def _message(value):
    if not isinstance(value, dict):
        raise WhatsAppPayloadError("message must be an object")
    message_type = _pattern(value.get("type"), _TOKEN_RE, "message.type", required=True)
    result = _compact(
        {
            "id": _identifier(value.get("id"), "message.id", required=True),
            "type": message_type,
            "from": _pattern(value.get("from"), _PHONE_RE, "message.from"),
            "from_user_id": _pattern(
                value.get("from_user_id"), _BSUID_RE, "message.from_user_id"
            ),
            "timestamp": _timestamp(
                value.get("timestamp"), "message.timestamp", required=True
            ),
            "context": _context(value.get("context")),
            "referral": _referral(value.get("referral")),
            "errors": _provider_errors(value.get("errors")),
        }
    )
    if not result.get("from") and not result.get("from_user_id"):
        raise WhatsAppPayloadError("message has no sender")
    sanitizer = _CONTENT_SANITIZERS.get(message_type)
    if sanitizer:
        result[message_type] = sanitizer(value.get(message_type))
    return result


def _contact(value, message):
    """Return the contact block of one message sender, never another contact."""

    if not isinstance(value, list):
        return None
    sender_phone = message.get("from")
    sender_user = message.get("from_user_id")
    for contact in value[:100]:
        if not isinstance(contact, dict):
            continue
        wa_id = contact.get("wa_id")
        user_id = contact.get("user_id")
        if not (
            (sender_phone and wa_id == sender_phone)
            or (sender_user and user_id == sender_user)
        ):
            continue
        profile = contact.get("profile")
        profile = profile if isinstance(profile, dict) else {}
        try:
            name = _text(profile.get("name"), "contact.name", 256, single_line=True)
        except WhatsAppPayloadError:
            name = None
        try:
            username = _identifier(
                profile.get("username"), "contact.username", maximum=128
            )
        except WhatsAppPayloadError:
            username = None
        result = _compact(
            {
                "wa_id": wa_id
                if isinstance(wa_id, str) and _PHONE_RE.fullmatch(wa_id)
                else None,
                "user_id": user_id
                if isinstance(user_id, str) and _BSUID_RE.fullmatch(user_id)
                else None,
                "profile": _compact({"name": name, "username": username}) or None,
            }
        )
        return result or None
    return None


def _status(value):
    if not isinstance(value, dict):
        raise WhatsAppPayloadError("status must be an object")
    callback = value.get("biz_opaque_callback_data")
    try:
        callback = _identifier(callback, "status.callback", maximum=512)
    except WhatsAppPayloadError:
        callback = None
    result = _compact(
        {
            "id": _identifier(value.get("id"), "status.id", required=True),
            "status": _pattern(
                value.get("status"), _TOKEN_RE, "status.status", required=True
            ),
            "timestamp": _timestamp(
                value.get("timestamp"), "status.timestamp", required=True
            ),
            "recipient_id": _pattern(
                value.get("recipient_id"), _PHONE_RE, "status.recipient_id"
            ),
            "recipient_user_id": _pattern(
                value.get("recipient_user_id"), _BSUID_RE, "status.recipient_user_id"
            ),
            "recipient_type": _pattern(
                value.get("recipient_type"), _TOKEN_RE, "status.recipient_type"
            ),
            "biz_opaque_callback_data": callback,
            "errors": _provider_errors(value.get("errors")),
        }
    )
    return result


def _metadata(value):
    if not isinstance(value, dict):
        raise WhatsAppPayloadError("metadata must be an object")
    display = value.get("display_phone_number")
    try:
        display = _text(display, "metadata.display_phone_number", 64, single_line=True)
    except WhatsAppPayloadError:
        display = None
    return _compact(
        {
            "phone_number_id": _pattern(
                value.get("phone_number_id"),
                _ID_RE,
                "metadata.phone_number_id",
                required=True,
            ),
            "display_phone_number": display or None,
        }
    )


def atomic_payload(entry, change_index, value, collection, index):
    """Return one sanitized atomic event or raise :class:`WhatsAppPayloadError`."""

    rows = value.get(collection)
    row = rows[index]
    payload = {
        "schema_version": WHATSAPP_CLOUD_PAYLOAD_SCHEMA_VERSION,
        "object": WHATSAPP_OBJECT_TYPE,
        "entry": _compact(
            {
                "id": entry["id"],
                "time": _timestamp(entry.get("time"), "entry.time")
                if entry.get("time") is not None
                else None,
            }
        ),
        "field": WHATSAPP_WEBHOOK_FIELD,
        "change_index": change_index,
        "collection": collection,
        "index": index,
        "metadata": _metadata(value.get("metadata")),
    }
    if collection == "messages":
        message = _message(row)
        payload["message"] = message
        contact = _contact(value.get("contacts"), message)
        if contact:
            payload["contact"] = contact
    elif collection == "statuses":
        payload["status"] = _status(row)
    else:
        payload["error"] = _provider_error(row)
    return payload


def declared_item_keys(sanitized_envelope):
    """Read the atomic keys the shared sanitizer declared for this delivery."""

    keys = []
    if not isinstance(sanitized_envelope, dict):
        return keys
    if sanitized_envelope.get("object") != WHATSAPP_OBJECT_TYPE:
        return keys
    for entry_index, entry in enumerate(
        (sanitized_envelope.get("entry") or [])[:MAX_WEBHOOK_ENTRIES]
    ):
        if not isinstance(entry, dict):
            continue
        for change in (entry.get("changes") or [])[:MAX_CHANGES_PER_ENTRY]:
            if (
                not isinstance(change, dict)
                or change.get("field") != WHATSAPP_WEBHOOK_FIELD
                or "reason" in change
                or not isinstance(change.get("change_index"), int)
            ):
                continue
            for collection in WHATSAPP_COLLECTIONS:
                count = change.get("%s_count" % collection)
                if isinstance(count, bool) or not isinstance(count, int):
                    continue
                keys.extend(
                    (
                        "entry:%s:changes:%s:%s:%s"
                        % (entry_index, change["change_index"], collection, index),
                        entry_index,
                        change["change_index"],
                        collection,
                        index,
                    )
                    for index in range(count)
                )
    return keys


def _raw_value(decoded_envelope, entry_index, change_index):
    try:
        entry = decoded_envelope["entry"][entry_index]
        change = entry["changes"][change_index]
    except (KeyError, IndexError, TypeError):
        return None, None
    if not isinstance(entry, dict) or not isinstance(change, dict):
        return None, None
    value = change.get("value")
    return entry, value if isinstance(value, dict) else None


def raw_row(decoded_envelope, payload):
    """Return the original provider row behind one sanitized atomic payload."""

    _entry, value = _raw_value(
        decoded_envelope, payload.get("entry_index", -1), payload.get("change_index")
    )
    rows = value.get(payload.get("collection")) if value else None
    index = payload.get("index")
    if (
        not isinstance(rows, list)
        or isinstance(index, bool)
        or not isinstance(index, int)
        or not 0 <= index < len(rows)
        or not isinstance(rows[index], dict)
    ):
        return None
    return rows[index]


def occurrence_identity(payload):
    """Return the semantic dedupe material for a message or status (not errors)."""

    phone_number_id = (payload.get("metadata") or {}).get("phone_number_id")
    if payload.get("collection") == "messages":
        message = payload.get("message") or {}
        return {
            "object": WHATSAPP_OBJECT_TYPE,
            "phone_number_id": phone_number_id,
            "type": "message",
            "wamid": message.get("id"),
        }
    if payload.get("collection") == "statuses":
        status = payload.get("status") or {}
        return {
            "object": WHATSAPP_OBJECT_TYPE,
            "phone_number_id": phone_number_id,
            "type": "status",
            "wamid": status.get("id"),
            "status": status.get("status"),
            "timestamp": status.get("timestamp"),
        }
    return None


def occurrence_digest(payload):
    identity = occurrence_identity(payload)
    return canonical_digest(identity) if identity else ""


def inbox_dedupe_key(payload):
    digest = occurrence_digest(payload)
    if not digest:
        return ""
    kind = "message" if payload.get("collection") == "messages" else "status"
    return "whatsapp_cloud:%s:sha256:%s" % (kind, digest)


def error_occurrence_digest(connection, delivery, item_key):
    """Identity of one WABA error: connection, exact delivery body and key (R18)."""

    return canonical_digest(
        {
            "connection_id": connection.id,
            "content_sha256": delivery.content_sha256,
            "item_key": item_key,
        }
    )


def _claimable(payload):
    """Apply the shared ledger validation before claiming, never after."""

    try:
        validate_sanitized_payload(payload)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (MetaWebhookSanitizationError, TypeError, ValueError):
        return False
    return len(encoded) <= _MAX_CLAIMED_ITEM_BYTES


def whatsapp_item_specs(decoded_envelope, delivery, *, eligible_item_keys=None):
    """Return claimable specs for the WhatsApp keys declared by the sanitizer.

    A row that cannot be represented by the bounded contract is simply not
    claimed: the shared dispatcher keeps its content-free placeholder.
    """

    if not isinstance(decoded_envelope, dict):
        return ()
    if str(decoded_envelope.get("object") or "").strip().lower() != (
        WHATSAPP_OBJECT_TYPE
    ):
        return ()
    eligible_item_keys = (
        None if eligible_item_keys is None else frozenset(eligible_item_keys)
    )
    sanitized_envelope = delivery.sanitized_envelope_json or {}
    result = []
    for item_key, entry_index, change_index, collection, index in declared_item_keys(
        sanitized_envelope
    ):
        if eligible_item_keys is not None and item_key not in eligible_item_keys:
            continue
        entry, value = _raw_value(decoded_envelope, entry_index, change_index)
        sanitized_entry = sanitized_envelope["entry"][entry_index]
        if (
            not entry
            or value is None
            or str(entry.get("id") or "").strip() != sanitized_entry.get("id")
            or not isinstance(value.get(collection), list)
            or index >= len(value[collection])
        ):
            continue
        try:
            payload = atomic_payload(
                sanitized_entry, change_index, value, collection, index
            )
        except (WhatsAppPayloadError, KeyError, TypeError, IndexError):
            continue
        payload["entry_index"] = entry_index
        if not _claimable(payload):
            continue
        if collection == "errors":
            occurrence_ref = "wac:error:%s" % canonical_digest(
                {
                    "entry": sanitized_entry["id"],
                    "content_sha256": delivery.content_sha256,
                    "item_key": item_key,
                }
            )
        else:
            occurrence_ref = "wac:%s:%s" % (
                "message" if collection == "messages" else "status",
                occurrence_digest(payload),
            )
        result.append(
            {
                "item_key": item_key,
                "kind": "messaging",
                "object_type": WHATSAPP_OBJECT_TYPE,
                "event_field": WHATSAPP_WEBHOOK_FIELD,
                "target_asset_id": sanitized_entry["id"],
                "occurrence_ref": occurrence_ref,
                "payload_json": payload,
            }
        )
    return tuple(result)


def claim_account_id(payload):
    """Return the inbox a claimed item belongs to, or ``None`` if unrecorded.

    Ingestion records the inbox of the one connection configured for the
    number when the item is claimed. A number later moved to another inbox
    never makes its earlier claims that inbox's, nor the reverse (CC-WAC-13).
    """

    claim = payload.get("claim") if isinstance(payload, dict) else None
    value = claim.get("account_id") if isinstance(claim, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def route_contract(payload):
    """Return the canonical route of one atomic item, or ``None``."""

    if not isinstance(payload, dict):
        return None
    if (
        payload.get("object") != WHATSAPP_OBJECT_TYPE
        or payload.get("field") != WHATSAPP_WEBHOOK_FIELD
        or payload.get("collection") not in WHATSAPP_COLLECTIONS
    ):
        return None
    entry = payload.get("entry")
    metadata = payload.get("metadata")
    if not isinstance(entry, dict) or not isinstance(metadata, dict):
        return None
    waba_id = entry.get("id")
    phone_number_id = metadata.get("phone_number_id")
    if not (
        isinstance(waba_id, str)
        and _ID_RE.fullmatch(waba_id)
        and isinstance(phone_number_id, str)
        and _ID_RE.fullmatch(phone_number_id)
    ):
        return None
    return {
        "object": WHATSAPP_OBJECT_TYPE,
        "waba_id": waba_id,
        "phone_number_id": phone_number_id,
        "collection": payload["collection"],
    }


def item_key_parts(item_key):
    match = _ITEM_KEY_RE.fullmatch(item_key or "")
    if not match:
        return None
    return {
        "entry_index": int(match.group(1)),
        "change_index": int(match.group(2)),
        "collection": match.group(3),
        "index": int(match.group(4)),
    }


def subscription_contract():
    return {
        "consumer_key": WHATSAPP_CLOUD_CONSUMER_KEY,
        "object_type": WHATSAPP_CLOUD_SUBSCRIPTION["object_type"],
        "fields": WHATSAPP_CLOUD_SUBSCRIPTION["fields"],
    }
