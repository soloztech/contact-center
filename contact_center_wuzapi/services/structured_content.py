"""Small, bounded projections for the pinned WuzAPI rich message contracts."""

import json
import math
import quopri
import re

from odoo.addons.contact_center_base.services.adapter import AdapterError
from odoo.addons.contact_center_base.services.structured_content import (
    validate_outbound_structured_content,
    validate_structured_content,
)

OUTBOUND_STRUCTURED_CONTENT = {
    "buttons": {
        "body_mode": "required",
        "max_body_length": 1024,
        "max_buttons": 3,
        "action_types": ["reply", "url", "phone"],
        "max_title_length": 60,
        "max_footer_length": 60,
        "max_button_title_length": 20,
        "max_id_length": 200,
        "max_phone_length": 30,
    },
    "list": {
        "body_mode": "required",
        "max_body_length": 1024,
        "max_sections": 10,
        "max_rows": 10,
        "max_title_length": 60,
        "max_footer_length": 60,
        "max_button_text_length": 20,
        "max_section_title_length": 60,
        "max_row_title_length": 24,
        "max_row_description_length": 72,
        "max_id_length": 200,
    },
    "contacts": {
        "body_mode": "none",
        "max_body_length": 0,
        "max_contacts": 1,
        "max_name_length": 120,
        "max_phones": 5,
        "max_emails": 5,
        "max_phone_length": 30,
        "max_email_length": 254,
    },
    "location": {
        "body_mode": "none",
        "max_body_length": 0,
        "max_name_length": 120,
        "max_address_length": 300,
        "allow_live": False,
    },
}
_MAX_PARAMS_BYTES = 16_384
_MAX_VCARD_BYTES = 32_768


def _get(value, key):
    if not isinstance(value, dict):
        return None
    return next(
        (item for name, item in value.items() if name.lower() == key.lower()),
        None,
    )


def _label(value, maximum):
    if not isinstance(value, str):
        return ""
    return "".join(
        character
        for character in " ".join(value.split())
        if ord(character) >= 32 and ord(character) != 127
    )[:maximum].strip()


def _identifier(value):
    # Truncating identifiers would change the selected action's identity.
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return ""
    return value


def _items(value, limit):
    return value[:limit] if isinstance(value, list) else []


