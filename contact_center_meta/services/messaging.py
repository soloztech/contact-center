import hashlib
import json
import re
from urllib.parse import urlsplit

from odoo.addons.meta_webhook_base.services.sanitizer import MAX_WEBHOOK_ENTRIES

from .contracts import MAX_MESSAGE_ATTACHMENTS, MAX_MESSAGING_ITEMS_PER_ENTRY

_PRIVATE_LOCATOR_REFERENCE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_OBJECTS = {
    "page": ("messenger", "messenger_page"),
    "instagram": ("instagram", "instagram_page_linked"),
}


def _bounded_text(value, field_name, maximum, *, required=False):
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % field_name)
    if required and not value:
        raise ValueError("%s must not be empty" % field_name)
    if len(value) > maximum:
        raise ValueError("%s is too long" % field_name)
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        raise ValueError("%s contains control characters" % field_name)
    return value


def _bounded_id(value, field_name, *, required=False):
    value = _bounded_text(value, field_name, 256, required=required)
    if value and any(character.isspace() for character in value):
        raise ValueError("%s contains whitespace" % field_name)
    return value


def _bounded_integer(value, field_name):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an integer" % field_name)
    if value < 0 or value > 9_999_999_999_999:
        raise ValueError("%s is out of range" % field_name)
    return value


def _copy_optional_id(source, target, key, field_name=None):
    value = source.get(key)
    if value is not None:
        target[key] = _bounded_id(value, field_name or key)


def _sanitize_party(value, field_name):
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % field_name)
    result = {}
    _copy_optional_id(value, result, "id", "%s.id" % field_name)
    return result


def _sanitize_referral(value, field_name):
    """Keep valid optional attribution fields without rejecting the message."""

    if not isinstance(value, dict):
        return {}
    result = {}
    for key, maximum in (
        ("ref", 2048),
        ("source", 128),
        ("type", 128),
        ("ad_id", 256),
    ):
        if value.get(key) is not None:
            try:
                sanitized = _bounded_text(
                    value[key], "%s.%s" % (field_name, key), maximum
                ).strip()
            except ValueError:
                continue
            if not sanitized:
                continue
            if key in ("ref", "ad_id") and any(
                ord(character) < 32 or ord(character) == 127 for character in sanitized
            ):
                continue
            result[key] = sanitized
    return result


def _private_https_url(value, field_name):
    value = _bounded_text(value, field_name, 8192, required=True)
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("%s has an invalid port" % field_name) from error
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise ValueError("%s must be a credential-free HTTPS URL" % field_name)
    return value


def _append_locator_rejection(rejections, sequence, slot, reason):
    """Record a bounded, secret-free rejection for one private media slot."""

    if rejections is not None:
        rejections.append(
            {
                "sequence": sequence,
                "slot": slot,
                "reason": reason,
            }
        )


def _private_locator_reference(seed, sequence, slot):
    if not _PRIVATE_LOCATOR_REFERENCE_PATTERN.fullmatch(seed or ""):
        raise ValueError("private locator seed must be a SHA-256 digest")
    return hashlib.sha256(
        ("meta-private-locator:%s:%s:%s" % (seed, sequence, slot)).encode("utf-8")
    ).hexdigest()


def _private_locator_candidate(seed, sequence, slot, media_type, url):
    return {
        "reference": _private_locator_reference(seed, sequence, slot),
        "sequence": sequence,
        "slot": slot,
        "media_type": str(media_type or "")[:64],
        "download_url": url,
        "url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
    }


