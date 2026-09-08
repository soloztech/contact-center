import hashlib
import json
import logging

from psycopg2 import errorcodes
from psycopg2.errors import SerializationFailure, UniqueViolation
from werkzeug.wrappers import Response

from odoo import fields, http
from odoo.http import request

from ..services.adapter import (
    WUZAPI_HUMAN_MESSAGE_WRAPPER_FIELDS,
    WUZAPI_LIFECYCLE_EVENT_STATES,
    WUZAPI_LIFECYCLE_EVENT_TYPES,
    WUZAPI_VERSION,
)

_logger = logging.getLogger(__name__)

MAX_WEBHOOK_BODY_BYTES = 1024 * 1024


class _WebhookSerializationFailure(SerializationFailure):
    """Request a fresh transaction through Odoo's normal concurrency retry."""

    @property
    def pgcode(self):
        return errorcodes.SERIALIZATION_FAILURE


_DROP = object()
_PRIMARY_MEDIA_FIELDS = (
    "imageMessage",
    "audioMessage",
    "videoMessage",
    "documentMessage",
    "ptvMessage",
    "stickerMessage",
)
_DROP_WHOLE_KEYS = {
    "accountencryptionattestation",
    "appstatesynckeyshare",
    "associatedprimaryidentitykey",
    "axolotlsenderkeydistributionmessage",
    "botmessagesecret",
    "enccommentmessage",
    "encreactionmessage",
    "fastratchetkeysenderkeydistributionmessage",
    "messagesecret",
    "messagedistributionmessage",
    "paddingbytes",
    "placeholdermessage",
    "rawmessage",
    "secretencryptedmessage",
    "senderkeydistributionmessage",
    "sourcewebmsg",
    "stickersyncrmrmessage",
    "teebotmetadata",
}


def _reject_nonstandard_json_constant(_value):
    """Reject NaN/Infinity, which JSON permits neither on the wire nor in PostgreSQL."""

    raise ValueError("non-standard JSON numeric constant")


_CALL_EVENT_TYPES = {"calloffer", "callaccept", "callterminate"}
_ACTIVE_LIFECYCLE_INBOX_STATES = {"pending", "processing", "retry"}


def _normalized_key(value):
    return "".join(character for character in str(value).lower() if character.isalnum())


def _lookup_item(values, field_name):
    """Return the exact item selected by the adapter's normalized lookup."""

    if not isinstance(values, dict):
        return None, None
    if field_name in values:
        return field_name, values[field_name]
    expected = _normalized_key(field_name)
    for key, value in values.items():
        if _normalized_key(key) == expected:
            return key, value
    return None, None


def _lookup_value(values, field_name):
    return _lookup_item(values, field_name)[1]


def _primary_media_key(envelope):
    """Return the sole mediaKey item the adapter can project for this event."""

    event = _lookup_value(envelope, "event")
    message = _lookup_value(event, "Message")
    if not isinstance(message, dict):
        return None

    current = message
    associated_child_seen = False
    for depth in range(9):
        nested_message = None
        selected_wrapper = ""
        for field_name in WUZAPI_HUMAN_MESSAGE_WRAPPER_FIELDS:
            wrapper = _lookup_value(current, field_name)
            nested = _lookup_value(wrapper, "message")
            if isinstance(nested, dict):
                nested_message = nested
                selected_wrapper = field_name
                break
        if nested_message is None:
            break
        if depth == 8:
            return None
        if selected_wrapper == "associatedChildMessage":
            if associated_child_seen:
                return None
            associated_child_seen = True
        current = nested_message

    # Keep these priorities aligned with services.adapter._message_content.
    if isinstance(_lookup_value(current, "conversation"), str):
        return None
    extended = _lookup_value(current, "extendedTextMessage")
    if isinstance(_lookup_value(extended, "text"), str):
        return None

    selected_media = None
    for field_name in _PRIMARY_MEDIA_FIELDS:
        provider_media = _lookup_value(current, field_name)
        if isinstance(provider_media, dict):
            selected_media = provider_media
            break
    if selected_media is None:
        return None

    key, media_key = _lookup_item(selected_media, "mediaKey")
    if not isinstance(media_key, str) or not media_key.strip():
        return None
    return selected_media, key


