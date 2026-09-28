"""Cloud API outbound: sends, reactions and read receipts.

``POST /{version}/{phone_number_id}/messages`` has no remote idempotency key.
Every result that cannot prove a refusal is uncertain and never re-sent
automatically (R01). Sends and reactions require an inbound customer message in
the last 24 hours on the stable route: the same inbox, WhatsApp Business Account
and phone number ID, including replaced connections of that route (R15). Read
receipts have their own 30-day contract (R10).
"""

import datetime
import re
import uuid

from odoo import fields

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    AmbiguousTimeoutError,
    ProviderPausedError,
    TransientAdapterError,
)
from odoo.addons.contact_center_base.services.dto import AdapterResult, CommandDTO

from .api import graph_send, resolve_graph_runtime
from .contracts import (
    BSUID_NAMESPACE,
    CLIENT_MESSAGE_ID_PREFIX,
    MARK_READ_WINDOW,
    MAX_FUTURE_CLOCK_SKEW,
    PHONE_NAMESPACE,
    RESPONSE_WINDOW,
    WHATSAPP_SYSTEM_CONTENT_TYPE,
)
from .identity import bsuid_value, phone_digits
from .media import outbound_media_content, upload_media

_MAX_TEXT_CHARACTERS = 4096
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_MESSAGE_ID = 512
_WAMID_RE = re.compile(r"^wamid\.[A-Za-z0-9._=+/-]{1,500}$")
_MAX_READ_IDS = 100


def derive_client_message_id(command_id):
    try:
        return "%s%s" % (CLIENT_MESSAGE_ID_PREFIX, uuid.UUID(str(command_id)))
    except (AttributeError, TypeError, ValueError) as error:
        raise AdapterError("WhatsApp Cloud command ID must be a UUID") from error


def _utc_naive(value):
    value = fields.Datetime.to_datetime(value)
    if not value:
        return None
    if value.tzinfo:
        return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value


def _valid_message_id(value):
    return bool(
        isinstance(value, str)
        and value.strip()
        and value == value.strip()
        and len(value) <= _MAX_MESSAGE_ID
        and not any(character.isspace() for character in value)
    )


def _active_route(connection):
    if not connection._wac_outbound_is_ready():
        raise ProviderPausedError("WhatsApp Cloud outbound route is unavailable")
    return connection.sudo()


def _route_values(connection):
    sudo_connection = connection.sudo()
    phone_number_id = sudo_connection.wa_phone_number_id or ""
    waba_id = sudo_connection.wa_business_account_id or ""
    if not re.fullmatch(r"[0-9]{1,40}", phone_number_id) or not re.fullmatch(
        r"[0-9]{1,40}", waba_id
    ):
        raise AdapterError("WhatsApp Cloud outbound route is invalid")
    return phone_number_id, waba_id, sudo_connection.wa_graph_version


def _direct_channel_binding(env, connection, command):
    if command.conversation.conversation_type != "direct":
        raise AdapterError("WhatsApp Cloud supports direct conversations only")
    bindings = (
        env["contact.center.channel.binding"]
        .sudo()
        .search(
            [
                ("account_id", "=", connection.account_id.id),
                ("conversation_ref", "=", command.conversation_ref),
                ("conversation_type", "=", "direct"),
                ("active", "=", True),
                ("merged_into_id", "=", False),
            ],
            limit=2,
        )
    )
    if len(bindings) != 1:
        raise AdapterError("WhatsApp Cloud conversation evidence is ambiguous")
    return bindings


