"""Small, provider-neutral cards. No provider payloads or executable actions."""

import json
import math
import re
from urllib.parse import parse_qsl, urlsplit

OUTBOUND_TYPES = frozenset({"buttons", "list", "contacts", "location"})
# Storage/rendering safety ceilings, independent of any provider's send limits.
MAX_STRUCTURED_BYTES = 128 * 1024
OUTBOUND_SPEC_LIMITS = {
    "buttons": {
        "max_buttons": (1, 25),
        "max_title_length": (0, 512),
        "max_footer_length": (0, 512),
        "max_button_title_length": (1, 200),
        "max_id_length": (1, 512),
        "max_phone_length": (1, 64),
    },
    "list": {
        "max_sections": (1, 20),
        "max_rows": (1, 100),
        "max_title_length": (0, 512),
        "max_footer_length": (0, 512),
        "max_button_text_length": (1, 200),
        "max_section_title_length": (1, 512),
        "max_row_title_length": (1, 200),
        "max_row_description_length": (0, 1000),
        "max_id_length": (1, 512),
    },
    "contacts": {
        "max_contacts": (1, 50),
        "max_name_length": (1, 512),
        "max_phones": (0, 20),
        "max_emails": (0, 20),
        "max_phone_length": (1, 64),
        "max_email_length": (1, 254),
    },
    "location": {
        "max_name_length": (0, 512),
        "max_address_length": (0, 2000),
    },
}


def _object(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError("Structured content contains unknown fields")
    if set(required) - set(value):
        raise ValueError("Structured content is missing required fields")


def _text(value, maximum, required=True):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (required and not value.strip())
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("Structured content contains invalid text")


def _optional_text(value, limits):
    for key, maximum in limits.items():
        if key in value:
            _text(value[key], maximum, required=False)


def _array(value, maximum, minimum=1):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError("Structured content contains an invalid collection")


def _url(value):
    _text(value, 2048)
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or "\\" in value
            or any(char.isspace() for char in value)
            or any(
                any(
                    secret in key.lower()
                    for secret in ("token", "secret", "signature", "credential")
                )
                for key, _value in parse_qsl(parsed.query)
            )
        ):
            raise ValueError("invalid URL")
    except ValueError as error:
        raise ValueError("Structured links must be public HTTPS URLs") from error


def _phone(value):
    _text(value, 64)
    if not re.fullmatch(r"\+?[0-9 ()\-.]+", value) or not any(
        char.isdigit() for char in value
    ):
        raise ValueError("Invalid contact phone")


def _email(value):
    _text(value, 254)
    if not re.fullmatch(r"[^@\s?&#<>]+@[^@\s?&#<>]+\.[^@\s?&#<>]+", value):
        raise ValueError("Invalid contact email")


def _validate_buttons(value):
    _object(value, {"type", "title", "footer", "buttons"}, {"buttons"})
    _optional_text(value, {"title": 512, "footer": 512})
    _array(value["buttons"], 25)
    ids = []
    for button in value["buttons"]:
        if not isinstance(button, dict):
            raise ValueError("Invalid button")
        action = button.get("type")
        if not isinstance(action, str):
            raise ValueError("Invalid button action")
        field = {"reply": "id", "url": "url", "phone": "phone"}.get(action)
        if not field:
            raise ValueError("Invalid button action")
        _object(button, {"type", "title", field}, {"title", field})
        _text(button["title"], 200)
        if action == "url":
            _url(button[field])
        elif action == "phone":
            _phone(button[field])
        else:
            _text(button[field], 512)
        if action == "reply":
            ids.append(button[field])
    if len(ids) != len(set(ids)):
        raise ValueError("Button IDs must be unique")