def _sanitize_webhook_value(value, primary_media_key=None, depth=0):
    if depth > 32:
        raise ValueError("webhook JSON nesting is too deep")
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = _normalized_key(key)
            if normalized in _DROP_WHOLE_KEYS:
                continue
            if (
                normalized == "base64"
                or normalized.endswith("base64")
                or normalized.endswith("b64")
                or normalized.endswith("sidecar")
                or "thumbnail" in normalized
                or normalized == "waveform"
            ):
                continue
            # Preserve only the exact key selected by the same wrapper/content
            # priorities as the adapter. Ignored media siblings and lookalikes
            # must not add redundant WhatsApp key material to the Inbox ledger.
            if normalized == "mediakey" and (
                primary_media_key is None
                or value is not primary_media_key[0]
                or key != primary_media_key[1]
            ):
                continue
            sanitized = _sanitize_webhook_value(item, primary_media_key, depth + 1)
            if sanitized is not _DROP:
                result[key] = sanitized
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            sanitized = _sanitize_webhook_value(item, primary_media_key, depth + 1)
            if sanitized is not _DROP:
                result.append(sanitized)
        return result
    if isinstance(value, str) and value.lstrip().lower().startswith("data:"):
        return _DROP
    return value


def sanitize_webhook_envelope(envelope):
    """Remove inline binaries and redundant WhatsApp crypto material."""

    if not isinstance(envelope, dict):
        raise ValueError("webhook envelope must be an object")
    sanitized = _sanitize_webhook_value(envelope, _primary_media_key(envelope))
    event_type = next(
        (value for key, value in sanitized.items() if _normalized_key(key) == "type"),
        "",
    )
    if _normalized_key(event_type) in _CALL_EVENT_TYPES:
        event = next(
            (
                value
                for key, value in sanitized.items()
                if _normalized_key(key) == "event" and isinstance(value, dict)
            ),
            None,
        )
        if event is not None:
            # whatsmeow's call event Data is a binary protocol node. It is not
            # needed for correlation and may serialize bytes as base64, so drop
            # the whole subtree before the durable Inbox ledger is written.
            for key in tuple(event):
                if _normalized_key(key) == "data":
                    del event[key]
    return sanitized


def _json_response(payload, status):
    return Response(
        json.dumps(payload, separators=(",", ":")),
        status=status,
        content_type="application/json; charset=utf-8",
    )


def _connection_ingress_ignore_reason(connection):
    if not connection.active or not connection.account_id.active:
        return "inactive"
    # Account access readiness is deliberately handled by the durable ledger
    # below. A correctly authenticated callback for an unscoped inbox must be
    # retained as blocked evidence, while a retired transport is acknowledged
    # without creating a second logical stream.
    if connection.role != "primary" or not connection.inbound_active:
        return "inactive_transport"
    return ""


def _guided_onboarding_ingress_block_reason(connection, ignore_reason):
    """Buffer a first inbox after pairing but before its explicit activation."""

    if (
        ignore_reason != "inactive_transport"
        or not connection.onboarding_ref
        or connection.onboarding_activated_at
        or connection.role not in ("migration", "standby")
        or connection.inbound_active
        or connection.outbound_active
    ):
        return ""
    connection.env.cr.execute(
        """
        SELECT 1
          FROM contact_center_provider_connection
         WHERE account_id = %s
           AND active IS TRUE
           AND role = 'primary'
         LIMIT 1
        """,
        [connection.account_id.id],
    )
    return "" if connection.env.cr.fetchone() else "onboarding_not_activated"