def latest_eligible_inbound_at(env, connection, command, *, now=None):
    """Return the latest customer message on the stable route, or fail closed.

    SQL aggregates across replaced connections of the same inbox, WABA and
    phone number ID; another number or provider never opens the window.
    """

    binding = _direct_channel_binding(env, connection, command)
    phone_number_id, waba_id, _version = _route_values(connection)
    env["contact.center.message.binding"].flush_model(
        [
            "channel_binding_id",
            "direction",
            "origin",
            "provider_connection_id",
            "content_type",
        ]
    )
    env["mail.message"].flush_model(["date"])
    env["contact.center.provider.connection"].flush_model(
        ["account_id", "adapter_key", "wa_phone_number_id", "wa_business_account_id"]
    )
    env.cr.execute(
        """
        SELECT MAX(message.date)
          FROM contact_center_message_binding AS message_binding
          JOIN mail_message AS message
            ON message.id = message_binding.message_id
          JOIN contact_center_provider_connection AS provider_connection
            ON provider_connection.id = message_binding.provider_connection_id
         WHERE message_binding.channel_binding_id = %s
           AND message_binding.direction = 'inbound'
           AND message_binding.origin = 'provider'
           AND message_binding.content_type <> %s
           AND provider_connection.account_id = %s
           AND provider_connection.adapter_key = 'whatsapp_cloud'
           AND provider_connection.wa_phone_number_id = %s
           AND provider_connection.wa_business_account_id = %s
        """,
        [
            binding.id,
            WHATSAPP_SYSTEM_CONTENT_TYPE,
            connection.account_id.id,
            phone_number_id,
            waba_id,
        ],
    )
    latest = _utc_naive(env.cr.fetchone()[0])
    now = _utc_naive(now or fields.Datetime.now())
    if not latest:
        raise AdapterError(
            "WhatsApp outside the customer service window; templates are not "
            "supported yet"
        )
    if latest > now + MAX_FUTURE_CLOCK_SKEW:
        raise AdapterError("WhatsApp window evidence has a future timestamp")
    if latest < now - RESPONSE_WINDOW:
        raise AdapterError(
            "WhatsApp outside the customer service window; templates are not "
            "supported yet"
        )
    return latest


def _recipient(command):
    """Return the recipient fields for the conversation's routing address."""

    target = command.target_address
    if (
        not target
        or target.role not in ("primary", "routing")
        or command.conversation_ref != target.value_normalized
    ):
        raise AdapterError("WhatsApp Cloud outbound target is invalid")
    supplied = {
        (address.namespace, address.value_normalized)
        for address in command.conversation.addresses
    }
    if (target.namespace, target.value_normalized) not in supplied:
        raise AdapterError("WhatsApp Cloud outbound target is not in the conversation")
    if target.namespace == PHONE_NAMESPACE:
        digits = phone_digits(target.value_normalized)
        if not digits or target.value != target.value_normalized:
            raise AdapterError("WhatsApp Cloud outbound phone is invalid")
        return {"to": digits}
    if target.namespace == BSUID_NAMESPACE:
        waba_id = target.value_normalized.split(":", 1)[0]
        if bsuid_value(waba_id, target.value) != target.value_normalized:
            raise AdapterError("WhatsApp Cloud outbound BSUID is invalid")
        return {"recipient": target.value}
    raise AdapterError("WhatsApp Cloud outbound target namespace is unsupported")


def _check_waba_scope(connection, command):
    target = command.target_address
    if target and target.namespace == BSUID_NAMESPACE:
        _phone, waba_id, _version = _route_values(connection)
        if target.value_normalized.split(":", 1)[0] != waba_id:
            raise AdapterError("WhatsApp BSUID belongs to another business account")


def _reply_message_id(command):
    reply_reference = command.reply_to
    message_reply_id = command.message.reply_to_external_id
    if not reply_reference:
        if message_reply_id:
            raise AdapterError("WhatsApp Cloud reply evidence is incomplete")
        return ""
    if set(reply_reference) - {"external_message_id", "protocol_snapshot"}:
        raise AdapterError("WhatsApp Cloud reply evidence has unsupported fields")
    reference_id = reply_reference.get("external_message_id")
    if (
        not _valid_message_id(reference_id)
        or reference_id != message_reply_id
        or reply_reference.get("protocol_snapshot", {})
        != command.message.protocol_snapshot
    ):
        raise AdapterError("WhatsApp Cloud reply evidence is inconsistent")
    return reference_id