def _params(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_PARAMS_BYTES:
        return {}
    try:
        result = json.loads(value)
    except (ValueError, RecursionError):
        return {}
    return result if isinstance(result, dict) else {}


def _native_button(button):
    name = _get(button, "name")
    params = _params(_get(button, "buttonParamsJSON"))
    title = _label(params.get("display_text"), 20)
    if not title:
        return {}
    if name == "quick_reply":
        identifier = _identifier(params.get("id"))
        return {"type": "reply", "id": identifier, "title": title} if identifier else {}
    if name == "cta_url":
        return {"type": "url", "url": params.get("url"), "title": title}
    if name == "cta_call":
        return {"type": "phone", "phone": params.get("phone_number"), "title": title}
    return {}


def _selection(message):
    for kind, identifier_field in (
        ("buttonsResponseMessage", "selectedButtonID"),
        ("templateButtonReplyMessage", "selectedID"),
    ):
        value = _get(message, kind)
        if isinstance(value, dict):
            response = (
                _get(value, "response") if kind == "buttonsResponseMessage" else value
            )
            return {
                "type": "selection",
                "id": _identifier(_get(value, identifier_field)),
                "title": _label(_get(response, "selectedDisplayText"), 200),
            }
    value = _get(message, "listResponseMessage")
    if isinstance(value, dict):
        return {
            "type": "selection",
            "id": _identifier(_get(_get(value, "singleSelectReply"), "selectedRowID")),
            "title": _label(_get(value, "title"), 200),
        }
    value = _get(message, "interactiveResponseMessage")
    # encoding/json preserves both exported oneof struct names at this boundary.
    native = _get(
        _get(value, "InteractiveResponseMessage"), "NativeFlowResponseMessage"
    )
    if _get(native, "name") not in ("quick_reply", "single_select"):
        return {}
    params = _params(_get(native, "paramsJSON"))
    return {
        "type": "selection",
        "id": _identifier(params.get("id")),
        "title": _label(
            params.get("display_text") or _get(_get(value, "body"), "text"), 200
        ),
    }


def _buttons(message):
    value = _get(message, "buttonsMessage")
    if isinstance(value, dict):
        buttons = []
        for item in _items(_get(value, "buttons"), 3):
            identifier = _identifier(_get(item, "buttonID"))
            title = _label(_get(_get(item, "buttonText"), "displayText"), 20)
            if identifier and title:
                buttons.append({"type": "reply", "id": identifier, "title": title})
        return {
            "type": "buttons",
            "title": _label(_get(_get(value, "Header"), "Text"), 60),
            "footer": _label(_get(value, "footerText"), 60),
            "buttons": buttons,
        }, _get(value, "contentText")
    value = _get(message, "interactiveMessage")
    if not isinstance(value, dict):
        return {}, None
    native = _get(_get(value, "InteractiveMessage"), "NativeFlowMessage")
    buttons = [_native_button(item) for item in _items(_get(native, "buttons"), 3)]
    return {
        "type": "buttons",
        "title": _label(_get(_get(value, "header"), "title"), 60),
        "footer": _label(_get(_get(value, "footer"), "text"), 60),
        "buttons": [button for button in buttons if button],
    }, _get(_get(value, "body"), "text")


def _list(message):
    value = _get(message, "listMessage")
    if not isinstance(value, dict):
        return {}, None
    sections = []
    remaining = 10
    for section in _items(_get(value, "sections"), 10):
        rows = []
        for row in _items(_get(section, "rows"), remaining):
            identifier = _identifier(_get(row, "rowID"))
            title = _label(_get(row, "title"), 24)
            if identifier and title:
                rows.append(
                    {
                        "id": identifier,
                        "title": title,
                        "description": _label(_get(row, "description"), 72),
                    }
                )
        if rows:
            sections.append(
                {"title": _label(_get(section, "title"), 60) or "Opções", "rows": rows}
            )
            remaining -= len(rows)
        if not remaining:
            break
    return {
        "type": "list",
        "title": _label(_get(value, "title"), 60),
        "footer": _label(_get(value, "footerText"), 60),
        "button_text": _label(_get(value, "buttonText"), 20),
        "sections": sections,
    }, _get(value, "description")


def _vcard_values(raw):
    """Read only names/phones/emails; never import arbitrary vCard properties."""

    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_VCARD_BYTES:
        return []
    lines = []
    for line in raw.replace("\r\n", "\n").split("\n")[:256]:
        if (
            lines
            and "ENCODING=QUOTED-PRINTABLE" in lines[-1].upper()
            and lines[-1].endswith("=")
        ):
            lines[-1] = lines[-1][:-1] + line
        elif lines and line.startswith((" ", "\t")):
            lines[-1] += line[1:]
        else:
            lines.append(line)
    values = []
    for line in lines:
        header, separator, value = line.partition(":")
        field = header.split(";", 1)[0].rsplit(".", 1)[-1].upper()
        if not separator or field not in ("FN", "TEL", "EMAIL"):
            continue
        if "ENCODING=QUOTED-PRINTABLE" in header.upper():
            charset_match = re.search(r"(?:^|;)CHARSET=([^;]+)", header, re.IGNORECASE)
            charset = (
                charset_match.group(1).strip('"').lower() if charset_match else "utf-8"
            )
            if charset not in ("utf-8", "utf8", "iso-8859-1", "latin1", "us-ascii"):
                continue
            value = quopri.decodestring(value.encode("utf-8")).decode(
                charset, errors="replace"
            )
        value = re.sub(
            r"\\([nN,;\\])", lambda match: "\n" if match[1] in "nN" else match[1], value
        )
        values.append((field, value))
    return values


def _contact(value):
    name = _label(_get(value, "displayName"), 120)
    phones, emails = [], []
    for field, item in _vcard_values(_get(value, "vcard")):
        if field == "FN" and not name:
            name = _label(item, 120)
        elif field == "TEL":
            phone = re.sub(
                r"[\s().-]", "", re.sub(r"^tel:", "", item, flags=re.IGNORECASE)
            )
            if (
                re.fullmatch(r"\+?[0-9]{3,29}", phone)
                and phone not in phones
                and len(phones) < 5
            ):
                phones.append(phone)
        elif field == "EMAIL":
            email = item.strip()
            if (
                len(email) <= 254
                and re.fullmatch(r"[^@\s?&#<>]+@[^@\s?&#<>]+\.[^@\s?&#<>]+", email)
                and email not in emails
                and len(emails) < 5
            ):
                emails.append(email)
    return {"name": name or "Contato", "phones": phones, "emails": emails}


def _contacts(message):
    value = _get(message, "contactMessage")
    if isinstance(value, dict):
        return {"type": "contacts", "contacts": [_contact(value)]}
    value = _get(message, "contactsArrayMessage")
    contacts = [
        _contact(item)
        for item in _items(_get(value, "contacts"), 10)
        if isinstance(item, dict)
    ]
    return {"type": "contacts", "contacts": contacts} if contacts else {}


def _location(message):
    value = _get(message, "locationMessage")
    live = False
    if not isinstance(value, dict):
        value = _get(message, "liveLocationMessage")
        live = True
    if not isinstance(value, dict):
        return {}
    latitude, longitude = _get(value, "degreesLatitude"), _get(
        value, "degreesLongitude"
    )
    for coordinate, limit in ((latitude, 90), (longitude, 180)):
        if (
            isinstance(coordinate, bool)
            or not isinstance(coordinate, (int, float))
            or abs(coordinate) > limit
            or not math.isfinite(coordinate)
        ):
            return {}
    return {
        "type": "location",
        "latitude": float(latitude),
        "longitude": float(longitude),
        "name": _label(_get(value, "caption" if live else "name"), 120),
        "address": _label(_get(value, "address"), 300),
        "live": live,
    }


def normalize_structured_content(message):
    """Return a validated neutral card plus its body, or keep the text fallback."""

    content = _selection(message) or _contacts(message) or _location(message)
    body = None
    if not content:
        content, body = _list(message)
    if not content:
        content, body = _buttons(message)
    try:
        validate_structured_content(content)
    except ValueError:
        return {}, None
    if isinstance(body, str):
        body = "".join(
            character
            for character in body
            if ord(character) >= 32 or character in "\n\t"
        )[:4096]
    return content, body if isinstance(body, str) else None


def validate_outbound_content(content, text):
    try:
        validate_outbound_structured_content(content, text, OUTBOUND_STRUCTURED_CONTENT)
    except ValueError as error:
        raise AdapterError(str(error)) from error
    kind = content.get("type")
    if kind == "buttons":
        identifiers = [
            button["id"] for button in content["buttons"] if button["type"] == "reply"
        ]
    elif kind == "list":
        identifiers = [
            row["id"] for section in content["sections"] for row in section["rows"]
        ]
    else:
        identifiers = []
    if any(identifier != identifier.strip() for identifier in identifiers):
        raise AdapterError(
            "WuzAPI action identifiers cannot start or end with whitespace"
        )
    if kind == "location":
        # The pinned handler treats these valid coordinates as missing fields.
        if content["latitude"] == 0 or content["longitude"] == 0:
            raise AdapterError(
                "The pinned WuzAPI cannot send latitude or longitude zero"
            )


def _vcard_escape(value):
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def build_outbound_content(content, text):
    validate_outbound_content(content, text)
    kind = content["type"]
    if kind == "buttons":
        buttons = []
        for button in content["buttons"]:
            mapped = {"title": button["title"]}
            if button["type"] == "reply":
                mapped.update(type="reply", id=button["id"])
            elif button["type"] == "url":
                mapped.update(type="cta_url", url=button["url"])
            else:
                mapped.update(type="cta_call", phone_number=button["phone"])
            buttons.append(mapped)
        return "/chat/send/buttons", {
            "Body": text,
            "Title": content.get("title", ""),
            "Footer": content.get("footer", ""),
            "Buttons": buttons,
        }
    if kind == "list":
        return "/chat/send/list", {
            "Desc": text,
            "ButtonText": content["button_text"],
            "TopText": content.get("title", ""),
            "FooterText": content.get("footer", ""),
            "Sections": [
                {
                    "title": section["title"],
                    "rows": [
                        {
                            "RowId": row["id"],
                            "title": row["title"],
                            "desc": row.get("description", ""),
                        }
                        for row in section["rows"]
                    ],
                }
                for section in content["sections"]
            ],
        }
    if kind == "contacts":
        contact = content["contacts"][0]
        lines = ["BEGIN:VCARD", "VERSION:3.0", "FN:%s" % _vcard_escape(contact["name"])]
        lines.extend(
            "TEL;TYPE=CELL:%s" % _vcard_escape(phone)
            for phone in contact.get("phones", [])
        )
        lines.extend(
            "EMAIL;TYPE=INTERNET:%s" % _vcard_escape(email)
            for email in contact.get("emails", [])
        )
        lines.append("END:VCARD")
        return "/chat/send/contact", {
            "Name": contact["name"],
            "Vcard": "\r\n".join(lines) + "\r\n",
        }
    return "/chat/send/location", {
        "Latitude": content["latitude"],
        "Longitude": content["longitude"],
        # The pinned endpoint has no Address field. Preserve both human labels
        # in the only location label it actually transmits.
        "Name": "\n".join(
            dict.fromkeys(
                value
                for value in (content.get("name"), content.get("address"))
                if value
            )
        ),
    }