def _authenticate_connection_webhook(connection, adapter, headers, body):
    """Authenticate with the bounded current/pending/draining keyring."""

    previous_secret = False
    previous_valid_until = fields.Datetime.to_datetime(
        connection.wuzapi_hmac_previous_valid_until
    )
    if (
        connection.wuzapi_hmac_previous_secret
        and previous_valid_until
        and previous_valid_until > fields.Datetime.to_datetime(fields.Datetime.now())
    ):
        previous_secret = connection.wuzapi_hmac_previous_secret
    return adapter.authenticate_webhook_secrets(
        (
            connection.wuzapi_hmac_secret,
            connection.wuzapi_hmac_pending_secret,
            previous_secret,
        ),
        headers,
        body,
    )


def _locked_ingress_response(connection, adapter, headers, body):
    """Return ``(response, block_reason)`` after serialized ingress admission."""

    connection = connection._contact_center_lock_ingress_admission()
    if not connection:
        return _json_response({"error": "not_found"}, 404), ""
    connection.ensure_one()
    connection.invalidate_recordset(
        [
            "active",
            "role",
            "inbound_active",
            "account_id",
            "wuzapi_hmac_secret",
            "wuzapi_hmac_pending_secret",
            "wuzapi_hmac_rotation_state",
            "wuzapi_hmac_previous_secret",
            "wuzapi_hmac_previous_valid_until",
            "onboarding_ref",
            "onboarding_activated_at",
            "wuzapi_onboarding_state",
        ]
    )
    connection.account_id.invalidate_recordset(
        ["active", "access_user_ids", "access_team_ids"]
    )
    if connection.account_id.access_team_ids:
        connection.account_id.access_team_ids.invalidate_recordset(
            ["active", "agent_ids", "supervisor_ids"]
        )
    # The first signature check protects the account-level lock from public
    # unauthenticated contention. Re-check against the row locked above so a
    # callback signed with the retired secret cannot cross a concurrent secret
    # rotation that committed while this request was parsing.
    if not _authenticate_connection_webhook(connection, adapter, headers, body):
        return _json_response({"error": "invalid_signature"}, 401), ""
    ignore_reason = _connection_ingress_ignore_reason(connection)
    block_reason = _guided_onboarding_ingress_block_reason(connection, ignore_reason)
    if block_reason:
        return None, block_reason
    if ignore_reason:
        # A correctly signed archived or secondary transport may keep retrying
        # callbacks. Acknowledge it without projecting a second logical inbox.
        return _json_response({"accepted": True, "ignored": ignore_reason}, 200), ""
    return None, ""


def _read_bounded_body(http_request, maximum_bytes):
    """Read a public body with a hard limit on Odoo 16's Werkzeug API."""

    # Odoo's HTTP dispatcher may already have cached JSON before entering a
    # ``type='http'`` controller.  Reuse only the bounded prefix in that case.
    cached_body = getattr(http_request, "_cached_data", None)
    if cached_body is not None:
        return cached_body[: maximum_bytes + 1]

    # With a verified Content-Length, Werkzeug's own reader is bounded by the
    # WSGI LimitedStream. Keep its request-local cache: Odoo retries the entire
    # controller on a transaction conflict, after the input stream was consumed.
    content_length = http_request.content_length
    if content_length is not None:
        return http_request.get_data(cache=True)

    # A length-less/chunked request must not reach unbounded ``get_data``.
    input_stream = http_request.environ.get("wsgi.input")
    if input_stream is None:
        raise ValueError("request body stream is unavailable")
    body = input_stream.read(maximum_bytes + 1)
    http_request._cached_data = body
    return body