def _common_scope(connection, command, command_type):
    if not isinstance(command, CommandDTO):
        raise AdapterError("WhatsApp Cloud outbound requires a CommandDTO")
    if (
        command.account_ref != connection.account_id.external_ref
        or command.connection_ref != connection.external_ref
        or command.command_type != command_type
        or command.own_protocol_participant
        or command.target_protocol_participant
    ):
        raise AdapterError("WhatsApp Cloud command scope is invalid")


def _send_payload(connection, command):
    _common_scope(connection, command, "send_message")
    message = command.message
    if (
        not message
        or message.structured_content
        or command.options
        or command.extensions
    ):
        raise AdapterError("WhatsApp Cloud sends direct text or one media object")
    expected_client_id = derive_client_message_id(command.command_id)
    if (
        command.client_message_id != expected_client_id
        or message.client_message_id != expected_client_id
    ):
        raise AdapterError("WhatsApp Cloud correlation ID is inconsistent")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
    }
    payload.update(_recipient(command))
    _check_waba_scope(connection, command)
    if message.media:
        media = message.media[0]
        payload["type"] = media.kind
        payload[media.kind] = {}
    else:
        text = message.text
        if (
            message.content_type != "text"
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise AdapterError("WhatsApp Cloud outbound text is empty")
        if len(text) > _MAX_TEXT_CHARACTERS:
            raise AdapterError("WhatsApp Cloud outbound text exceeds 4096 characters")
        if any(ord(character) == 0 for character in text):
            raise AdapterError("WhatsApp Cloud outbound text contains invalid data")
        payload["type"] = "text"
        payload["text"] = {"body": text}
    reply_id = _reply_message_id(command)
    if reply_id:
        payload["context"] = {"message_id": reply_id}
    payload["biz_opaque_callback_data"] = expected_client_id
    return payload


def _media_payload(payload, media, descriptor, caption, media_id):
    body = {"id": media_id}
    if caption:
        body["caption"] = caption
    if media.kind == "document" and descriptor.get("document_name"):
        body["filename"] = descriptor["document_name"]
    payload[media.kind] = body
    return payload


def _react_payload(connection, command):
    _common_scope(connection, command, "react")
    options = command.options or {}
    if set(options) - {
        "target_external_message_id",
        "target_from_me",
        "target_participant",
        "emoji",
        "operation",
    }:
        raise AdapterError("WhatsApp Cloud reaction options are unsupported")
    target_id = options.get("target_external_message_id")
    operation = options.get("operation") or "add"
    emoji = options.get("emoji") or ""
    if not _valid_message_id(target_id) or operation not in ("add", "remove"):
        raise AdapterError("WhatsApp Cloud reaction target is invalid")
    if operation == "add" and (not isinstance(emoji, str) or not emoji):
        raise AdapterError("WhatsApp Cloud reaction requires an emoji")
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual"}
    payload.update(_recipient(command))
    _check_waba_scope(connection, command)
    payload["type"] = "reaction"
    payload["reaction"] = {
        "message_id": target_id,
        # WhatsApp removes a reaction with an empty emoji.
        "emoji": emoji if operation == "add" else "",
    }
    return payload


def _endpoint(connection):
    phone_number_id, _waba, version = _route_values(connection)
    return phone_number_id, "/%s/%s/messages" % (version, phone_number_id)


def _window_policy(latest):
    return {
        "response_window": "standard_24h",
        "last_inbound_at": fields.Datetime.to_string(latest),
        "expires_at": fields.Datetime.to_string(latest + RESPONSE_WINDOW),
    }


def _read_targets(env, connection, command, *, now=None):
    """Return the newest read target of one batch, enforcing the 30-day window."""

    _common_scope(connection, command, "mark_read")
    options = command.options or {}
    raw_ids = options.get("external_message_ids")
    if set(options) != {"external_message_ids"} or not isinstance(raw_ids, list):
        raise AdapterError("WhatsApp Cloud read receipt options are invalid")
    if not raw_ids or len(raw_ids) > _MAX_READ_IDS:
        raise AdapterError("WhatsApp Cloud read receipt batch is invalid")
    message_ids = list(
        dict.fromkeys(value for value in raw_ids if _valid_message_id(value))
    )
    if len(message_ids) != len(raw_ids):
        raise AdapterError("WhatsApp Cloud read receipt IDs are invalid")
    channel_binding = _direct_channel_binding(env, connection, command)
    targets = (
        env["contact.center.message.binding"]
        .sudo()
        .search(
            [
                ("channel_binding_id", "=", channel_binding.id),
                ("provider_connection_id", "=", connection.id),
                ("direction", "=", "inbound"),
                ("external_message_id", "in", message_ids),
            ]
        )
    )
    # Control cards carry local IDs; only provider messages can be marked read.
    targets = targets.filtered(
        lambda target: _WAMID_RE.fullmatch(target.external_message_id or "")
    )
    if not targets:
        return None, message_ids
    newest = max(
        targets,
        key=lambda target: (
            _utc_naive(target.message_id.date) or datetime.datetime.min,
            target.message_id.id,
        ),
    )
    now = _utc_naive(now or fields.Datetime.now())
    newest_at = _utc_naive(newest.message_id.date)
    if not newest_at or newest_at < now - MARK_READ_WINDOW:
        raise AdapterError("WhatsApp read receipts are accepted only for 30 days")
    return newest, message_ids


def prepare_request_snapshot(env, connection, command):
    _active_route(connection)
    phone_number_id, endpoint = _endpoint(connection)
    if command.command_type == "mark_read":
        newest, message_ids = _read_targets(env, connection, command)
        return {
            "provider": "whatsapp_cloud",
            "provider_version": connection.sudo().wa_graph_version,
            "method": "POST",
            "endpoint": endpoint,
            "payload": {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": newest.external_message_id if newest else "",
            },
            "policy": {
                "read_receipt_window": "30d",
                "covered_message_ids": message_ids,
                "skipped": not newest,
            },
        }
    if command.command_type == "react":
        payload = _react_payload(connection, command)
    elif command.command_type == "send_message":
        payload = _send_payload(connection, command)
    else:
        raise AdapterError("WhatsApp Cloud command is not implemented")
    latest = latest_eligible_inbound_at(env, connection, command)
    snapshot = {
        "provider": "whatsapp_cloud",
        "provider_version": connection.sudo().wa_graph_version,
        "method": "POST",
        "endpoint": endpoint,
        "payload": payload,
        "policy": _window_policy(latest),
    }
    if command.command_type == "send_message" and command.message.media:
        media, _content, descriptor = outbound_media_content(env, connection, command)
        # The provider media ID is allocated only during dispatch. Record the
        # upload intent without bytes or any served URL.
        _media_payload(
            payload,
            media,
            descriptor,
            command.message.text,
            {"from_private_upload": descriptor},
        )
        snapshot["upload"] = {
            "method": "POST",
            "endpoint": "/%s/%s/media"
            % (connection.sudo().wa_graph_version, phone_number_id),
            "file": descriptor,
        }
    return snapshot


def _message_id(response):
    messages = response.get("messages") if isinstance(response, dict) else None
    first = messages[0] if isinstance(messages, list) and messages else None
    message_id = first.get("id") if isinstance(first, dict) else None
    return message_id if _valid_message_id(message_id) else ""


def _safe_response(response, runtime, phone_number_id, message_id):
    contacts = response.get("contacts") if isinstance(response, dict) else None
    first = contacts[0] if isinstance(contacts, list) and contacts else {}
    first = first if isinstance(first, dict) else {}
    messages = response.get("messages") if isinstance(response, dict) else None
    status = (
        messages[0].get("message_status")
        if isinstance(messages, list) and messages and isinstance(messages[0], dict)
        else ""
    )
    return {
        "message_id": message_id,
        "message_status": status if isinstance(status, str) else "",
        "contact_wa_id": first.get("wa_id")
        if isinstance(first.get("wa_id"), str)
        else "",
        "graph_version": runtime.graph_version,
        "phone_number_id": phone_number_id,
    }


def _post_message(runtime, token, phone_number_id, payload):
    response = graph_send(
        runtime,
        token,
        "%s/messages" % phone_number_id,
        payload,
        max_response_bytes=_MAX_RESPONSE_BYTES,
    )
    message_id = _message_id(response)
    if not message_id:
        # Meta may have accepted the message: never classify as a refusal.
        raise AmbiguousTimeoutError("WhatsApp accepted the send without a message ID")
    return response, message_id


def _execute_send(env, connection, command):
    _active_route(connection)
    runtime, token, _revision = resolve_graph_runtime(connection)
    payload = _send_payload(connection, command)
    phone_number_id, _endpoint_path = _endpoint(connection)
    # Re-evaluate immediately before crossing the network boundary.
    latest_eligible_inbound_at(env, connection, command)
    if command.message.media:
        media, content, descriptor = outbound_media_content(env, connection, command)
        media_id = upload_media(
            runtime, token, phone_number_id, media, content, descriptor
        )
        _media_payload(payload, media, descriptor, command.message.text, media_id)
        _active_route(connection)
        latest_eligible_inbound_at(env, connection, command)
    response, message_id = _post_message(runtime, token, phone_number_id, payload)
    return AdapterResult.success(
        external_message_id=message_id,
        provider_response=_safe_response(
            response, runtime, phone_number_id, message_id
        ),
    )


def _execute_react(env, connection, command):
    _active_route(connection)
    runtime, token, _revision = resolve_graph_runtime(connection)
    payload = _react_payload(connection, command)
    phone_number_id, _endpoint_path = _endpoint(connection)
    latest_eligible_inbound_at(env, connection, command)
    response, message_id = _post_message(runtime, token, phone_number_id, payload)
    return AdapterResult.success(
        provider_response=_safe_response(response, runtime, phone_number_id, message_id)
    )


def _execute_mark_read(env, connection, command):
    _active_route(connection)
    newest, message_ids = _read_targets(env, connection, command)
    if not newest:
        return AdapterResult.success(
            provider_response={"skipped": True, "covered_message_ids": message_ids}
        )
    runtime, token, _revision = resolve_graph_runtime(connection)
    phone_number_id, _endpoint_path = _endpoint(connection)
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        # WhatsApp marks the earlier messages of the conversation as read too.
        "message_id": newest.external_message_id,
    }
    try:
        response = graph_send(
            runtime,
            token,
            "%s/messages" % phone_number_id,
            payload,
            max_response_bytes=16 * 1024,
        )
    except AmbiguousTimeoutError as error:
        # Marking as read is idempotent: an unknown outcome is safely retried.
        raise TransientAdapterError("WhatsApp read receipt needs a retry") from error
    if not isinstance(response, dict) or response.get("success") is not True:
        raise TransientAdapterError("WhatsApp read receipt returned no success")
    return AdapterResult.success(
        provider_response={
            "success": True,
            "message_id": newest.external_message_id,
            "covered_message_ids": message_ids,
        }
    )


def execute_command(env, connection, command):
    if command.command_type == "send_message":
        return _execute_send(env, connection, command)
    if command.command_type == "react":
        return _execute_react(env, connection, command)
    if command.command_type == "mark_read":
        return _execute_mark_read(env, connection, command)
    raise AdapterError("WhatsApp Cloud command is not implemented")
