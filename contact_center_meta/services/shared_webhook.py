import hashlib

from .contracts import (
    MAX_MESSAGING_ITEMS_PER_ENTRY,
    META_INSTAGRAM_WEBHOOK_FIELDS,
    META_PAGE_WEBHOOK_FIELDS,
)
from .messaging import (
    atomic_dedupe_key,
    atomic_events,
    canonical_json_digest,
    private_media_locators,
    routing_values,
    sanitize_webhook_envelope,
)

META_MESSAGING_CONSUMER_KEY = "contact_center.meta"
_SUPPORTED_OBJECT_TYPES = frozenset({"page", "instagram"})
_MEDIA_VAULT_UNAVAILABLE_REASON = "shared_locator_vault_unavailable"
META_MESSAGING_SUBSCRIPTIONS = {
    "messenger_page": {
        "object_type": "page",
        "fields": META_PAGE_WEBHOOK_FIELDS,
    },
    "instagram_page_linked": {
        "object_type": "instagram",
        "fields": META_INSTAGRAM_WEBHOOK_FIELDS,
    },
}
_PAGE_EVENT_FIELDS = {
    "delivery": "message_deliveries",
    "message_edit": "message_edits",
    "postback": "messaging_postbacks",
    "reaction": "message_reactions",
    "read": "message_reads",
    "referral": "messaging_referrals",
}
_INSTAGRAM_EVENT_FIELDS = {
    "postback": "messaging_postbacks",
    "reaction": "message_reactions",
    "read": "messaging_seen",
    "referral": "messaging_referral",
}


def _private_locator_fallback_rejections(envelope, delivery):
    """Fail closed if a caller bypasses the shared private-locator vault."""

    seed = hashlib.sha256(
        ("shared-meta:%s:%s" % (delivery.public_ref, delivery.content_sha256)).encode(
            "utf-8"
        )
    ).hexdigest()
    candidates = private_media_locators(envelope, seed)
    return tuple(
        {
            "sequence": candidate["sequence"],
            "slot": candidate["slot"],
            "reason": _MEDIA_VAULT_UNAVAILABLE_REASON,
        }
        for candidate in candidates
    )


def shared_private_media(decoded_envelope, delivery, *, eligible_item_keys=None):
    """Extract private URLs and return only opaque references for projection.

    ``candidates`` is intentionally consumed immediately by the private vault.  It
    must never be added to a ledger, DTO, exception or log record.
    """

    seed = hashlib.sha256(
        ("shared-meta:%s:%s" % (delivery.public_ref, delivery.content_sha256)).encode(
            "utf-8"
        )
    ).hexdigest()
    rejections = []
    extracted = private_media_locators(
        decoded_envelope,
        seed,
        rejections=rejections,
    )
    eligible_item_keys = (
        None if eligible_item_keys is None else frozenset(eligible_item_keys)
    )
    candidates = []
    references = {}
    for candidate in extracted:
        sequence = candidate["sequence"]
        entry_index, item_index = divmod(sequence, MAX_MESSAGING_ITEMS_PER_ENTRY)
        item_key = "entry:%s:messaging:%s" % (entry_index, item_index)
        if eligible_item_keys is not None and item_key not in eligible_item_keys:
            continue
        candidate = dict(
            candidate,
            meta_item_key=item_key,
        )
        candidates.append(candidate)
        references[(sequence, candidate["slot"])] = candidate["reference"]
    if eligible_item_keys is not None:
        eligible_sequences = {
            entry_index * MAX_MESSAGING_ITEMS_PER_ENTRY + item_index
            for entry_index, item_index in (
                (
                    int(item_key.split(":")[1]),
                    int(item_key.split(":")[3]),
                )
                for item_key in eligible_item_keys
            )
        }
        rejections = [
            rejection
            for rejection in rejections
            if rejection["sequence"] in eligible_sequences
        ]
    return tuple(candidates), references, tuple(rejections)


def messaging_item_specs(
    decoded_envelope,
    delivery,
    *,
    private_locator_references=None,
    private_locator_rejections=None,
    eligible_item_keys=None,
):
    """Project Meta messaging carriers into shared, sanitized item specs.

    This function never persists the provider envelope and never returns a signed
    URL.  The shared dispatcher remains the sole owner of delivery/item ledgers.
    """

    if not isinstance(decoded_envelope, dict):
        return ()
    object_type = str(decoded_envelope.get("object") or "").strip().lower()
    if object_type not in _SUPPORTED_OBJECT_TYPES:
        return ()
    if private_locator_references is None:
        private_locator_references = {}
        private_locator_rejections = _private_locator_fallback_rejections(
            decoded_envelope, delivery
        )
    sanitized = sanitize_webhook_envelope(
        decoded_envelope,
        private_locator_references=private_locator_references,
        private_locator_rejections=private_locator_rejections,
    )
    eligible_item_keys = (
        None if eligible_item_keys is None else frozenset(eligible_item_keys)
    )
    result = []
    for atomic_event in atomic_events(sanitized):
        entry_index = atomic_event["entry_index"]
        item_index = atomic_event["item_index"]
        item_key = "entry:%s:messaging:%s" % (entry_index, item_index)
        if eligible_item_keys is not None and item_key not in eligible_item_keys:
            continue
        route = routing_values(atomic_event)
        if route:
            occurrence_ref = atomic_dedupe_key(atomic_event, route)
        else:
            occurrence_ref = "messaging:%s:%s" % (
                atomic_event["entry"]["id"],
                canonical_json_digest(atomic_event),
            )
        event_field = messaging_event_field(object_type, atomic_event["messaging"])
        if not event_field:
            # Leave unknown carriers unclaimed.  Another installed consumer can
            # claim the item; otherwise meta_webhook_base keeps its placeholder
            # visible as unrouted technical evidence.
            continue
        result.append(
            {
                "item_key": item_key,
                "kind": "messaging",
                "object_type": object_type,
                "event_field": event_field,
                "target_asset_id": atomic_event["entry"]["id"],
                "occurrence_ref": occurrence_ref,
                "payload_json": atomic_event,
            }
        )
    return tuple(result)


def messaging_event_field(object_type, messaging_item):
    """Classify one carrier using Meta's actual subscription field names."""

    item = messaging_item if isinstance(messaging_item, dict) else {}
    message = item.get("message")
    if isinstance(message, dict):
        if object_type == "page" and message.get("is_echo") is True:
            return "message_echoes"
        return "messages"
    fields = (
        _INSTAGRAM_EVENT_FIELDS if object_type == "instagram" else _PAGE_EVENT_FIELDS
    )
    for carrier, event_field in fields.items():
        if carrier in item:
            return event_field
    return ""


def subscription_contract(transport_mode):
    """Return the immutable shared-webhook contract for one Meta route mode."""

    contract = META_MESSAGING_SUBSCRIPTIONS.get(transport_mode)
    if not contract:
        return None
    return {
        "object_type": contract["object_type"],
        "fields": frozenset(contract["fields"]),
    }


def route_contract(payload):
    """Return canonical connection dimensions for one shared messaging item."""

    route = routing_values(payload)
    if not route:
        return None
    return {
        "route": route,
        "platform": route["platform"],
        "transport_mode": route["transport_mode"],
        "target_asset_id": route["asset_id"],
    }


def inbox_dedupe_key(payload, route):
    return atomic_dedupe_key(payload, route)