def _inbox_dedupe_key(inbox_model, connection, envelope, digest):
    """Deduplicate lifecycle retries without suppressing a later state cycle."""

    event_type = envelope.get("type")
    if event_type not in WUZAPI_LIFECYCLE_EVENT_TYPES:
        return "wuzapi:json:sha256:%s" % digest
    last_lifecycle = inbox_model.search(
        [
            ("provider_connection_id", "=", connection.id),
            ("inbox_dedupe_key", "=like", "wuzapi:lifecycle:%"),
        ],
        order="id desc",
        limit=1,
    )
    last_metadata = (last_lifecycle.metadata_json or {}) if last_lifecycle else {}
    if last_lifecycle and last_metadata.get("event_type") == event_type:
        if last_lifecycle.state in _ACTIVE_LIFECYCLE_INBOX_STATES:
            return last_lifecycle.inbox_dedupe_key
        target_state = WUZAPI_LIFECYCLE_EVENT_STATES[event_type]
        canonical_target = (
            "degraded" if target_state in ("paused", "error") else target_state
        )
        health_superseded = bool(
            connection.last_state_source == "health_job"
            and connection.state != canonical_target
            # Datetime cannot order a health observation against inbox
            # create_date reliably because only the latter may keep
            # microseconds. The health-side cursor snapshots the last provider
            # event already accepted when that health state was recorded.
            and connection.last_health_state_inbox_event_id >= last_lifecycle.id
        )
        if not health_superseded:
            return last_lifecycle.inbox_dedupe_key
        health_revision = hashlib.sha256(
            (
                "%s|%s|%s|%s"
                % (
                    last_lifecycle.id,
                    connection.state,
                    connection.last_state_source,
                    connection.last_state_observed_at.isoformat(),
                )
            ).encode("ascii")
        ).hexdigest()[:16]
        return "wuzapi:lifecycle:%s:after:%s:health:%s:sha256:%s" % (
            event_type,
            last_lifecycle.id,
            health_revision,
            digest,
        )
    return "wuzapi:lifecycle:%s:after:%s:sha256:%s" % (
        event_type,
        last_lifecycle.id or 0,
        digest,
    )


def _find_or_create_inbox(inbox_model, domain, values):
    inbox = inbox_model.search(domain, limit=1)
    if inbox:
        return inbox, True
    try:
        with inbox_model.env.cr.savepoint():
            return inbox_model.create(values), False
    except UniqueViolation as error:
        if error.diag.constraint_name != (
            "contact_center_inbox_event_connection_dedupe_unique"
        ):
            raise
        # Under REPEATABLE READ, a concurrent winner may remain invisible even
        # after its insert has committed. Searching this snapshot again cannot
        # resolve the duplicate; Odoo must restart the whole transaction.
        raise _WebhookSerializationFailure(
            "Concurrent WuzAPI webhook requires a fresh snapshot"
        ) from None


def _inbox_ledger_values(
    connection,
    envelope,
    persisted_envelope,
    body,
    digest,
    dedupe_key,
    ingress_block_reason,
):
    """Build one sanitized ledger row, including fail-closed routing state."""

    values = {
        "provider_connection_id": connection.id,
        "inbox_dedupe_key": dedupe_key,
        "provider_schema_version": WUZAPI_VERSION,
        "raw_envelope_json": persisted_envelope,
        "metadata_json": {
            "content_sha256": digest,
            "content_length": len(body),
            "event_type": str(envelope.get("type") or "")[:80],
            "instance_name": str(envelope.get("instanceName") or "")[:120],
            "user_id": str(envelope.get("userID") or "")[:120],
            "raw_envelope_sanitized": True,
        },
    }
    account_ready = connection.account_id._contact_center_access_is_ready()
    if ingress_block_reason:
        values.update(
            {
                "state": "blocked",
                "last_error_class": "OnboardingNotActivated",
                "last_error_message": (
                    "The paired provider is waiting for explicit inbox activation."
                ),
            }
        )
        values["metadata_json"].update(
            {
                "blocked_reason": ingress_block_reason,
                "onboarding_ref": connection.onboarding_ref,
            }
        )
    elif not account_ready:
        values.update(
            {
                "state": "blocked",
                "last_error_class": "AccountNotReady",
                "last_error_message": (
                    "The Contact Center account has no valid owner or access team."
                ),
            }
        )
        values["metadata_json"]["blocked_reason"] = "account_not_ready"
        _logger.warning(
            "WuzAPI webhook persisted as blocked for unassigned Contact Center "
            "account %s",
            connection.account_id.id,
        )
    return values