def _attachment_locator_candidates(
    message,
    seed,
    sequence,
    entry_index,
    item_index,
    rejections,
):
    attachments = message.get("attachments")
    if attachments is None:
        return ()
    if not isinstance(attachments, list):
        _append_locator_rejection(
            rejections, sequence, "attachments", "invalid_attachment_array"
        )
        return ()
    if len(attachments) > MAX_MESSAGE_ATTACHMENTS:
        _append_locator_rejection(
            rejections, sequence, "attachments", "too_many_attachments"
        )
        return ()
    result = []
    for attachment_index, attachment in enumerate(attachments):
        slot = "attachment:%s" % attachment_index
        if not isinstance(attachment, dict):
            _append_locator_rejection(rejections, sequence, slot, "invalid_attachment")
            continue
        payload = attachment.get("payload")
        if not isinstance(payload, dict) or payload.get("url") is None:
            continue
        try:
            url = _private_https_url(
                payload["url"],
                "entry[%s].messaging[%s].message.attachments[%s].payload.url"
                % (entry_index, item_index, attachment_index),
            )
        except ValueError:
            _append_locator_rejection(rejections, sequence, slot, "invalid_https_url")
            continue
        result.append(
            _private_locator_candidate(
                seed, sequence, slot, attachment.get("type"), url
            )
        )
    return tuple(result)


def _story_locator_candidate(
    message,
    seed,
    sequence,
    entry_index,
    item_index,
    rejections,
):
    reply_to = message.get("reply_to")
    story = reply_to.get("story") if isinstance(reply_to, dict) else None
    if not isinstance(story, dict) or story.get("url") is None:
        return None
    try:
        url = _private_https_url(
            story["url"],
            "entry[%s].messaging[%s].message.reply_to.story.url"
            % (entry_index, item_index),
        )
    except ValueError:
        _append_locator_rejection(rejections, sequence, "story", "invalid_https_url")
        return None
    return _private_locator_candidate(seed, sequence, "story", "story", url)


def private_media_locators(envelope, seed, *, rejections=None):
    """Extract signed media URLs before sanitizing the immutable webhook ledger.

    The returned rows are provider-private and must be persisted only in the Meta
    locator vault.  Their deterministic opaque references are the sole values that
    may cross into the sanitized envelope and provider-neutral DTO.
    """

    if not isinstance(envelope, dict):
        raise ValueError("webhook envelope must be an object")
    entries = envelope.get("entry")
    if not isinstance(entries, list) or len(entries) > MAX_WEBHOOK_ENTRIES:
        raise ValueError("entry must be a bounded array")
    result = []
    for entry_index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError("entry[%s] must be an object" % entry_index)
        messaging = entry.get("messaging", [])
        if (
            not isinstance(messaging, list)
            or len(messaging) > MAX_MESSAGING_ITEMS_PER_ENTRY
        ):
            raise ValueError(
                "entry[%s].messaging must be a bounded array" % entry_index
            )
        for item_index, item in enumerate(messaging):
            if not isinstance(item, dict):
                _append_locator_rejection(
                    rejections,
                    entry_index * MAX_MESSAGING_ITEMS_PER_ENTRY + item_index,
                    "message",
                    "invalid_messaging_item",
                )
                continue
            message = item.get("message")
            if not isinstance(message, dict):
                continue
            sequence = entry_index * MAX_MESSAGING_ITEMS_PER_ENTRY + item_index
            result.extend(
                _attachment_locator_candidates(
                    message,
                    seed,
                    sequence,
                    entry_index,
                    item_index,
                    rejections,
                )
            )
            story = _story_locator_candidate(
                message,
                seed,
                sequence,
                entry_index,
                item_index,
                rejections,
            )
            if story:
                result.append(story)
    return tuple(result)


def _locator_reference(locator_references, sequence, slot):
    reference = (locator_references or {}).get((sequence, slot))
    if reference is None:
        return ""
    if not _PRIVATE_LOCATOR_REFERENCE_PATTERN.fullmatch(reference):
        raise ValueError("private locator reference is invalid")
    return reference


def _sanitization_rejection(slot, reason):
    return {"slot": slot[:64], "reason": reason[:64]}


