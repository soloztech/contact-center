from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    ProviderAdapter,
    ProviderPausedError,
    ProviderRateLimitError,
    TransientAdapterError,
    adapter_registry,
)
from odoo.addons.meta_api_base.services import (
    META_API_RUNTIME_CONTEXT_KEY,
    META_API_RUNTIME_TOKEN,
)
from odoo.addons.meta_api_base.services.credentials import MetaCredentialResolutionError
from odoo.addons.meta_api_base.services.signature import verify_signature

from .api import graph_request, resolve_graph_runtime
from .contracts import (
    MARK_READ_WINDOW,
    RESPONSE_WINDOW,
    WHATSAPP_CLOUD_ADAPTER_KEY,
    WHATSAPP_CLOUD_PROVIDER_SCHEMA_VERSION,
)
from .media import STICKER_LIMITS, download_media, media_capabilities, receive_limit
from .normalizer import (
    conversation_route,
    history_creative,
    normalize_whatsapp_cloud_event,
)
from .outbound import (
    derive_client_message_id,
    execute_command,
    prepare_request_snapshot,
)

HEALTH_OBSERVATION_CONTEXT_KEY = "contact_center_wac_health_observation"
_HEALTH_FIELDS = (
    "id,display_phone_number,verified_name,quality_rating,status,"
    "throughput,name_status,code_verification_status"
)
_CONNECTED_STATUSES = frozenset({"CONNECTED", "FLAGGED"})


def _connection(connection):
    connection.ensure_one()
    if (
        connection.adapter_key != WHATSAPP_CLOUD_ADAPTER_KEY
        or not connection.sudo().wa_webhook_asset_id
    ):
        raise AdapterError("WhatsApp Cloud connection has no business account asset")
    return connection


def _text(value, maximum=128):
    if not isinstance(value, str):
        return ""
    return "".join(character for character in value.strip() if ord(character) >= 32)[
        :maximum
    ]


