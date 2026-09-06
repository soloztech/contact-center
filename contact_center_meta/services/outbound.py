import datetime
import re

from odoo import fields

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    ProviderPausedError,
)
from odoo.addons.contact_center_base.services.dto import AdapterResult, CommandDTO

from .api import graph_request, resolve_graph_runtime
from .outbound_media import private_media_content, upload_private_media

_GRAPH_ID_PATTERN = re.compile(r"^[0-9]{5,40}$")
_RESPONSE_WINDOW = datetime.timedelta(hours=24)
_MAX_FUTURE_CLOCK_SKEW = datetime.timedelta(minutes=5)
_MAX_RESPONSE_BYTES = 64 * 1024
_PLATFORM_CONTRACTS = {
    "messenger_page": {
        "platform": "messenger",
        "target_namespace": "meta.messenger.psid",
        "maximum_text_bytes": None,
        "maximum_text_characters": 2000,
        "messaging_type": "RESPONSE",
    },
    "instagram_page_linked": {
        "platform": "instagram",
        "target_namespace": "meta.instagram.igsid",
        # Meta's references have disagreed on characters versus bytes.  Keep the
        # first outbound phase at the stricter boundary until the active contract
        # is demonstrated with fixtures from the dedicated App.
        "maximum_text_bytes": 1000,
        "maximum_text_characters": None,
        "messaging_type": None,
    },
}


def _utc_naive(value):
    value = fields.Datetime.to_datetime(value)
    if not value:
        return None
    if value.tzinfo:
        return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value


def _connection_contract(connection):
    contract = _PLATFORM_CONTRACTS.get(connection.meta_transport_mode)
    if (
        not contract
        or connection.account_id.platform != contract["platform"]
        or not _GRAPH_ID_PATTERN.fullmatch(connection.meta_target_asset_id or "")
    ):
        raise AdapterError("Meta outbound connection route is invalid")
    return contract


def _page_send_route(connection, contract):
    """Return the Page-ID Send API route for both page-linked transports.

    ``meta_target_asset_id`` remains the webhook/recipient asset: it is the Page
    for Messenger and the Instagram professional account for Instagram.  Meta's
    page-linked Send API is different: both transports are sent through the Page
    that owns the Page access token.
    """

    page = connection.sudo().meta_webhook_page_id
    page_id = page.external_page_id if page else ""
    target_asset_id = connection.meta_target_asset_id or ""
    if (
        not page
        or not _GRAPH_ID_PATTERN.fullmatch(page_id or "")
        or not _GRAPH_ID_PATTERN.fullmatch(target_asset_id)
    ):
        raise AdapterError("Meta outbound Page route is invalid")
    if contract["platform"] == "messenger" and target_asset_id != page_id:
        raise AdapterError("Meta Messenger outbound route does not match the Page")
    if contract["platform"] == "instagram" and target_asset_id == page_id:
        raise AdapterError(
            "Meta Instagram outbound route must use a linked Instagram asset"
        )
    return page_id


def _active_route(connection):
    if (
        not connection.active
        or not connection.account_id.active
        or not connection.sudo().meta_webhook_asset_id
        or not connection._meta_outbound_is_ready()
    ):
        raise ProviderPausedError("Meta outbound route is unavailable")
    return connection.sudo().meta_webhook_page_id


def _direct_channel_binding(env, connection, command):
    if command.conversation.conversation_type != "direct":
        raise AdapterError("Meta outbound supports direct conversations only")
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
        raise AdapterError("Meta outbound conversation evidence is ambiguous")
    return bindings