def _sanitize_attachment_payload(
    payload,
    field_name,
    *,
    sequence,
    slot,
    locator_references,
    rejected_reason,
):
    if not isinstance(payload, dict):
        return {}, []
    result = {}
    rejections = []
    try:
        _copy_optional_id(
            payload,
            result,
            "sticker_id",
            "%s.payload.sticker_id" % field_name,
        )
    except ValueError:
        rejections.append(_sanitization_rejection(slot, "invalid_sticker_id"))
    reference = _locator_reference(locator_references, sequence, slot)
    if reference:
        result["private_locator_ref"] = reference
    if rejected_reason:
        result["locator_rejected"] = rejected_reason
        rejections.append(_sanitization_rejection(slot, rejected_reason))
    return result, rejections


def _sanitize_attachment(
    attachment,
    field_name,
    *,
    sequence,
    slot,
    locator_references,
    rejected_reason,
):
    if not isinstance(attachment, dict):
        return {}, [_sanitization_rejection(slot, "invalid_attachment")]
    result = {}
    rejections = []
    for key, maximum, reason in (
        ("type", 64, "invalid_attachment_type"),
        ("name", 255, "invalid_attachment_name"),
    ):
        if attachment.get(key) is None:
            continue
        try:
            result[key] = _bounded_text(
                attachment[key], "%s.%s" % (field_name, key), maximum
            )
        except ValueError:
            rejections.append(_sanitization_rejection(slot, reason))
    payload, payload_rejections = _sanitize_attachment_payload(
        attachment.get("payload"),
        field_name,
        sequence=sequence,
        slot=slot,
        locator_references=locator_references,
        rejected_reason=rejected_reason,
    )
    if payload:
        result["payload"] = payload
    if result.get("type") in {"share", "ig_post", "ig_reel", "reel", "story_mention"}:
        raw_payload = attachment.get("payload")
        if isinstance(raw_payload, dict):
            social_payload = result.setdefault("payload", {})
            permalink = _public_social_url(raw_payload.get("url"))
            if permalink:
                social_payload["public_permalink"] = permalink
            media_kind = _social_media_kind(raw_payload.get("url"))
            if media_kind and social_payload.get("private_locator_ref"):
                social_payload["media_kind"] = media_kind
            title = raw_payload.get("title")
            if isinstance(title, str) and not any(
                ord(character) < 32 or ord(character) == 127 for character in title
            ):
                social_payload["title"] = title[:200]
    rejections.extend(payload_rejections)
    return result, rejections


def _sanitize_attachments(
    value,
    field_name,
    *,
    sequence,
    locator_references,
    locator_rejections=None,
):
    if not isinstance(value, list):
        return [], (_sanitization_rejection("attachments", "invalid_attachment_array"),)
    if len(value) > MAX_MESSAGE_ATTACHMENTS:
        return [], (_sanitization_rejection("attachments", "too_many_attachments"),)
    rejected_by_slot = {
        item["slot"]: item["reason"]
        for item in (locator_rejections or ())
        if item.get("sequence") == sequence
    }
    result = []
    rejections = []
    for index, attachment in enumerate(value):
        slot = "attachment:%s" % index
        sanitized, item_rejections = _sanitize_attachment(
            attachment,
            "%s[%s]" % (field_name, index),
            sequence=sequence,
            slot=slot,
            locator_references=locator_references,
            rejected_reason=rejected_by_slot.get(slot),
        )
        # Locator slots refer to the original provider array. Keep an empty
        # placeholder for rejected entries so later valid media keep their slot.
        result.append(sanitized)
        rejections.extend(item_rejections)
    return result, tuple(rejections)


def _sanitize_message_flags(value, result):
    rejections = []
    for key in ("is_echo", "is_deleted", "is_unsupported", "is_self"):
        if value.get(key) is None:
            continue
        if isinstance(value[key], bool):
            result[key] = value[key]
        else:
            rejections.append(_sanitization_rejection("message", "invalid_%s" % key))
    return rejections