@adapter_registry.register(
    WHATSAPP_CLOUD_ADAPTER_KEY, module="contact_center_whatsapp_cloud"
)
class WhatsAppCloudAdapter(ProviderAdapter):
    """WhatsApp Business Platform (Cloud API) over the shared Meta core."""

    key = WHATSAPP_CLOUD_ADAPTER_KEY
    display_name = "WhatsApp Cloud API"

    def authenticate_webhook(self, connection, headers, body):
        connection = _connection(connection)
        app = connection.sudo().wa_api_app_id
        try:
            runtime = app.with_context(
                **{META_API_RUNTIME_CONTEXT_KEY: META_API_RUNTIME_TOKEN}
            )._resolve_runtime(expected_revision=app.revision)
        except MetaCredentialResolutionError:
            return False
        return verify_signature(runtime.app_secret, headers, body)

    def conversation_route(self, connection, envelope):
        return conversation_route(_connection(connection), envelope)

    def normalize_event(self, connection, envelope):
        return normalize_whatsapp_cloud_event(_connection(connection), envelope)

    def derive_client_message_id(self, command_id):
        return derive_client_message_id(command_id)

    def execute_command(self, connection, command):
        return execute_command(self.env, _connection(connection), command)

    def prepare_request_snapshot(self, connection, command):
        return prepare_request_snapshot(self.env, _connection(connection), command)

    def get_capabilities(self, connection):
        connection = _connection(connection)
        sudo_connection = connection.sudo()
        return {
            "schema_version": "1.0",
            "send_message": True,
            "sender_signature": False,
            "mark_read": True,
            "reply": True,
            "react": True,
            "edit_message": False,
            "delete_message": False,
            "identity_avatar": False,
            "media": media_capabilities(),
            "conversation_types": {},
            "extensions": {
                "provider.whatsapp_cloud": {
                    "graph_api_version": sudo_connection.wa_graph_version,
                    "provider_schema_version": WHATSAPP_CLOUD_PROVIDER_SCHEMA_VERSION,
                    "window_hours": int(RESPONSE_WINDOW.total_seconds() // 3600),
                    "mark_read_window_days": MARK_READ_WINDOW.days,
                    "outbound_response_window": "standard_24h",
                    "outbound_templates": False,
                    "outbound_correlation": "biz_opaque_callback_data",
                    "inbound_delivery_ledger": "meta_webhook_base",
                    "inbound_media_max_bytes": {
                        kind: receive_limit(kind)
                        for kind in ("image", "audio", "video", "document")
                    },
                    "sticker_max_bytes": dict(STICKER_LIMITS),
                    "failed_status_ledger": (
                        "contact.center.whatsapp.cloud.delivery.failure"
                    ),
                }
            },
        }

    def get_health(self, connection):
        """Read the phone number itself; no observation is renewed otherwise."""

        connection = _connection(connection)
        if not connection._wac_runtime_topology_is_ready(require_subscription=False):
            return {"state": "degraded", "reason": "provider_paused"}
        sudo_connection = connection.sudo()
        phone_number_id = sudo_connection.wa_phone_number_id
        try:
            runtime, token, _revision = resolve_graph_runtime(connection)
            payload = graph_request(
                runtime,
                token,
                "GET",
                phone_number_id,
                params={"fields": _HEALTH_FIELDS},
                max_response_bytes=16 * 1024,
            )
        except ProviderRateLimitError as error:
            return {
                "state": "degraded",
                "reason": "rate_limited",
                "retry_after_seconds": getattr(error, "retry_after_seconds", 0) or 60,
            }
        except ProviderPausedError:
            # Missing, revoked or expired system-user token.
            return {
                "state": "authentication_required",
                "reason": "authentication_required",
            }
        except TransientAdapterError:
            return {"state": "disconnected", "reason": "unreachable"}
        if not isinstance(payload, dict):
            return {"state": "degraded", "reason": "invalid_response"}
        if str(payload.get("id") or "") != phone_number_id:
            return {
                "state": "degraded",
                "reason": "identity_mismatch",
                "identity_matches": False,
            }
        status = _text(payload.get("status")).upper()
        throughput = payload.get("throughput")
        observation = {
            "display_phone_number": _text(payload.get("display_phone_number"), 64),
            "verified_name": _text(payload.get("verified_name"), 256),
            "quality_rating": _text(payload.get("quality_rating"), 32).upper(),
            "status": status,
            "throughput_level": _text(
                throughput.get("level") if isinstance(throughput, dict) else "", 32
            ).upper(),
        }
        holder = connection.env.context.get(HEALTH_OBSERVATION_CONTEXT_KEY)
        if isinstance(holder, dict):
            holder.clear()
            holder.update(observation)
        if not status:
            return {
                "state": "degraded",
                "reason": "invalid_response",
                "identity_matches": True,
            }
        if status == "RATE_LIMITED":
            return {
                "state": "degraded",
                "reason": "rate_limited",
                "identity_matches": True,
            }
        if status not in _CONNECTED_STATUSES:
            return {
                "state": "degraded",
                "reason": "provider_paused",
                "identity_matches": True,
            }
        if not connection._wac_inbound_route_is_ready():
            return {
                "state": "degraded",
                "reason": "provider_paused",
                "identity_matches": True,
            }
        return {"state": "connected", "reason": "healthy", "identity_matches": True}

    def is_provider_read_ready(self, connection, purpose):
        connection = _connection(connection)
        if purpose != "media_download":
            return super().is_provider_read_ready(connection, purpose)
        return bool(
            connection._wac_runtime_topology_is_ready(require_subscription=False)
            and not connection.identity_mismatch_latched
        )

    def download_media(self, connection, media):
        return download_media(_connection(connection), media)

    def ad_origin_preview_from_history(self, payload, source_key):
        return history_creative(payload, source_key)