def _validate_list(value):
    _object(
        value,
        {"type", "title", "footer", "button_text", "sections"},
        {"button_text", "sections"},
    )
    _optional_text(value, {"title": 512, "footer": 512})
    _text(value["button_text"], 200)
    _array(value["sections"], 20)
    ids = []
    for section in value["sections"]:
        _object(section, {"title", "rows"}, {"title", "rows"})
        _text(section["title"], 512)
        _array(section["rows"], 100)
        for row in section["rows"]:
            _object(row, {"id", "title", "description"}, {"id", "title"})
            _text(row["id"], 512)
            _text(row["title"], 200)
            _optional_text(row, {"description": 1000})
            ids.append(row["id"])
    if len(ids) > 100 or len(ids) != len(set(ids)):
        raise ValueError("Lists require at most one hundred unique row IDs")


def _validate_contacts(value):
    _object(value, {"type", "contacts"}, {"contacts"})
    _array(value["contacts"], 50)
    for contact in value["contacts"]:
        _object(contact, {"name", "phones", "emails"}, {"name"})
        _text(contact["name"], 512)
        for key, maximum in (("phones", 64), ("emails", 254)):
            if key in contact:
                _array(contact[key], 20, minimum=0)
                for item in contact[key]:
                    _text(item, maximum)
                    (_phone if key == "phones" else _email)(item)


def _validate_location(value):
    _object(
        value,
        {"type", "latitude", "longitude", "name", "address", "live"},
        {"latitude", "longitude"},
    )
    _optional_text(value, {"name": 512, "address": 2000})
    for key, bound in (("latitude", 90), ("longitude", 180)):
        coordinate = value[key]
        if (
            not isinstance(coordinate, (int, float))
            or isinstance(coordinate, bool)
            or not -bound <= coordinate <= bound
            or not math.isfinite(coordinate)
        ):
            raise ValueError("Location coordinates are invalid")
    if "live" in value and not isinstance(value["live"], bool):
        raise ValueError("Location live flag must be boolean")


def _validate_selection(value):
    _object(value, {"type", "id", "title"}, {"id", "title"})
    _text(value["id"], 512)
    _text(value["title"], 512)


def _validate_shared(value):
    _object(value, {"type", "items"}, {"items"})
    _array(value["items"], 10)
    for item in value["items"]:
        _object(item, {"kind", "title", "url"}, {"kind"})
        if not isinstance(item["kind"], str) or item["kind"] not in {
            "post",
            "reel",
            "story",
            "link",
        }:
            raise ValueError("Invalid shared content kind")
        _optional_text(item, {"title": 200})
        if "url" in item:
            _url(item["url"])


def validate_structured_content(value):
    """Validate an entire card, returning it unchanged for stable replay."""

    if not isinstance(value, dict):
        raise ValueError("Structured content must be an object")
    if not value:
        return value
    kind = value.get("type")
    validators = {
        "buttons": _validate_buttons,
        "list": _validate_list,
        "contacts": _validate_contacts,
        "location": _validate_location,
        "selection": _validate_selection,
        "shared": _validate_shared,
    }
    if not isinstance(kind, str) or kind not in validators:
        raise ValueError("Unknown structured content type")
    validators[kind](value)
    if len(json.dumps(value, ensure_ascii=True).encode("ascii")) > MAX_STRUCTURED_BYTES:
        raise ValueError("Structured content exceeds the storage limit")
    return value


def validate_outbound_structured_capabilities(value):
    """Require complete explicit send specs; malformed declarations enable nothing."""
    _object(value, OUTBOUND_TYPES)
    for kind, spec in value.items():
        limits = OUTBOUND_SPEC_LIMITS[kind]
        fields = set(limits) | {"body_mode", "max_body_length"}
        if kind == "buttons":
            fields.add("action_types")
        if kind == "location":
            fields.add("allow_live")
        _object(spec, fields, fields)
        for name, (minimum, maximum) in limits.items():
            number = spec[name]
            if type(number) is not int or not minimum <= number <= maximum:
                raise ValueError("Invalid outbound card limit: %s" % name)
        mode = spec["body_mode"]
        if not isinstance(mode, str) or mode not in {"none", "optional", "required"}:
            raise ValueError("Invalid outbound card body mode")
        maximum = spec["max_body_length"]
        if type(maximum) is not int or not 0 <= maximum <= 65536:
            raise ValueError("Invalid outbound card body limit")
        if (mode == "none") != (maximum == 0):
            raise ValueError("Outbound card body mode and limit are inconsistent")
        if kind == "buttons":
            actions = spec["action_types"]
            _array(actions, 3)
            if (
                any(not isinstance(action, str) for action in actions)
                or set(actions) - {"reply", "url", "phone"}
                or len(actions) != len(set(actions))
            ):
                raise ValueError("Invalid outbound button actions")
        if kind == "location" and not isinstance(spec["allow_live"], bool):
            raise ValueError("Invalid outbound live location capability")
    return value