def _story_locator_rejection(locator_rejections, sequence):
    return next(
        (
            item["reason"]
            for item in (locator_rejections or ())
            if item.get("sequence") == sequence and item.get("slot") == "story"
        ),
        "",
    )


def _sanitize_story(
    story,
    field_name,
    *,
    sequence,
    locator_references,
    locator_rejections,
):
    if not isinstance(story, dict):
        return {}, [_sanitization_rejection("story", "invalid_story")]
    result = {}
    rejections = []
    try:
        _copy_optional_id(story, result, "id", "%s.id" % field_name)
    except ValueError:
        rejections.append(_sanitization_rejection("story", "invalid_story_id"))
    reference = _locator_reference(locator_references, sequence, "story")
    if reference:
        result["private_locator_ref"] = reference
        media_kind = _social_media_kind(story.get("url"))
        if media_kind:
            result["media_kind"] = media_kind
    rejected_reason = _story_locator_rejection(locator_rejections, sequence)
    if rejected_reason:
        result["locator_rejected"] = rejected_reason
        rejections.append(_sanitization_rejection("story", rejected_reason))
    return result, rejections


def _sanitize_reply_to(
    reply_to,
    field_name,
    *,
    sequence,
    locator_references,
    locator_rejections,
):
    if not isinstance(reply_to, dict):
        return {}, [_sanitization_rejection("reply_to", "invalid_reply")]
    result = {}
    rejections = []
    try:
        _copy_optional_id(reply_to, result, "mid", "%s.mid" % field_name)
    except ValueError:
        rejections.append(_sanitization_rejection("reply_to", "invalid_reply_mid"))
    if reply_to.get("story") is not None:
        story, story_rejections = _sanitize_story(
            reply_to["story"],
            "%s.story" % field_name,
            sequence=sequence,
            locator_references=locator_references,
            locator_rejections=locator_rejections,
        )
        if isinstance(reply_to["story"], dict):
            result["story"] = story
        rejections.extend(story_rejections)
    return result, rejections


def _sanitize_quick_reply(quick_reply, field_name):
    if not isinstance(quick_reply, dict):
        return None
    try:
        return {
            "payload": _bounded_text(
                quick_reply.get("payload") or "",
                "%s.payload" % field_name,
                2048,
            )
        }
    except ValueError:
        return None


def _sanitize_message(
    value,
    field_name,
    *,
    sequence,
    locator_references=None,
    locator_rejections=None,
):
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % field_name)
    result = {}
    _copy_optional_id(value, result, "mid", "%s.mid" % field_name)
    if value.get("text") is not None:
        result["text"] = _bounded_text(value["text"], "%s.text" % field_name, 20_000)
    rejections = _sanitize_message_flags(value, result)
    if value.get("reply_to") is not None:
        reply_to, reply_rejections = _sanitize_reply_to(
            value["reply_to"],
            "%s.reply_to" % field_name,
            sequence=sequence,
            locator_references=locator_references,
            locator_rejections=locator_rejections,
        )
        if reply_to:
            result["reply_to"] = reply_to
        rejections.extend(reply_rejections)
    if value.get("quick_reply") is not None:
        quick_reply = _sanitize_quick_reply(
            value["quick_reply"], "%s.quick_reply" % field_name
        )
        if quick_reply is None:
            rejections.append(
                _sanitization_rejection("quick_reply", "invalid_quick_reply")
            )
        else:
            result["quick_reply"] = quick_reply
    if value.get("attachments") is not None:
        attachments, attachment_rejections = _sanitize_attachments(
            value["attachments"],
            "%s.attachments" % field_name,
            sequence=sequence,
            locator_references=locator_references,
            locator_rejections=locator_rejections,
        )
        result["attachments"] = attachments
        rejections.extend(attachment_rejections)
    if value.get("referral") is not None:
        result["referral"] = _sanitize_referral(
            value["referral"], "%s.referral" % field_name
        )
    if rejections:
        result["sanitization_rejections"] = rejections[:MAX_MESSAGE_ATTACHMENTS]
    return result