def _conversation_ingress_response(connection, adapter, headers, body, envelope):
    response, block_reason = _locked_ingress_response(
        connection, adapter, headers, body
    )
    if response is None and request.env[
        "contact.center.conversation.ignore"
    ]._ignored_envelope(connection, envelope):
        response = _json_response({"accepted": True, "ignored": True}, 200)
    return response, block_reason


class WuzapiWebhookController(http.Controller):
    @http.route(
        "/contact-center/webhook/wuzapi/<string:webhook_key>",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def webhook(self, webhook_key, **_kwargs):
        connection = (
            request.env["contact.center.provider.connection"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("adapter_key", "=", "wuzapi"),
                    ("wuzapi_webhook_key", "=", webhook_key),
                ],
                limit=1,
            )
        )
        if not connection:
            return _json_response({"error": "not_found"}, 404)
        if request.httprequest.mimetype != "application/json":
            return _json_response({"error": "unsupported_media_type"}, 415)
        content_length = request.httprequest.content_length
        if content_length is not None and content_length > MAX_WEBHOOK_BODY_BYTES:
            return _json_response({"error": "payload_too_large"}, 413)
        # Enforce the same limit for chunked requests that omit Content-Length;
        # ``get_data`` would otherwise materialize an unbounded public body.
        try:
            body = _read_bounded_body(
                request.httprequest,
                MAX_WEBHOOK_BODY_BYTES,
            )
        except (AttributeError, OSError, ValueError):
            return _json_response({"error": "invalid_request_body"}, 400)
        if len(body) > MAX_WEBHOOK_BODY_BYTES:
            return _json_response({"error": "payload_too_large"}, 413)
        if not body:
            return _json_response({"error": "empty_payload"}, 400)
        adapter = connection.get_adapter()
        headers = request.httprequest.headers
        if not _authenticate_connection_webhook(connection, adapter, headers, body):
            return _json_response({"error": "invalid_signature"}, 401)
        try:
            envelope = json.loads(
                body.decode("utf-8"),
                parse_constant=_reject_nonstandard_json_constant,
            )
        except (ValueError, RecursionError):
            return _json_response({"error": "invalid_json"}, 400)
        if not isinstance(envelope, dict):
            return _json_response({"error": "invalid_envelope"}, 400)
        # Linearize webhook admission with the administrator's primary switch.
        # Authentication and bounded parsing happen first, so an unauthenticated
        # caller cannot take the topology lock. Once acquired, a callback is
        # unambiguously before or after the cutover and never slips through the
        # retired transport on stale ORM cache state.
        ingress_response, ingress_block_reason = _conversation_ingress_response(
            connection, adapter, headers, body, envelope
        )
        if ingress_response is not None:
            return ingress_response
        try:
            persisted_envelope = sanitize_webhook_envelope(envelope)
        except ValueError:
            return _json_response({"error": "invalid_envelope"}, 400)

        digest = hashlib.sha256(body).hexdigest()
        inbox_model = request.env["contact.center.inbox.event"].sudo()
        dedupe_key = _inbox_dedupe_key(
            inbox_model, connection, persisted_envelope, digest
        )
        domain = [
            ("provider_connection_id", "=", connection.id),
            ("inbox_dedupe_key", "=", dedupe_key),
        ]
        values = _inbox_ledger_values(
            connection,
            envelope,
            persisted_envelope,
            body,
            digest,
            dedupe_key,
            ingress_block_reason,
        )
        inbox, duplicate = _find_or_create_inbox(inbox_model, domain, values)
        if not duplicate and ingress_block_reason == "onboarding_not_activated":
            connection._contact_center_record_onboarding_ingress(
                connection.onboarding_ref
            )
        return _json_response(
            {
                "accepted": True,
                "duplicate": duplicate,
                "event_id": inbox.id,
                "blocked": inbox.state == "blocked",
            },
            200 if duplicate else 202,
        )