def _latest_eligible_inbound_at(env, connection, command, *, now=None):
    """Return authenticated user-message evidence for the standard response window.

    Archiving a technical connection must not erase a still-valid provider window.
    Evidence is scoped to the canonical account/conversation and to inbound messages
    observed through any Meta connection for that same account.
    """

    binding = _direct_channel_binding(env, connection, command)
    # The response-window lookup intentionally uses SQL so it can aggregate
    # evidence across replacement Meta connections.  Flush every field read by
    # that query first: ORM writes in the current transaction (including tests
    # and a future same-transaction caller) must never leave stale evidence in
    # PostgreSQL and accidentally open the window.
    env["contact.center.message.binding"].flush_model(
        [
            "channel_binding_id",
            "direction",
            "origin",
            "provider_connection_id",
        ]
    )
    env["mail.message"].flush_model(["date"])
    env["contact.center.provider.connection"].flush_model(["account_id", "adapter_key"])
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
           AND provider_connection.account_id = %s
           AND provider_connection.adapter_key = 'meta'
        """,
        [binding.id, connection.account_id.id],
    )
    latest = _utc_naive(env.cr.fetchone()[0])
    now = _utc_naive(now or fields.Datetime.now())
    if not latest:
        raise AdapterError(
            "Meta outbound requires an inbound user message in the last 24 hours"
        )
    if latest > now + _MAX_FUTURE_CLOCK_SKEW:
        raise AdapterError("Meta outbound window evidence has a future timestamp")
    if latest < now - _RESPONSE_WINDOW:
        raise AdapterError("Meta standard 24-hour response window is closed")
    return latest


def _target_id(connection, command, contract):
    target = command.target_address
    if (
        not target
        or target.namespace != contract["target_namespace"]
        or target.role not in ("primary", "routing")
        or not _GRAPH_ID_PATTERN.fullmatch(target.value_normalized or "")
        or target.value != target.value_normalized
        or command.conversation_ref != target.value_normalized
    ):
        raise AdapterError("Meta outbound target is invalid")
    supplied_addresses = {
        (address.namespace, address.value_normalized)
        for address in command.conversation.addresses
    }
    if (target.namespace, target.value_normalized) not in supplied_addresses:
        raise AdapterError("Meta outbound target is absent from the conversation")
    return target.value_normalized


def _reply_message_id(command):
    reply_reference = command.reply_to
    message_reply_id = command.message.reply_to_external_id
    message_snapshot = command.message.protocol_snapshot
    if not reply_reference:
        if message_reply_id or message_snapshot:
            raise AdapterError("Meta outbound reply evidence is incomplete")
        return ""
    if set(reply_reference) - {"external_message_id", "protocol_snapshot"}:
        raise AdapterError("Meta outbound reply evidence has unsupported fields")
    reference_reply_id = reply_reference.get("external_message_id")
    reference_snapshot = reply_reference.get("protocol_snapshot", {})
    if (
        not isinstance(reference_reply_id, str)
        or not reference_reply_id.strip()
        or reference_reply_id != reference_reply_id.strip()
        or len(reference_reply_id) > 512
        or any(character.isspace() for character in reference_reply_id)
        or reference_reply_id != message_reply_id
        or not isinstance(reference_snapshot, dict)
        or reference_snapshot != message_snapshot
    ):
        raise AdapterError("Meta outbound reply evidence is inconsistent")
    return reference_reply_id


def _message_payload(connection, command):
    if not isinstance(command, CommandDTO):
        raise AdapterError("Meta outbound requires a CommandDTO")
    contract = _connection_contract(connection)
    if (
        command.account_ref != connection.account_id.external_ref
        or command.connection_ref != connection.external_ref
        or command.command_type != "send_message"
        or not command.message
        or command.message.structured_content
        or command.options
        or command.extensions
        or command.own_protocol_participant
        or command.target_protocol_participant
    ):
        raise AdapterError(
            "Meta outbound supports direct text messages or private media only"
        )
    if command.message.media:
        message = {
            "attachment": {
                "type": "file"
                if command.message.content_type == "document"
                else command.message.content_type,
                "payload": {},
            }
        }
    else:
        text = command.message.text
        if (
            command.message.content_type != "text"
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise AdapterError("Meta outbound text is empty")
        if any(ord(character) == 0 for character in text):
            raise AdapterError("Meta outbound text contains invalid control data")
        maximum_characters = contract["maximum_text_characters"]
        maximum_bytes = contract["maximum_text_bytes"]
        if maximum_characters and len(text) > maximum_characters:
            raise AdapterError("Meta outbound text exceeds the provider limit")
        if maximum_bytes and len(text.encode("utf-8")) > maximum_bytes:
            raise AdapterError("Meta outbound text exceeds the provider byte limit")
        message = {"text": text}
    target_id = _target_id(connection, command, contract)
    reply_message_id = _reply_message_id(command)
    payload = {"recipient": {"id": target_id}, "message": message}
    if contract["messaging_type"]:
        payload["messaging_type"] = contract["messaging_type"]
    if reply_message_id:
        payload["reply_to"] = {"mid": reply_message_id}
    return contract, target_id, payload


def prepare_send_request(env, connection, command, *, now=None):
    _active_route(connection)
    contract, _target_id_value, payload = _message_payload(connection, command)
    page_id = _page_send_route(connection, contract)
    latest = _latest_eligible_inbound_at(
        env,
        connection,
        command,
        now=now,
    )
    expires_at = latest + _RESPONSE_WINDOW
    snapshot = {
        "provider": "meta",
        "provider_version": connection.meta_api_app_id.graph_version,
        "method": "POST",
        "endpoint": "/%s/messages" % page_id,
        "payload": payload,
        "policy": {
            "response_window": "standard_24h",
            "last_inbound_at": fields.Datetime.to_string(latest),
            "expires_at": fields.Datetime.to_string(expires_at),
        },
    }
    if command.message.media:
        _media, _content, descriptor = private_media_content(env, connection, command)
        # The provider attachment ID is allocated only during dispatch. Record the
        # immutable upload intent without persisting bytes or a publicly served URL.
        payload["message"]["attachment"]["payload"]["attachment_id"] = {
            "from_private_upload": descriptor
        }
        snapshot["upload"] = {
            "method": "POST",
            "endpoint": "/%s/message_attachments" % page_id,
            "filedata": descriptor,
        }
        if contract["platform"] == "instagram":
            snapshot["upload"]["platform"] = "instagram"
    return snapshot


def execute_send_request(env, connection, command):
    _active_route(connection)
    runtime, page_token, _page_revision = resolve_graph_runtime(connection)
    contract, target_id, payload = _message_payload(connection, command)
    page_id = _page_send_route(connection, contract)
    # Re-evaluate immediately before crossing the network boundary.  A command
    # prepared just inside the window cannot be dispatched after it closes.
    _latest_eligible_inbound_at(env, connection, command)
    if command.message.media:
        media, content, descriptor = private_media_content(env, connection, command)
        attachment_id = upload_private_media(
            runtime, page_token, page_id, contract, media, content, descriptor
        )
        payload["message"]["attachment"]["payload"]["attachment_id"] = attachment_id
        _active_route(connection)
        _latest_eligible_inbound_at(env, connection, command)
    response = graph_request(
        runtime,
        page_token,
        "POST",
        "%s/messages" % page_id,
        json_data=payload,
        mutating=True,
        max_response_bytes=_MAX_RESPONSE_BYTES,
    )
    recipient_id = response.get("recipient_id") if isinstance(response, dict) else None
    message_id = response.get("message_id") if isinstance(response, dict) else None
    safe_response = {
        "recipient_id": recipient_id if isinstance(recipient_id, str) else "",
        "message_id": message_id if isinstance(message_id, str) else "",
        "graph_version": runtime.graph_version,
        "transport_mode": connection.meta_transport_mode,
    }
    if (
        recipient_id != target_id
        or not isinstance(message_id, str)
        or not message_id.strip()
        or len(message_id) > 512
        or any(character.isspace() for character in message_id)
    ):
        return AdapterResult(
            status="uncertain",
            error_code="invalid_success_correlation",
            error_message=(
                "Meta accepted the send without an exact recipient/message correlation"
            ),
            provider_response=safe_response,
        )
    return AdapterResult.success(
        external_message_id=message_id,
        provider_response=safe_response,
    )