def _sanitize_postback(value, field_name):
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % field_name)
    result = {}
    for key, maximum in (("mid", 256), ("title", 1024), ("payload", 4096)):
        if value.get(key) is not None:
            result[key] = _bounded_text(
                value[key], "%s.%s" % (field_name, key), maximum
            )
    if value.get("referral") is not None:
        result["referral"] = _sanitize_referral(
            value["referral"], "%s.referral" % field_name
        )
    return result


def _sanitize_reaction(value, field_name):
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % field_name)
    result = {}
    for key, maximum in (
        ("mid", 256),
        ("action", 64),
        ("reaction", 64),
        ("emoji", 64),
    ):
        if value.get(key) is not None:
            result[key] = _bounded_text(
                value[key], "%s.%s" % (field_name, key), maximum
            )
    return result


def _sanitize_delivery(value, field_name):
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % field_name)
    result = {}
    mids = value.get("mids")
    if mids is not None:
        if not isinstance(mids, list) or len(mids) > 100:
            raise ValueError("%s.mids must be a bounded array" % field_name)
        result["mids"] = [
            _bounded_id(item, "%s.mids" % field_name, required=True) for item in mids
        ]
    watermark = _bounded_integer(value.get("watermark"), "%s.watermark" % field_name)
    if watermark is not None:
        result["watermark"] = watermark
    return result


def _sanitize_read(value, field_name):
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % field_name)
    result = {}
    watermark = _bounded_integer(value.get("watermark"), "%s.watermark" % field_name)
    if watermark is not None:
        result["watermark"] = watermark
    _copy_optional_id(value, result, "mid", "%s.mid" % field_name)
    return result


def _sanitize_message_edit(value, field_name):
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % field_name)
    result = {}
    _copy_optional_id(value, result, "mid", "%s.mid" % field_name)
    if value.get("text") is not None:
        result["text"] = _bounded_text(value["text"], "%s.text" % field_name, 20_000)
    revision = _bounded_integer(value.get("num_edit"), "%s.num_edit" % field_name)
    if revision is not None:
        result["num_edit"] = revision
    return result


def _sanitize_messaging_item(
    value,
    field_name,
    *,
    sequence,
    locator_references=None,
    locator_rejections=None,
):
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % field_name)
    result = {}
    for key in ("sender", "recipient"):
        if value.get(key) is not None:
            result[key] = _sanitize_party(value[key], "%s.%s" % (field_name, key))
    timestamp = _bounded_integer(value.get("timestamp"), "%s.timestamp" % field_name)
    if timestamp is not None:
        result["timestamp"] = timestamp
    if value.get("message") is not None:
        result["message"] = _sanitize_message(
            value["message"],
            "%s.message" % field_name,
            sequence=sequence,
            locator_references=locator_references,
            locator_rejections=locator_rejections,
        )
    handlers = {
        "postback": _sanitize_postback,
        "reaction": _sanitize_reaction,
        "delivery": _sanitize_delivery,
        "read": _sanitize_read,
        "referral": _sanitize_referral,
        "message_edit": _sanitize_message_edit,
    }
    for key, sanitizer in handlers.items():
        if value.get(key) is not None:
            try:
                result[key] = sanitizer(value[key], "%s.%s" % (field_name, key))
            except ValueError:
                result.setdefault("sanitization_rejections", []).append(
                    _sanitization_rejection(key, "invalid_optional_event")
                )
    return result


def _quarantined_messaging_item(value, field_name):
    """Keep one malformed item visible without retaining unbounded provider data."""

    result = {
        "sanitization_rejections": [
            _sanitization_rejection("message", "invalid_messaging_item")
        ]
    }
    if not isinstance(value, dict):
        return result
    for key in ("sender", "recipient"):
        try:
            result[key] = _sanitize_party(value.get(key), "%s.%s" % (field_name, key))
        except ValueError:
            continue
    try:
        timestamp = _bounded_integer(
            value.get("timestamp"), "%s.timestamp" % field_name
        )
    except ValueError:
        timestamp = None
    if timestamp is not None:
        result["timestamp"] = timestamp
    return result


