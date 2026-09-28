"""Translate one sanitized atomic WhatsApp Cloud event into EventDTO v1."""

import base64
import binascii
import datetime
import hashlib
import math
import re

from odoo.addons.contact_center_base.services.ad_origin_preview import (
    normalize_creative,
    public_source_url,
)
from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    UnsupportedEventError,
)
from odoo.addons.contact_center_base.services.dto import (
    ActorDTO,
    AttributionDTO,
    ConversationDTO,
    DTOValidationError,
    EventDTO,
    ExternalIdentifierDTO,
    MediaDTO,
    MessageDTO,
)
from odoo.addons.contact_center_base.services.structured_content import (
    validate_structured_content,
)

from .contracts import (
    WHATSAPP_CLOUD_PROVIDER_SCHEMA_VERSION,
    WHATSAPP_SYSTEM_EVENT_TYPE,
    WHATSAPP_SYSTEM_EXTENSION,
)
from .identity import remote_addresses
from .shared_webhook import occurrence_digest, route_contract

UNSUPPORTED_TEXT = "[Tipo de mensagem do WhatsApp não suportado]"
_MEDIA_KINDS = {
    "image": "image",
    "sticker": "image",
    "audio": "audio",
    "video": "video",
    "document": "document",
}
_DELIVERY_STATES = {
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    # A played voice message has necessarily been read.
    "played": "read",
}
_SYSTEM_TYPES = {
    "user_changed_number",
    "user_changed_user_id",
    "customer_changed_number",
    "customer_identity_changed",
}
_NUMERIC_ID = re.compile(r"^[0-9]{1,40}$")
_CALLBACK_RE = re.compile(
    r"^wac:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_MAX_SUMMARY_LINES = 10


def _required_mapping(value, field_name):
    if not isinstance(value, dict):
        raise AdapterError("WhatsApp %s must be an object" % field_name)
    return value


def _occurred_at(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AdapterError("WhatsApp timestamp must be a positive integer")
    try:
        return datetime.datetime.fromtimestamp(value, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise AdapterError("WhatsApp timestamp is outside the UTC range") from error


def _sha256_hex(value):
    """Accept the hex or base64 digest forms used by the Cloud API."""

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


def _label(value, maximum=512):
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    return "".join(
        character
        for character in value
        if ord(character) >= 32 and ord(character) != 127
    )[:maximum].strip()


def _summary(lines):
    result = []
    for line in lines:
        line = _label(line)
        if line and line not in result:
            result.append(line)
        if len(result) >= _MAX_SUMMARY_LINES:
            break
    return "\n".join(result)


def _route(connection, envelope):
    contract = route_contract(envelope)
    sudo_connection = connection.sudo()
    if (
        not contract
        or contract["phone_number_id"] != sudo_connection.wa_phone_number_id
        or contract["waba_id"] != sudo_connection.wa_business_account_id
    ):
        raise AdapterError("WhatsApp event route does not match its connection")
    if connection.account_id.platform != "whatsapp":
        raise AdapterError("WhatsApp Cloud requires a WhatsApp inbox")
    return contract


def _sender_ids(envelope, message):
    """The sender's phone and BSUID; the matched contact block completes them."""

    contact = envelope.get("contact")
    contact = contact if isinstance(contact, dict) else {}
    return (
        message.get("from") or contact.get("wa_id"),
        message.get("from_user_id") or contact.get("user_id"),
    )


def conversation_route(connection, envelope):
    """Classify routing headers only; used by ignore/delete privacy policies."""

    if not isinstance(envelope, dict):
        return None
    try:
        contract = _route(connection, envelope)
    except AdapterError:
        return None
    if contract["collection"] == "messages":
        message = envelope.get("message")
        if not isinstance(message, dict):
            return None
        wa_id, bsuid = _sender_ids(envelope, message)
    elif contract["collection"] == "statuses":
        status = envelope.get("status")
        if not isinstance(status, dict):
            return None
        wa_id, bsuid = status.get("recipient_id"), status.get("recipient_user_id")
    else:
        return None
    reference, addresses, _actors = remote_addresses(
        contract["waba_id"],
        wa_id,
        bsuid,
        phone_field="from",
        bsuid_field="from_user_id",
    )
    if not reference:
        return None
    return {
        "conversation_type": "direct",
        "conversation_ref": reference,
        "addresses": [
            (address.namespace, address.value_normalized) for address in addresses
        ],
    }


def _provider_extension(connection, contract, event_kind, **values):
    extension = {
        "graph_api_version": connection.sudo().wa_graph_version,
        "object": contract["object"],
        "phone_number_id": contract["phone_number_id"],
        "event_kind": event_kind,
    }
    extension.update(values)
    return {"provider.whatsapp_cloud": extension}


def _base_values(
    connection,
    contract,
    *,
    event_id,
    event_type,
    event_kind,
    occurred_at,
    wa_id,
    bsuid,
    display_name="",
    direction="inbound",
    phone_field="message.from",
    bsuid_field="message.from_user_id",
):
    reference, conversation, actors = remote_addresses(
        contract["waba_id"],
        wa_id,
        bsuid,
        phone_field=phone_field,
        bsuid_field=bsuid_field,
    )
    if not reference:
        raise AdapterError("WhatsApp event has no valid remote address")
    return {
        "provider_schema_version": WHATSAPP_CLOUD_PROVIDER_SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": _occurred_at(occurred_at),
        "account_ref": connection.account_id.external_ref,
        "connection_ref": connection.external_ref,
        "conversation_ref": reference,
        "platform": connection.account_id.platform,
        "direction": direction,
        "is_from_me": False,
        "origin": "provider",
        "actor": ActorDTO(addresses=actors, display_name=display_name),
        "conversation": ConversationDTO(
            addresses=conversation, conversation_type="direct"
        ),
        "extensions": _provider_extension(connection, contract, event_kind),
    }


def _attribution(referral):
    """Build one ad touchpoint only from a concrete ad identity (L01 rule)."""

    if not isinstance(referral, dict) or not referral:
        return ()
    source_type = referral.get("source_type")
    source_type = source_type.strip().lower() if isinstance(source_type, str) else ""
    source_id = referral.get("source_id")
    source_id = source_id if isinstance(source_id, str) else ""
    source_id = source_id if _NUMERIC_ID.fullmatch(source_id) else ""
    ctwa_clid = referral.get("ctwa_clid")
    ctwa_clid = ctwa_clid if isinstance(ctwa_clid, str) else ""
    if source_type != "ad" or not (source_id or ctwa_clid):
        # An empty or marker-only referral proves no paid click.
        return ()
    try:
        identifiers = []
        if source_id:
            identifiers.append(
                ExternalIdentifierDTO(
                    namespace="meta.source_id",
                    role="ad_source",
                    value=source_id,
                    source_field="message.referral.source_id",
                )
            )
        if ctwa_clid:
            identifiers.append(
                ExternalIdentifierDTO(
                    namespace="meta.ctwa_clid",
                    role="click",
                    value=ctwa_clid,
                    source_field="message.referral.ctwa_clid",
                )
            )
        media_type = referral.get("media_type")
        creative = normalize_creative(
            {
                "title": referral.get("headline"),
                "body": referral.get("body"),
                "media_type": media_type.lower() if isinstance(media_type, str) else "",
                "thumbnail_ref": referral.get("thumbnail_ref"),
            }
        )
        return (
            AttributionDTO(
                touchpoint_type="paid_ad_click",
                evidence_level="provider_asserted",
                network="meta",
                source_platform="",
                source_type="ad",
                # Only the private inbox envelope can carry the permalink; the
                # public validator runs again on every normalization.
                source_url=public_source_url(referral.get("source_url")),
                external_identifiers=tuple(identifiers),
                creative=creative,
            ),
        )
    except DTOValidationError:
        # Acquisition telemetry never suppresses the customer's message.
        return ()


def _media_content(message, message_type):
    provider_media = _required_mapping(message.get(message_type), message_type)
    kind = _MEDIA_KINDS[message_type]
    media_id = provider_media.get("id")
    if not isinstance(media_id, str) or not _NUMERIC_ID.fullmatch(media_id):
        raise AdapterError("WhatsApp media has no provider media ID")
    mime_type = provider_media.get("mime_type")
    mime_type = mime_type if isinstance(mime_type, str) else ""
    file_name = provider_media.get("filename") if message_type == "document" else ""
    media = MediaDTO(
        kind=kind,
        external_media_id=media_id,
        remote_locator={"media_id": media_id},
        mime_type=mime_type,
        file_name=_label(file_name, 255) if isinstance(file_name, str) else "",
        sha256=_sha256_hex(provider_media.get("sha256")),
        is_voice_note=bool(kind == "audio" and provider_media.get("voice") is True),
    )
    caption = provider_media.get("caption")
    text = caption if isinstance(caption, str) else ""
    if message_type == "sticker" and not text:
        text = "[Figurinha]"
    return {
        "text": text,
        "content_type": "sticker" if message_type == "sticker" else kind,
        "media": (media,),
        "structured_content": {},
    }


def _location_content(message):
    location = _required_mapping(message.get("location"), "location")
    latitude, longitude = location.get("latitude"), location.get("longitude")
    for coordinate, bound in ((latitude, 90), (longitude, 180)):
        if (
            isinstance(coordinate, bool)
            or not isinstance(coordinate, (int, float))
            or not math.isfinite(coordinate)
            or abs(coordinate) > bound
        ):
            raise AdapterError("WhatsApp location coordinates are invalid")
    name = _label(location.get("name"), 120)
    address = _label(location.get("address"), 300)
    content = {
        "type": "location",
        "latitude": float(latitude),
        "longitude": float(longitude),
        "live": False,
    }
    if name:
        content["name"] = name
    if address:
        content["address"] = address
    lines = ["Localização: %s" % name if name else "[Localização]", address]
    lines.append("%.6f, %.6f" % (float(latitude), float(longitude)))
    return {
        "text": _summary(lines),
        "content_type": "location",
        "media": (),
        "structured_content": content,
    }


def _contacts_content(message):
    cards = message.get("contacts")
    if not isinstance(cards, list) or not cards:
        raise UnsupportedEventError("WhatsApp contacts message has no card")
    contacts = []
    for card in cards[:10]:
        if not isinstance(card, dict):
            continue
        name = card.get("name") if isinstance(card.get("name"), dict) else {}
        display = _label(name.get("formatted_name")) or _label(
            " ".join(
                value
                for value in (name.get("first_name"), name.get("last_name"))
                if isinstance(value, str)
            )
        )
        phones = []
        for phone in card.get("phones") or []:
            number = re.sub(r"[\s().-]", "", phone.get("phone") or "")
            if re.fullmatch(r"\+?[0-9]{3,29}", number) and number not in phones:
                phones.append(number)
        emails = []
        for email in card.get("emails") or []:
            value = (email.get("email") or "").strip()
            if (
                len(value) <= 254
                and re.fullmatch(r"[^@\s?&#<>]+@[^@\s?&#<>]+\.[^@\s?&#<>]+", value)
                and value not in emails
            ):
                emails.append(value)
        contacts.append(
            {"name": display or "Contato", "phones": phones[:5], "emails": emails[:5]}
        )
    if not contacts:
        raise UnsupportedEventError("WhatsApp contacts message has no valid card")
    content = {"type": "contacts", "contacts": contacts}
    validate_structured_content(content)
    names = [contact["name"] for contact in contacts]
    lines = (
        ["Contato: %s" % names[0]]
        if len(names) == 1
        else ["[Contatos]"] + ["• %s" % name for name in names]
    )
    return {
        "text": _summary(lines),
        "content_type": "contacts",
        "media": (),
        "structured_content": content,
    }


def _selection_content(identifier, title):
    if not isinstance(identifier, str) or not identifier.strip():
        raise UnsupportedEventError("WhatsApp reply selection has no identifier")
    title = _label(title) or _label(identifier)
    content = {"type": "selection", "id": identifier[:512], "title": title}
    validate_structured_content(content)
    return {
        "text": title,
        "content_type": "selection",
        "media": (),
        "structured_content": content,
    }


def _interactive_content(message):
    interactive = _required_mapping(message.get("interactive"), "interactive")
    kind = interactive.get("type")
    if kind not in ("button_reply", "list_reply"):
        raise UnsupportedEventError("WhatsApp interactive reply is not implemented")
    reply = _required_mapping(interactive.get(kind), kind)
    return _selection_content(reply.get("id"), reply.get("title"))


def _button_content(message):
    button = _required_mapping(message.get("button"), "button")
    payload = button.get("payload")
    text = button.get("text")
    return _selection_content(payload if payload else text, text)


def _message_content(message):
    message_type = message.get("type")
    if message_type == "text":
        body = (message.get("text") or {}).get("body")
        if not isinstance(body, str) or not body.strip():
            raise UnsupportedEventError("WhatsApp text message is empty")
        return {
            "text": body,
            "content_type": "text",
            "media": (),
            "structured_content": {},
        }
    if message_type in _MEDIA_KINDS:
        return _media_content(message, message_type)
    if message_type == "location":
        return _location_content(message)
    if message_type == "contacts":
        return _contacts_content(message)
    if message_type == "interactive":
        return _interactive_content(message)
    if message_type == "button":
        return _button_content(message)
    raise UnsupportedEventError(
        "WhatsApp message type is not implemented: %s" % (message_type or "missing")
    )


def _unsupported_content(message):
    codes = [
        error.get("code")
        for error in (message.get("errors") or [])
        if isinstance(error, dict) and isinstance(error.get("code"), int)
    ]
    return {
        "text": UNSUPPORTED_TEXT,
        "content_type": "unsupported",
        "media": (),
        "structured_content": {},
        "codes": codes,
    }


def _reply_values(message):
    context = message.get("context")
    if not isinstance(context, dict):
        return "", {}, False
    reply_id = context.get("id")
    reply_id = reply_id if isinstance(reply_id, str) and reply_id.strip() else ""
    forwarded = bool(
        context.get("forwarded") is True or context.get("frequently_forwarded") is True
    )
    return (
        reply_id,
        ({"external_message_id": reply_id} if reply_id else {}),
        forwarded,
    )


def _profile_name(envelope):
    contact = envelope.get("contact")
    profile = contact.get("profile") if isinstance(contact, dict) else None
    name = profile.get("name") if isinstance(profile, dict) else ""
    return _label(name, 255)


def _normalize_reaction(connection, envelope, contract, message):
    reaction = _required_mapping(message.get("reaction"), "reaction")
    target = reaction.get("message_id")
    if not isinstance(target, str) or not target.strip():
        raise AdapterError("WhatsApp reaction has no target message")
    emoji = reaction.get("emoji") or ""
    wa_id, bsuid = _sender_ids(envelope, message)
    values = _base_values(
        connection,
        contract,
        event_id="MessageReaction:%s" % message["id"],
        event_type="message.reaction",
        event_kind="reaction",
        occurred_at=message.get("timestamp"),
        wa_id=wa_id,
        bsuid=bsuid,
        display_name=_profile_name(envelope),
    )
    values["mutation"] = {
        "type": "react",
        "target_external_message_id": target,
        # An empty emoji is WhatsApp's removal of the previous reaction.
        "operation": "add" if emoji else "remove",
        "emoji": emoji,
    }
    return EventDTO(**values)


def _normalize_system(connection, envelope, contract, message):
    system = message.get("system") if isinstance(message.get("system"), dict) else {}
    system_type = system.get("type")
    system_type = system_type if system_type in _SYSTEM_TYPES else "other"
    wa_id, bsuid = _sender_ids(envelope, message)
    values = _base_values(
        connection,
        contract,
        event_id="SystemMessage:%s" % message["id"],
        event_type=WHATSAPP_SYSTEM_EVENT_TYPE,
        event_kind="system",
        occurred_at=message.get("timestamp"),
        wa_id=wa_id,
        bsuid=bsuid,
    )
    values["extensions"][WHATSAPP_SYSTEM_EXTENSION] = {
        "system_type": system_type,
        "message_id": message["id"],
    }
    return EventDTO(**values)


def _normalize_message(connection, envelope, contract):
    message = _required_mapping(envelope.get("message"), "message")
    external_message_id = message.get("id")
    if not isinstance(external_message_id, str) or not external_message_id.strip():
        raise AdapterError("WhatsApp message has no ID")
    message_type = message.get("type")
    if message_type == "reaction":
        return _normalize_reaction(connection, envelope, contract, message)
    if message_type == "system":
        return _normalize_system(connection, envelope, contract, message)
    reply_to_external_id, reply_to, forwarded = _reply_values(message)
    if reply_to_external_id == external_message_id:
        raise AdapterError("WhatsApp message cannot reply to itself")
    unsupported_codes = []
    try:
        content = _message_content(message)
    except UnsupportedEventError:
        content = _unsupported_content(message)
        unsupported_codes = content.pop("codes")
    wa_id, bsuid = _sender_ids(envelope, message)
    values = _base_values(
        connection,
        contract,
        event_id="Message:%s" % external_message_id,
        event_type="message.created",
        event_kind="message",
        occurred_at=message.get("timestamp"),
        wa_id=wa_id,
        bsuid=bsuid,
        display_name=_profile_name(envelope),
    )
    occurred_at_text = values["occurred_at"].isoformat().replace("+00:00", "Z")
    snapshot = {
        "provider": "whatsapp_cloud",
        "message_id": external_message_id,
        "timestamp": occurred_at_text,
        "phone_number_id": contract["phone_number_id"],
        "type": message_type,
        "reply_to": dict(reply_to),
    }
    extension = values["extensions"]["provider.whatsapp_cloud"]
    extension["message_type"] = message_type
    if content["content_type"] == "unsupported":
        extension["unsupported_content"] = message_type or "missing"
        if unsupported_codes:
            extension["provider_error_codes"] = unsupported_codes[:5]
    values.update(
        {
            "message": MessageDTO(
                external_message_id=external_message_id,
                content_type=content["content_type"],
                text=content["text"],
                reply_to_external_id=reply_to_external_id,
                is_forwarded=forwarded,
                protocol_snapshot=snapshot,
                media=content["media"],
                structured_content=content["structured_content"],
            ),
            "reply_to": reply_to,
            "attribution": _attribution(message.get("referral")),
        }
    )
    return EventDTO(**values)


def status_correlation_id(status):
    """Return the core correlation ID of one status and whether it is ours."""

    callback = status.get("biz_opaque_callback_data")
    if isinstance(callback, str) and _CALLBACK_RE.fullmatch(callback):
        return callback, True
    return status.get("id"), False


def _normalize_status(connection, envelope, contract):
    status = _required_mapping(envelope.get("status"), "status")
    wamid = status.get("id")
    if not isinstance(wamid, str) or not wamid.strip():
        raise AdapterError("WhatsApp status has no message ID")
    if status.get("recipient_type") not in (None, "individual"):
        raise UnsupportedEventError("WhatsApp group statuses are not implemented")
    state = _DELIVERY_STATES.get(status.get("status"))
    if not state:
        # ``failed`` is not a core delivery state: the consumer records it in the
        # local failure ledger and never creates an inbox event for it.
        raise UnsupportedEventError(
            "WhatsApp status is not a core delivery state: %s"
            % (status.get("status") or "missing")
        )
    correlation_id, by_callback = status_correlation_id(status)
    event_id = "MessageStatus:%s" % occurrence_digest(envelope)
    values = _base_values(
        connection,
        contract,
        event_id=event_id,
        event_type="delivery.updated",
        event_kind="status",
        occurred_at=status.get("timestamp"),
        wa_id=status.get("recipient_id"),
        bsuid=status.get("recipient_user_id"),
        direction="outbound",
        phone_field="status.recipient_id",
        bsuid_field="status.recipient_user_id",
    )
    values["delivery"] = {
        "state": state,
        "external_message_ids": [correlation_id],
        "external_event_id": event_id,
    }
    extension = values["extensions"]["provider.whatsapp_cloud"]
    extension.update(
        {
            "status": status.get("status"),
            "provider_message_id": wamid,
            "correlation": "client_message_id" if by_callback else "provider",
        }
    )
    return EventDTO(**values)


def normalize_whatsapp_cloud_event(connection, envelope):
    """Translate one sanitized atomic Cloud API event (message or status)."""

    if not isinstance(envelope, dict):
        raise AdapterError("WhatsApp Cloud event must be an object")
    contract = _route(connection, envelope)
    if contract["collection"] == "messages":
        return _normalize_message(connection, envelope, contract)
    if contract["collection"] == "statuses":
        return _normalize_status(connection, envelope, contract)
    raise UnsupportedEventError("WhatsApp Cloud webhook errors are not conversations")


def history_creative(envelope, source_key):
    """Recover presentation copy for an already recorded ad touchpoint."""

    message = envelope.get("message") if isinstance(envelope, dict) else None
    if not isinstance(message, dict) or message.get("id") != source_key:
        return {}
    referral = message.get("referral")
    if not isinstance(referral, dict):
        return {}
    media_type = referral.get("media_type")
    return normalize_creative(
        {
            "title": referral.get("headline"),
            "body": referral.get("body"),
            "media_type": media_type.lower() if isinstance(media_type, str) else "",
            "public_url": referral.get("source_url"),
        }
    )
