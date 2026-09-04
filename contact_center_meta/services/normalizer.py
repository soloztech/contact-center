import datetime
import re

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    UnsupportedEventError,
)
from odoo.addons.contact_center_base.services.dto import (
    ActorDTO,
    AddressDTO,
    AttributionDTO,
    ConversationDTO,
    DTOValidationError,
    EventDTO,
    ExternalIdentifierDTO,
    MediaDTO,
    MessageDTO,
)

from .contracts import META_PROVIDER_SCHEMA_VERSION
from .messaging import atomic_dedupe_key, routing_values

_PLATFORM_CONTRACTS = {
    "messenger": {
        "object": "page",
        "transport_mode": "messenger_page",
        "remote_namespace": "meta.messenger.psid",
        "asset_namespace": "meta.messenger.page",
    },
    "instagram": {
        "object": "instagram",
        "transport_mode": "instagram_page_linked",
        "remote_namespace": "meta.instagram.igsid",
        "asset_namespace": "meta.instagram.account",
    },
}
_EVENT_CARRIERS = frozenset(
    {"postback", "reaction", "delivery", "read", "referral", "message_edit"}
)
_MEDIA_KINDS = {
    "image": "image",
    "sticker": "image",
    "audio": "audio",
    "video": "video",
    "file": "document",
}
_SOCIAL_ATTACHMENT_KINDS = {
    "share": "social_post",
    "ig_post": "social_post",
    "story_mention": "story_mention",
    "ig_reel": "reel",
    "reel": "reel",
}
_SOCIAL_ATTACHMENT_LABELS = {
    "instagram": {
        "social_post": "Instagram post shared",
        "story_mention": "Instagram story mention",
        "reel": "Instagram reel shared",
    },
    "messenger": {
        "social_post": "Facebook post shared",
        "story_mention": "Facebook story mention",
        "reel": "Facebook reel shared",
    },
}
_PRIVATE_LOCATOR_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_PATTERN = re.compile(r"[^a-z0-9]+")
_CRITICAL_MESSAGE_REJECTIONS = frozenset(
    {
        "invalid_is_echo",
        "invalid_is_self",
        "invalid_is_deleted",
        "invalid_is_unsupported",
    }
)
_UNSAFE_CONTENT_REJECTIONS = frozenset(
    {
        "invalid_attachment_array",
        "too_many_attachments",
        "invalid_attachment",
        "invalid_attachment_type",
        "invalid_https_url",
        "invalid_quick_reply",
        "invalid_story",
        "invalid_story_id",
    }
)


def _required_mapping(value, field_name):
    if not isinstance(value, dict):
        raise AdapterError("Meta %s must be an object" % field_name)
    return value


def _required_id(value, field_name):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 256
        or any(character.isspace() for character in value)
    ):
        raise AdapterError("Meta %s must be a bounded identifier" % field_name)
    return value.strip()