def sanitize_webhook_envelope(
    envelope,
    *,
    private_locator_references=None,
    private_locator_rejections=None,
):
    """Return the bounded allow-listed Meta envelope stored as evidence."""

    if not isinstance(envelope, dict):
        raise ValueError("webhook envelope must be an object")
    object_type = _bounded_text(
        envelope.get("object"), "object", 64, required=True
    ).lower()
    entries = envelope.get("entry")
    if not isinstance(entries, list):
        raise ValueError("entry must be an array")
    if len(entries) > MAX_WEBHOOK_ENTRIES:
        raise ValueError("entry contains too many items")
    sanitized_entries = []
    for entry_index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError("entry[%s] must be an object" % entry_index)
        sanitized_entry = {
            "id": _bounded_id(
                entry.get("id"), "entry[%s].id" % entry_index, required=True
            )
        }
        entry_time = _bounded_integer(entry.get("time"), "entry[%s].time" % entry_index)
        if entry_time is not None:
            sanitized_entry["time"] = entry_time
        messaging = entry.get("messaging", [])
        if not isinstance(messaging, list):
            raise ValueError("entry[%s].messaging must be an array" % entry_index)
        if len(messaging) > MAX_MESSAGING_ITEMS_PER_ENTRY:
            raise ValueError(
                "entry[%s].messaging contains too many items" % entry_index
            )
        sanitized_messages = []
        for item_index, item in enumerate(messaging):
            field_name = "entry[%s].messaging[%s]" % (entry_index, item_index)
            sequence = entry_index * MAX_MESSAGING_ITEMS_PER_ENTRY + item_index
            try:
                sanitized_item = _sanitize_messaging_item(
                    item,
                    field_name,
                    sequence=sequence,
                    locator_references=private_locator_references,
                    locator_rejections=private_locator_rejections,
                )
            except ValueError:
                sanitized_item = _quarantined_messaging_item(
                    item,
                    field_name,
                )
            sanitized_messages.append(sanitized_item)
        sanitized_entry["messaging"] = sanitized_messages
        sanitized_entries.append(sanitized_entry)
    return {"object": object_type, "entry": sanitized_entries}


def atomic_events(envelope):
    """Yield stable provider-private envelopes for each messaging item."""

    object_type = envelope["object"]
    for entry_index, entry in enumerate(envelope["entry"]):
        for item_index, item in enumerate(entry.get("messaging") or []):
            yield {
                "sequence": entry_index * MAX_MESSAGING_ITEMS_PER_ENTRY + item_index,
                "entry_index": entry_index,
                "item_index": item_index,
                "object": object_type,
                "entry": {
                    "id": entry["id"],
                    **({"time": entry["time"]} if "time" in entry else {}),
                },
                "messaging": item,
            }


def routing_values(atomic_event):
    object_type = atomic_event.get("object")
    platform_mode = _SUPPORTED_OBJECTS.get(object_type)
    entry = atomic_event.get("entry") or {}
    item = atomic_event.get("messaging") or {}
    asset_id = entry.get("id")
    if not platform_mode or not isinstance(asset_id, str) or not asset_id:
        return None
    sender = item.get("sender")
    recipient = item.get("recipient")
    if not isinstance(sender, dict) or not isinstance(recipient, dict):
        return None
    sender_id = sender.get("id")
    recipient_id = recipient.get("id")
    if not sender_id or not recipient_id:
        return None
    endpoint_ids = {sender_id, recipient_id}
    # An entry ID alone is insufficient routing evidence. Meta deliveries are
    # app-scoped, so one endpoint must also identify the same Page/Instagram
    # asset. This still accepts inbound events and outbound echoes while an
    # incomplete or unrelated signed item remains visible as unrouted.
    if asset_id not in endpoint_ids:
        return None
    platform, transport_mode = platform_mode
    return {
        "object": object_type,
        "platform": platform,
        "transport_mode": transport_mode,
        "asset_id": asset_id,
    }