def outbound_structured_capabilities(capabilities):
    """Project only a valid outbound map; never interpret the former list key."""
    if not isinstance(capabilities, dict):
        return {}
    value = capabilities.get("outbound_structured_content", {})
    try:
        return validate_outbound_structured_capabilities(value)
    except (TypeError, ValueError):
        return {}


def _outbound_text_limits(value, spec, fields):
    for field, limit in fields.items():
        if field in value and len(value[field]) > spec[limit]:
            raise ValueError("The provider limits the card field: %s" % field)


def _outbound_buttons(content, spec):
    if len(content["buttons"]) > spec["max_buttons"]:
        raise ValueError("The provider button count limit was exceeded")
    for button in content["buttons"]:
        if button["type"] not in spec["action_types"]:
            raise ValueError("The provider does not support this button action")
        _outbound_text_limits(
            button,
            spec,
            {
                "title": "max_button_title_length",
                "id": "max_id_length",
                "phone": "max_phone_length",
            },
        )


def _outbound_list(content, spec):
    if len(content["sections"]) > spec["max_sections"]:
        raise ValueError("The provider section count limit was exceeded")
    rows = []
    for section in content["sections"]:
        _outbound_text_limits(section, spec, {"title": "max_section_title_length"})
        rows.extend(section["rows"])
    if len(rows) > spec["max_rows"]:
        raise ValueError("The provider row count limit was exceeded")
    for row in rows:
        _outbound_text_limits(
            row,
            spec,
            {
                "id": "max_id_length",
                "title": "max_row_title_length",
                "description": "max_row_description_length",
            },
        )


def _outbound_contacts(content, spec):
    if len(content["contacts"]) > spec["max_contacts"]:
        raise ValueError("The provider contact count limit was exceeded")
    for contact in content["contacts"]:
        _outbound_text_limits(contact, spec, {"name": "max_name_length"})
        for field, maximum in (
            ("phones", "max_phone_length"),
            ("emails", "max_email_length"),
        ):
            items = contact.get(field, [])
            if len(items) > spec["max_%s" % field] or any(
                len(item) > spec[maximum] for item in items
            ):
                raise ValueError("The provider contact field limit was exceeded")


def validate_outbound_structured_content(content, text, capabilities):
    """Validate one send against its explicit provider spec, without provider I/O."""
    validate_structured_content(content)
    validate_outbound_structured_capabilities(capabilities)
    spec = capabilities.get(content.get("type"))
    if not spec:
        raise ValueError("This structured content type is not sendable")
    if not isinstance(text, str) or len(text) > spec["max_body_length"]:
        raise ValueError("The provider card body limit was exceeded")
    if spec["body_mode"] == "required" and not text.strip():
        raise ValueError("The provider requires a card body")
    kind = content["type"]
    if kind in {"buttons", "list"}:
        _outbound_text_limits(
            content, spec, {"title": "max_title_length", "footer": "max_footer_length"}
        )
    if kind == "buttons":
        _outbound_buttons(content, spec)
    elif kind == "list":
        _outbound_text_limits(content, spec, {"button_text": "max_button_text_length"})
        _outbound_list(content, spec)
    elif kind == "contacts":
        _outbound_contacts(content, spec)
    else:
        _outbound_text_limits(
            content, spec, {"name": "max_name_length", "address": "max_address_length"}
        )
        if content.get("live") and not spec["allow_live"]:
            raise ValueError("The provider cannot send live locations")
    return True