def _occurred_at(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AdapterError("Meta messaging.timestamp must be a positive integer")
    try:
        return datetime.datetime.fromtimestamp(value / 1000.0, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise AdapterError(
            "Meta messaging.timestamp is outside the UTC range"
        ) from error


def _canonical_token(value):
    if not isinstance(value, str):
        return ""
    token = _TOKEN_PATTERN.sub("_", value.strip().lower()).strip("_")
    if not token or not token[0].isalpha():
        return ""
    return token[:128]


def _bounded_text(value, field_name, maximum, *, required=False):
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise AdapterError("Meta %s must be a string" % field_name)
    if required and not value.strip():
        raise AdapterError("Meta %s must be a non-empty string" % field_name)
    if len(value) > maximum:
        raise AdapterError("Meta %s is too long" % field_name)
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        raise AdapterError("Meta %s contains control characters" % field_name)
    return value


def _attribution_values(container, platform, is_from_me, source_field):
    """Normalize bounded Meta referral evidence without inferring a CRM record."""

    if is_from_me:
        return ()
    referral = container.get("referral")
    if not isinstance(referral, dict) or not referral:
        return ()
    source = _canonical_token(referral.get("source"))
    if source == "shortlinks":
        # Meta's current official references use both SHORTLINK and SHORTLINKS.
        source = "shortlink"
    referral_type = _canonical_token(referral.get("type"))
    ad_id = referral.get("ad_id") or ""
    ref = referral.get("ref") or ""
    if source in {"ad", "ads"} or ad_id:
        touchpoint_type = "paid_ad_click"
        evidence_level = "provider_asserted"
    elif referral_type or ref:
        touchpoint_type = "entry_point"
        evidence_level = "provider_hint"
    else:
        return ()

    try:
        identifiers = []
        if ad_id:
            identifiers.append(
                ExternalIdentifierDTO(
                    namespace="meta.ad_id",
                    role="ad_source",
                    value=ad_id,
                    source_field="%s.ad_id" % source_field,
                )
            )
        if ref:
            identifiers.append(
                ExternalIdentifierDTO(
                    namespace="meta.ref",
                    role="entry_reference",
                    value=ref,
                    source_field="%s.ref" % source_field,
                )
            )
        return (
            AttributionDTO(
                touchpoint_type=touchpoint_type,
                evidence_level=evidence_level,
                network="meta",
                source_platform=platform,
                source_type="ad" if source in {"ad", "ads"} else source,
                external_identifiers=tuple(identifiers),
                entry_point=({"source": referral_type} if referral_type else {}),
            ),
        )
    except DTOValidationError:
        # Referral is optional acquisition telemetry. Provider drift or malformed
        # campaign parameters must never suppress an otherwise valid message.
        return ()


def _address(namespace, value, *, role, source_field):
    return AddressDTO(
        namespace=namespace,
        value=value,
        value_normalized=value,
        role=role,
        source_field=source_field,
        confidence="protocol",
        resolution_scope="account",
    )


def _message_payload(item):
    present_carriers = sorted(key for key in _EVENT_CARRIERS if key in item)
    message = item.get("message")
    if not isinstance(message, dict):
        event_kind = present_carriers[0] if present_carriers else "unknown"
        raise UnsupportedEventError(
            "Meta messaging event is not implemented: %s" % event_kind
        )
    if present_carriers:
        raise UnsupportedEventError(
            "Meta compound messaging events are not implemented"
        )
    return message


def _unsupported_content_reason(message):
    if message.get("is_unsupported") is True:
        return "provider_unsupported"
    if message.get("is_self") is True:
        return "self_message"
    return ""


def _raise_unsupported_content(reason):
    messages = {
        "provider_unsupported": "Meta marked this message as unsupported",
        "self_message": "Meta self messages require a dedicated contract",
    }
    raise UnsupportedEventError(messages[reason])


def _endpoint_values(connection, route, item, *, is_echo):
    account = connection.account_id
    platform = account.platform
    contract = _PLATFORM_CONTRACTS.get(platform)
    if not contract:
        raise AdapterError("Meta connection has an unsupported platform")
    if (
        route["object"] != contract["object"]
        or route["platform"] != platform
        or route["transport_mode"] != contract["transport_mode"]
        or connection.meta_transport_mode != contract["transport_mode"]
        or route["asset_id"] != connection.meta_target_asset_id
    ):
        raise AdapterError("Meta event route does not match its provider connection")

    sender = _required_mapping(item.get("sender"), "messaging.sender")
    recipient = _required_mapping(item.get("recipient"), "messaging.recipient")
    sender_id = _required_id(sender.get("id"), "messaging.sender.id")
    recipient_id = _required_id(recipient.get("id"), "messaging.recipient.id")
    asset_id = connection.meta_target_asset_id
    if is_echo:
        if sender_id != asset_id or recipient_id == asset_id:
            raise AdapterError("Meta echo endpoints contradict message.is_echo")
        remote_id = recipient_id
        remote_source = "messaging.recipient.id"
    else:
        if recipient_id != asset_id or sender_id == asset_id:
            raise AdapterError(
                "Meta inbound endpoints contradict the destination asset"
            )
        remote_id = sender_id
        remote_source = "messaging.sender.id"
    return contract, is_echo, asset_id, remote_id, remote_source


def _is_from_me_endpoint(connection, item):
    sender = _required_mapping(item.get("sender"), "messaging.sender")
    sender_id = _required_id(sender.get("id"), "messaging.sender.id")
    return sender_id == connection.meta_target_asset_id


def _event_id(prefix, envelope, route):
    digest = atomic_dedupe_key(envelope, route).rsplit(":", 1)[-1]
    return "%s:%s" % (prefix, digest)


def _provider_extension(connection, route, event_kind, **values):
    extension = {
        "graph_api_version": connection.meta_api_app_id.graph_version,
        "object": route["object"],
        "transport_mode": route["transport_mode"],
        "event_kind": event_kind,
    }
    extension.update(values)
    return {"provider.meta": extension}


def _event_values(
    connection,
    route,
    item,
    *,
    is_from_me,
    event_id,
    event_type,
    event_kind,
    direction=None,
    origin=None,
):
    contract, _is_echo, asset_id, remote_id, remote_source = _endpoint_values(
        connection,
        route,
        item,
        is_echo=is_from_me,
    )
    remote_address = _address(
        contract["remote_namespace"],
        remote_id,
        role="primary",
        source_field=remote_source,
    )
    actor_address = _address(
        contract["asset_namespace"] if is_from_me else contract["remote_namespace"],
        asset_id if is_from_me else remote_id,
        role="sender",
        source_field="messaging.sender.id",
    )
    return {
        "provider_schema_version": META_PROVIDER_SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": _occurred_at(item.get("timestamp")),
        "account_ref": connection.account_id.external_ref,
        "connection_ref": connection.external_ref,
        "conversation_ref": remote_id,
        "platform": connection.account_id.platform,
        "direction": direction or ("outbound" if is_from_me else "inbound"),
        "is_from_me": is_from_me,
        "origin": origin or ("external_device" if is_from_me else "provider"),
        "actor": ActorDTO(addresses=(actor_address,)),
        "conversation": ConversationDTO(
            addresses=(remote_address,),
            conversation_type="direct",
        ),
        "extensions": _provider_extension(
            connection,
            route,
            event_kind,
        ),
    }


def _normalize_referral_event(connection, envelope, route, entry, item):
    """Normalize a standalone m.me/ig.me/ad referral without inventing a message."""

    compound_carriers = sorted(
        carrier
        for carrier in _EVENT_CARRIERS
        if carrier != "referral" and carrier in item
    )
    if "message" in item or compound_carriers:
        raise UnsupportedEventError("Meta compound referral events are not implemented")
    _required_mapping(item.get("referral"), "messaging.referral")
    contract, _is_echo, _asset_id, remote_id, remote_source = _endpoint_values(
        connection,
        route,
        item,
        is_echo=False,
    )
    attribution = _attribution_values(
        item,
        connection.account_id.platform,
        False,
        "messaging.referral",
    )
    if not attribution:
        raise UnsupportedEventError(
            "Meta standalone referral contains no bounded attribution evidence"
        )
    occurred_at = _occurred_at(item.get("timestamp"))
    remote_address = _address(
        contract["remote_namespace"],
        remote_id,
        role="primary",
        source_field=remote_source,
    )
    actor_address = _address(
        contract["remote_namespace"],
        remote_id,
        role="sender",
        source_field="messaging.sender.id",
    )
    return EventDTO(
        provider_schema_version=META_PROVIDER_SCHEMA_VERSION,
        event_id="Referral:%s" % atomic_dedupe_key(envelope, route).rsplit(":", 1)[-1],
        event_type="attribution.observed",
        occurred_at=occurred_at,
        account_ref=connection.account_id.external_ref,
        connection_ref=connection.external_ref,
        conversation_ref=remote_id,
        platform=connection.account_id.platform,
        direction="inbound",
        is_from_me=False,
        origin="provider",
        actor=ActorDTO(addresses=(actor_address,)),
        conversation=ConversationDTO(
            addresses=(remote_address,),
            conversation_type="direct",
        ),
        attribution=attribution,
        extensions={
            "provider.meta": {
                "graph_api_version": connection.meta_api_app_id.graph_version,
                "object": route["object"],
                "transport_mode": route["transport_mode"],
                "event_kind": "referral",
            }
        },
    )


def _reply_values(message):
    reply = message.get("reply_to")
    if reply is None:
        reply = {}
    elif not isinstance(reply, dict):
        raise AdapterError("Meta message.reply_to must be an object")

    external_message_id = ""
    if reply.get("mid") is not None:
        external_message_id = _required_id(reply.get("mid"), "message.reply_to.mid")
    normalized_reply = (
        {"external_message_id": external_message_id} if external_message_id else {}
    )

    story_snapshot = {}
    if "story" in reply:
        story = _required_mapping(reply.get("story"), "message.reply_to.story")
        if story.get("id") is not None:
            story_snapshot["id"] = _required_id(
                story.get("id"), "message.reply_to.story.id"
            )
        locator_ref = story.get("private_locator_ref")
        if locator_ref is not None:
            if not isinstance(
                locator_ref, str
            ) or not _PRIVATE_LOCATOR_PATTERN.fullmatch(locator_ref):
                raise AdapterError(
                    "Meta message.reply_to.story private locator is invalid"
                )
            story_snapshot["private_locator_ref"] = locator_ref
        if not story_snapshot:
            raise UnsupportedEventError(
                "Meta story reply has no bounded story reference"
            )
    return external_message_id, normalized_reply, story_snapshot


def _private_locator_ref(payload, field_name):
    locator_ref = payload.get("private_locator_ref")
    if not isinstance(locator_ref, str) or not _PRIVATE_LOCATOR_PATTERN.fullmatch(
        locator_ref
    ):
        raise UnsupportedEventError(
            "Meta %s has no provider-private media locator" % field_name
        )
    return locator_ref


def _safe_file_name(value, field_name):
    value = _bounded_text(value, field_name, 255)
    if not value:
        return ""
    value = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return "" if value in (".", "..") else value


def _attachment_rows(message):
    attachments = message.get("attachments")
    if (
        isinstance(attachments, list)
        and not attachments
        and _sanitizer_rejected_attachments(message)
    ):
        raise UnsupportedEventError(
            "Meta message attachments were rejected during sanitization"
        )
    if not isinstance(attachments, list) or not attachments or len(attachments) > 10:
        raise AdapterError("Meta message.attachments must be a bounded non-empty array")
    rows = []
    for index, attachment in enumerate(attachments):
        attachment = _required_mapping(attachment, "message.attachments[%s]" % index)
        rows.append((index, attachment, _canonical_token(attachment.get("type"))))
    return tuple(rows)


def _media_descriptors(attachment_rows, external_message_id):
    descriptors = []
    seen = set()
    for index, attachment, provider_kind in attachment_rows:
        kind = _MEDIA_KINDS.get(provider_kind)
        if not kind:
            raise UnsupportedEventError(
                "Meta media attachment type is not implemented: %s"
                % (provider_kind or "missing")
            )
        payload = attachment.get("payload")
        if not isinstance(payload, dict):
            raise UnsupportedEventError(
                "Meta message.attachments[%s] has no provider-private media locator"
                % index
            )
        locator_ref = _private_locator_ref(payload, "message.attachments[%s]" % index)
        dedupe_keys = {("locator", kind, locator_ref)}
        sticker_id = payload.get("sticker_id")
        if provider_kind in ("image", "sticker") and sticker_id is not None:
            dedupe_keys.add(
                ("sticker", _required_id(sticker_id, "attachment.payload.sticker_id"))
            )
        if seen.intersection(dedupe_keys):
            continue
        seen.update(dedupe_keys)
        descriptors.append(
            MediaDTO(
                kind=kind,
                external_media_id="%s:%s" % (external_message_id, index),
                remote_locator={"private_locator_ref": locator_ref},
                file_name=_safe_file_name(
                    attachment.get("name"),
                    "message.attachments[%s].name" % index,
                ),
            )
        )
    return tuple(descriptors)


def _message_content_values(message, *, platform, external_message_id, story_snapshot):
    text = _bounded_text(message.get("text"), "message.text", 20_000)
    has_media = "attachments" in message
    has_quick_reply = "quick_reply" in message
    has_story_reply = bool(story_snapshot)
    if sum((has_media, has_quick_reply, has_story_reply)) > 1:
        raise UnsupportedEventError("Meta compound message content is not implemented")

    if has_media:
        attachment_rows = _attachment_rows(message)
        provider_kinds = {row[2] for row in attachment_rows}
        if provider_kinds.issubset(_SOCIAL_ATTACHMENT_KINDS):
            # Meta is transitioning Instagram post attachments from ``share``
            # to ``ig_post``. Both provider shapes represent one neutral social
            # content interaction; signed URLs stay solely in the private vault.
            content_kinds = {_SOCIAL_ATTACHMENT_KINDS[kind] for kind in provider_kinds}
            if len(content_kinds) != 1:
                raise UnsupportedEventError(
                    "Meta compound shared-content attachments are not implemented"
                )
            content_kind = next(iter(content_kinds))
            return {
                "text": text
                or _SOCIAL_ATTACHMENT_LABELS.get(platform, {}).get(
                    content_kind, "Social content shared"
                ),
                "content_type": "interactive",
                "media": (),
                "snapshot": {
                    "interaction": {
                        "type": "shared_content",
                        "content_kind": content_kind,
                    }
                },
            }
        if provider_kinds.intersection(_SOCIAL_ATTACHMENT_KINDS):
            raise UnsupportedEventError(
                "Meta compound shared-content attachments are not implemented"
            )
        media = _media_descriptors(attachment_rows, external_message_id)
        kinds = {item.kind for item in media}
        return {
            "text": text,
            "content_type": next(iter(kinds)) if len(kinds) == 1 else "media",
            "media": media,
            "snapshot": {},
        }
    if has_quick_reply:
        quick_reply = _required_mapping(
            message.get("quick_reply"), "message.quick_reply"
        )
        payload = _bounded_text(
            quick_reply.get("payload"),
            "message.quick_reply.payload",
            2048,
            required=True,
        )
        if not text.strip():
            raise UnsupportedEventError("Meta quick reply has no visible text")
        return {
            "text": text,
            "content_type": "text",
            "media": (),
            "snapshot": {"interaction": {"type": "quick_reply", "payload": payload}},
        }
    if has_story_reply:
        if platform != "instagram":
            raise UnsupportedEventError(
                "Meta story replies are implemented for Instagram only"
            )
        if not text.strip():
            raise UnsupportedEventError("Meta story reply has no visible text")
        return {
            "text": text,
            "content_type": "text",
            "media": (),
            "snapshot": {"story_reply": story_snapshot},
        }
    if not text.strip():
        raise UnsupportedEventError("Meta message has no supported human content")
    return {"text": text, "content_type": "text", "media": (), "snapshot": {}}


def _sanitizer_rejected_content(message):
    rejections = message.get("sanitization_rejections")
    if not isinstance(rejections, list):
        return False
    return any(
        isinstance(item, dict) and item.get("reason") in _UNSAFE_CONTENT_REJECTIONS
        for item in rejections
    )


def _sanitizer_rejected_attachments(message):
    """Return whether the sanitizer rejected this message's attachment slot."""

    rejections = message.get("sanitization_rejections")
    if not isinstance(rejections, list):
        return False
    return any(
        isinstance(item, dict)
        and (
            item.get("slot") == "attachments"
            or (
                isinstance(item.get("slot"), str)
                and item["slot"].startswith("attachment:")
            )
        )
        and item.get("reason") in _UNSAFE_CONTENT_REJECTIONS
        for item in rejections
    )


def _sanitizer_rejected_semantics(message):
    """Reject one item whose direction/lifecycle flags were not trustworthy."""

    rejections = message.get("sanitization_rejections")
    if not isinstance(rejections, list):
        return False
    return any(
        isinstance(item, dict) and item.get("reason") in _CRITICAL_MESSAGE_REJECTIONS
        for item in rejections
    )


def _normalize_deleted_message(
    connection, envelope, route, item, message, external_message_id, is_from_me
):
    if connection.account_id.platform != "instagram":
        raise UnsupportedEventError(
            "Meta message.is_deleted is implemented for Instagram only"
        )
    if any(key in message for key in ("attachments", "quick_reply", "referral")) or (
        isinstance(message.get("text"), str) and message["text"].strip()
    ):
        raise UnsupportedEventError(
            "Meta compound deleted messages are not implemented"
        )
    values = _event_values(
        connection,
        route,
        item,
        is_from_me=is_from_me,
        event_id=_event_id("MessageDelete", envelope, route),
        event_type="message.deleted",
        event_kind="message_deleted",
    )
    values["mutation"] = {
        "type": "delete",
        "target_external_message_id": external_message_id,
        "operation": "delete",
    }
    return EventDTO(**values)


def _normalize_message_event(connection, envelope, route, item):
    message = _message_payload(item)
    if _sanitizer_rejected_semantics(message):
        raise UnsupportedEventError(
            "Meta message semantic flags could not be authenticated"
        )
    is_from_me = message.get("is_echo") is True
    external_message_id = _required_id(message.get("mid"), "message.mid")
    reply_external_id, reply_to, story_snapshot = _reply_values(message)
    if reply_external_id == external_message_id:
        raise AdapterError("Meta message cannot reply to itself")
    if message.get("is_deleted") is True:
        return _normalize_deleted_message(
            connection,
            envelope,
            route,
            item,
            message,
            external_message_id,
            is_from_me,
        )

    attribution = _attribution_values(
        message,
        connection.account_id.platform,
        is_from_me,
        "messaging.message.referral",
    )
    unsupported_reason = _unsupported_content_reason(message)
    content = None
    if unsupported_reason:
        fallback_text = _bounded_text(message.get("text"), "message.text", 20_000)
        if unsupported_reason == "self_message":
            # ``is_self`` is not equivalent to the documented echo contract.
            # Projecting its text as inbound would attribute our own message to a
            # remote guest.  Attribution-only evidence may still be retained.
            if not attribution:
                _raise_unsupported_content(unsupported_reason)
        elif fallback_text.strip():
            content = {
                "text": fallback_text,
                "content_type": "text",
                "media": (),
                "snapshot": {},
            }
        elif not attribution:
            _raise_unsupported_content(unsupported_reason)
    else:
        fallback_text = _bounded_text(message.get("text"), "message.text", 20_000)
        if fallback_text.strip() and _sanitizer_rejected_content(message):
            content = {
                "text": fallback_text,
                "content_type": "text",
                "media": (),
                "snapshot": {},
            }
            unsupported_reason = "media" if "attachments" in message else "content"
        else:
            try:
                content = _message_content_values(
                    message,
                    platform=connection.account_id.platform,
                    external_message_id=external_message_id,
                    story_snapshot=story_snapshot,
                )
            except UnsupportedEventError:
                fallback_text = _bounded_text(
                    message.get("text"), "message.text", 20_000
                )
                if fallback_text.strip():
                    content = {
                        "text": fallback_text,
                        "content_type": "text",
                        "media": (),
                        "snapshot": {},
                    }
                elif not attribution:
                    raise
                unsupported_reason = "media" if "attachments" in message else "content"

    values = _event_values(
        connection,
        route,
        item,
        is_from_me=is_from_me,
        event_id="Message:%s" % external_message_id,
        event_type="message.created",
        event_kind="message",
    )
    occurred_at_text = values["occurred_at"].isoformat().replace("+00:00", "Z")
    snapshot = {
        "provider": "meta",
        "platform": connection.account_id.platform,
        "message_id": external_message_id,
        "timestamp": occurred_at_text,
        "object": route["object"],
        "asset_id": connection.meta_target_asset_id,
        "remote_id": values["conversation_ref"],
        "is_echo": is_from_me,
        "reply_to": dict(reply_to),
    }
    if content:
        snapshot.update(content["snapshot"])
    extension = values["extensions"]["provider.meta"]
    extension["message_kind"] = "echo" if is_from_me else "message"
    extension.pop("event_kind", None)
    if unsupported_reason:
        extension["unsupported_content"] = unsupported_reason
    values.update(
        {
            "message": MessageDTO(
                external_message_id=external_message_id,
                content_type=(content or {}).get("content_type", "unsupported"),
                text=(content or {}).get("text", ""),
                reply_to_external_id=reply_external_id,
                protocol_snapshot=snapshot,
                media=(content or {}).get("media", ()),
            ),
            "reply_to": reply_to,
            "attribution": attribution,
        }
    )
    return EventDTO(**values)


def _normalize_postback(connection, envelope, route, item):
    postback = _required_mapping(item.get("postback"), "messaging.postback")
    external_message_id = _required_id(postback.get("mid"), "postback.mid")
    title = _bounded_text(postback.get("title"), "postback.title", 1024)
    payload = _bounded_text(postback.get("payload"), "postback.payload", 4096)
    text = title if title.strip() else payload
    if not text.strip():
        raise UnsupportedEventError("Meta postback has no visible title or payload")
    values = _event_values(
        connection,
        route,
        item,
        is_from_me=False,
        event_id="Message:%s" % external_message_id,
        event_type="message.created",
        event_kind="postback",
    )
    values.update(
        {
            "message": MessageDTO(
                external_message_id=external_message_id,
                content_type="interactive",
                text=text,
                protocol_snapshot={
                    "provider": "meta",
                    "platform": connection.account_id.platform,
                    "message_id": external_message_id,
                    "timestamp": values["occurred_at"]
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "interaction": {
                        "type": "postback",
                        "title": title,
                        "payload": payload,
                    },
                },
            ),
            "attribution": _attribution_values(
                postback,
                connection.account_id.platform,
                False,
                "messaging.postback.referral",
            ),
        }
    )
    return EventDTO(**values)


def _normalize_reaction(connection, envelope, route, item):
    reaction = _required_mapping(item.get("reaction"), "messaging.reaction")
    target_id = _required_id(reaction.get("mid"), "reaction.mid")
    action = _canonical_token(reaction.get("action"))
    if action not in ("react", "unreact"):
        raise UnsupportedEventError(
            "Meta reaction action is not implemented: %s" % (action or "missing")
        )
    emoji = _bounded_text(reaction.get("emoji"), "reaction.emoji", 64)
    if action == "react" and not emoji:
        raise UnsupportedEventError("Meta added reaction has no emoji")
    is_from_me = _is_from_me_endpoint(connection, item)
    values = _event_values(
        connection,
        route,
        item,
        is_from_me=is_from_me,
        event_id=_event_id("MessageReaction", envelope, route),
        event_type="message.reaction",
        event_kind="reaction",
    )
    mutation = {
        "type": "react",
        "target_external_message_id": target_id,
        "operation": "add" if action == "react" else "remove",
        "emoji": emoji,
    }
    provider_reaction = _bounded_text(reaction.get("reaction"), "reaction.reaction", 64)
    if provider_reaction:
        mutation["provider_reaction"] = provider_reaction
    values["mutation"] = mutation
    return EventDTO(**values)


def _normalize_message_edit(connection, envelope, route, item):
    edit = _required_mapping(item.get("message_edit"), "messaging.message_edit")
    target_id = _required_id(edit.get("mid"), "message_edit.mid")
    text = _bounded_text(edit.get("text"), "message_edit.text", 20_000, required=True)
    revision = edit.get("num_edit")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise AdapterError("Meta message_edit.num_edit must be a positive integer")
    is_from_me = _is_from_me_endpoint(connection, item)
    values = _event_values(
        connection,
        route,
        item,
        is_from_me=is_from_me,
        event_id=_event_id("MessageEdit", envelope, route),
        event_type="message.updated",
        event_kind="message_edit",
    )
    values["mutation"] = {
        "type": "edit",
        "target_external_message_id": target_id,
        "operation": "replace",
        "new_text": text,
        "provider_revision": revision,
    }
    return EventDTO(**values)


def _positive_watermark(value, field_name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AdapterError("Meta %s must be a positive integer" % field_name)
    return value


def _normalize_delivery(connection, envelope, route, item):
    if connection.account_id.platform != "messenger":
        raise UnsupportedEventError(
            "Meta delivery receipts are implemented for Messenger only"
        )
    delivery = _required_mapping(item.get("delivery"), "messaging.delivery")
    raw_ids = delivery.get("mids")
    if raw_ids is None:
        message_ids = []
    elif isinstance(raw_ids, list):
        message_ids = list(
            dict.fromkeys(_required_id(value, "delivery.mids") for value in raw_ids)
        )
    else:
        raise AdapterError("Meta delivery.mids must be an array")
    watermark = None
    if delivery.get("watermark") is not None:
        watermark = _positive_watermark(delivery.get("watermark"), "delivery.watermark")
    if not message_ids and watermark is None:
        raise AdapterError("Meta delivery requires mids or a watermark")
    values = _event_values(
        connection,
        route,
        item,
        is_from_me=False,
        event_id=_event_id("MessageDelivery", envelope, route),
        event_type="delivery.updated",
        event_kind="delivery",
        direction="outbound",
        origin="provider",
    )
    normalized = {
        "state": "delivered",
        "external_message_ids": message_ids,
        "external_event_id": values["event_id"],
    }
    if watermark is not None:
        normalized["watermark"] = watermark
    values["delivery"] = normalized
    return EventDTO(**values)


def _normalize_read(connection, envelope, route, item):
    read = _required_mapping(item.get("read"), "messaging.read")
    platform = connection.account_id.platform
    message_ids = []
    normalized = {"state": "read"}
    if platform == "messenger":
        if read.get("mid") is not None:
            raise UnsupportedEventError("Messenger read receipts use a watermark")
        normalized["watermark"] = _positive_watermark(
            read.get("watermark"), "read.watermark"
        )
    elif platform == "instagram":
        message_ids = [_required_id(read.get("mid"), "read.mid")]
        if read.get("watermark") is not None:
            normalized["watermark"] = _positive_watermark(
                read.get("watermark"), "read.watermark"
            )
    else:
        raise UnsupportedEventError("Meta read receipt platform is not implemented")
    values = _event_values(
        connection,
        route,
        item,
        is_from_me=False,
        event_id=_event_id("MessageRead", envelope, route),
        event_type="delivery.updated",
        event_kind="read",
        direction="outbound",
        origin="provider",
    )
    normalized.update(
        {
            "external_message_ids": message_ids,
            "external_event_id": values["event_id"],
        }
    )
    values["delivery"] = normalized
    return EventDTO(**values)


def _normalize_control_event(connection, envelope, route, item):
    carriers = sorted(key for key in _EVENT_CARRIERS if key in item)
    if len(carriers) != 1:
        raise UnsupportedEventError(
            "Meta control event must contain exactly one supported carrier"
        )
    handlers = {
        "postback": _normalize_postback,
        "reaction": _normalize_reaction,
        "delivery": _normalize_delivery,
        "read": _normalize_read,
        "message_edit": _normalize_message_edit,
    }
    handler = handlers.get(carriers[0])
    if not handler:
        raise UnsupportedEventError(
            "Meta control event is not implemented: %s" % carriers[0]
        )
    return handler(connection, envelope, route, item)


def normalize_meta_event(connection, envelope):
    """Translate one sanitized atomic Messenger/Instagram event to EventDTO."""

    if not isinstance(envelope, dict):
        raise AdapterError("Meta webhook event must be an object")
    route = routing_values(envelope)
    if not route:
        raise AdapterError("Meta webhook event has no valid asset route")
    entry = _required_mapping(envelope.get("entry"), "entry")
    item = _required_mapping(envelope.get("messaging"), "messaging")
    entry_id = _required_id(entry.get("id"), "entry.id")
    if entry_id != route["asset_id"]:
        raise AdapterError("Meta entry asset does not match the routed asset")

    if "referral" in item and "message" not in item:
        return _normalize_referral_event(connection, envelope, route, entry, item)
    if "message" in item:
        return _normalize_message_event(connection, envelope, route, item)
    return _normalize_control_event(connection, envelope, route, item)