def canonical_json_digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _canonical_delivery_for_dedupe(value):
    if not isinstance(value, dict):
        return value
    canonical = dict(value)
    message_ids = canonical.get("mids")
    if isinstance(message_ids, list):
        canonical["mids"] = sorted(set(message_ids))
    return canonical


def atomic_dedupe_key(atomic_event, route):
    item = atomic_event.get("messaging") or {}
    semantic = None
    message = item.get("message")
    if isinstance(message, dict) and message.get("mid"):
        semantic = {
            "kind": "message_deleted" if message.get("is_deleted") else "message",
            "mid": message["mid"],
        }
    postback = item.get("postback")
    if semantic is None and isinstance(postback, dict) and postback.get("mid"):
        semantic = {"kind": "postback", "mid": postback["mid"]}
    message_edit = item.get("message_edit")
    if semantic is None and isinstance(message_edit, dict) and message_edit.get("mid"):
        semantic = {
            "kind": "message_edit",
            "mid": message_edit["mid"],
            "num_edit": message_edit.get("num_edit"),
        }
    referral = item.get("referral")
    if semantic is None and isinstance(referral, dict) and referral:
        semantic = {
            "kind": "referral",
            "sender_id": (item.get("sender") or {}).get("id"),
            "recipient_id": (item.get("recipient") or {}).get("id"),
            "timestamp": item.get("timestamp"),
            "referral": referral,
        }
    if semantic is None:
        semantic = {
            "kind": "event",
            "timestamp": item.get("timestamp"),
            "reaction": item.get("reaction"),
            "delivery": _canonical_delivery_for_dedupe(item.get("delivery")),
            "read": item.get("read"),
            "postback": item.get("postback"),
            "referral": item.get("referral"),
            "message": item.get("message"),
            "message_edit": item.get("message_edit"),
        }
    material = {
        "object": route["object"],
        "platform": route["platform"],
        "asset_id": route["asset_id"],
        "sender_id": (item.get("sender") or {}).get("id"),
        "recipient_id": (item.get("recipient") or {}).get("id"),
        "semantic": semantic,
    }
    return "meta:%s:event:sha256:%s" % (
        route["platform"],
        canonical_json_digest(material),
    )


def _public_social_url(value):
    """Keep only recognizable public Meta permalinks, without provider queries."""
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            return ""
    except ValueError:
        return ""
    path = parsed.path
    if hostname in {"instagram.com", "www.instagram.com"}:
        valid = re.fullmatch(
            r"/(?:p|reel|reels)/[A-Za-z0-9_-]+/?", path
        ) or re.fullmatch(r"/stories/[A-Za-z0-9_.]+/[0-9]+/?", path)
    elif hostname in {"facebook.com", "www.facebook.com", "m.facebook.com"}:
        valid = re.fullmatch(
            r"/(?:reel/[0-9]+|[A-Za-z0-9_.]+/posts/[A-Za-z0-9]+|share/[prv]/[A-Za-z0-9]+)/?",
            path,
        )
    else:
        return ""
    return "https://%s%s" % (hostname, path) if valid else ""


def _social_media_kind(value):
    """Infer only an explicit image/video extension on an allowed private CDN."""
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
    except ValueError:
        return ""
    if not any(
        hostname == suffix or hostname.endswith("." + suffix)
        for suffix in ("fbcdn.net", "fbsbx.com", "cdninstagram.com")
    ):
        return ""
    extension = parsed.path.rsplit(".", 1)[-1].lower()
    if extension in {"jpg", "jpeg", "png", "gif", "webp"}:
        return "image"
    return "video" if extension in {"mp4", "mov", "webm"} else ""
