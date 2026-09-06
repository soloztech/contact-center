import datetime

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    ProviderAdapter,
    ProviderPausedError,
    adapter_registry,
)
from odoo.addons.meta_api_base.services import (
    META_API_RUNTIME_CONTEXT_KEY,
    META_API_RUNTIME_TOKEN,
)
from odoo.addons.meta_api_base.services.credentials import MetaCredentialResolutionError
from odoo.addons.meta_api_base.services.signature import verify_signature

from .api import graph_debug_token, resolve_graph_runtime
from .contracts import META_PROVIDER_SCHEMA_VERSION
from .media import download_private_media, finalize_private_media
from .normalizer import normalize_meta_event
from .outbound import execute_send_request, prepare_send_request
from .outbound_media import media_capabilities
from .profile import fetch_identity_profile

_REQUIRED_SCOPES = {
    "messenger_page": frozenset({"pages_manage_metadata", "pages_messaging"}),
    "instagram_page_linked": frozenset(
        {
            "pages_manage_metadata",
            "instagram_basic",
            "instagram_manage_messages",
        }
    ),
}


def _connection(connection):
    connection.ensure_one()
    if connection.adapter_key != "meta" or not connection.sudo().meta_webhook_asset_id:
        raise AdapterError("Meta connection has no canonical messaging asset")
    return connection


def _epoch(value):
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        return datetime.datetime.utcfromtimestamp(value)
    except (OverflowError, OSError, ValueError):
        return None


def _token_observation(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise AdapterError("Meta token inspection response is invalid")
    scopes = {
        str(value).strip()
        for value in (data.get("scopes") or ())
        if isinstance(value, str) and value.strip()
    }
    granular_targets = {}
    granular = data.get("granular_scopes")
    if isinstance(granular, list):
        for item in granular:
            if not isinstance(item, dict):
                continue
            scope = str(item.get("scope") or "").strip()
            targets = {
                str(value).strip()
                for value in (item.get("target_ids") or ())
                if isinstance(value, (str, int)) and str(value).strip()
            }
            if scope:
                scopes.add(scope)
                granular_targets[scope] = targets
    expiries = [
        expiry
        for expiry in (
            _epoch(data.get("expires_at")),
            _epoch(data.get("data_access_expires_at")),
        )
        if expiry
    ]
    return {
        "valid": data.get("is_valid") is True,
        "app_id": str(data.get("app_id") or ""),
        "profile_id": str(data.get("profile_id") or ""),
        "token_type": str(data.get("type") or "").upper(),
        "scopes": scopes,
        "granular_targets": granular_targets,
        "expires_at": min(expiries) if expiries else None,
    }


def _scope_is_valid(observation, scope, target_id):
    if scope not in observation["scopes"]:
        return False
    targets = observation["granular_targets"].get(scope) or set()
    return not targets or target_id in targets


@adapter_registry.register("meta", module="contact_center_meta")
class MetaAdapter(ProviderAdapter):
    """Messenger and Instagram adapter over the canonical shared Meta core."""

    key = "meta"
    display_name = "Meta"

    def authenticate_webhook(self, connection, headers, body):
        connection = _connection(connection)
        app = connection.sudo().meta_api_app_id
        try:
            runtime = app.with_context(
                **{META_API_RUNTIME_CONTEXT_KEY: META_API_RUNTIME_TOKEN}
            )._resolve_runtime(expected_revision=app.revision)
        except MetaCredentialResolutionError:
            return False
        return verify_signature(runtime.app_secret, headers, body)

    def normalize_event(self, connection, envelope):
        return normalize_meta_event(_connection(connection), envelope)

    def execute_command(self, connection, command):
        return execute_send_request(self.env, _connection(connection), command)

    def prepare_request_snapshot(self, connection, command):
        return prepare_send_request(self.env, _connection(connection), command)

    def get_capabilities(self, connection):
        connection = _connection(connection)
        return {
            "schema_version": "1.0",
            "send_message": True,
            "sender_signature": False,
            "mark_read": False,
            "reply": True,
            "react": False,
            "edit_message": False,
            "delete_message": False,
            "identity_avatar": True,
            "media": media_capabilities(connection),
            "conversation_types": {},
            "extensions": {
                "provider.meta": {
                    "graph_api_version": connection.meta_api_app_id.graph_version,
                    "provider_schema_version": META_PROVIDER_SCHEMA_VERSION,
                    "transport_enabled": True,
                    "outbound_direct_text": True,
                    "outbound_response_window": "standard_24h",
                    "outbound_echo_correlation": "provider_message_id",
                    "outbound_media": True,
                    "outbound_reply": True,
                    "outbound_reaction": False,
                    "inbound_delivery_ledger": "meta_webhook_base",
                    "inbound_direct_text": True,
                    "inbound_echo": True,
                    "inbound_media": ["image", "audio", "video", "document"],
                    "private_media_locator_vault": True,
                    "identity_profile_sync": True,
                }
            },
        }

    def get_health(self, connection):
        connection = _connection(connection)
        if not connection._meta_runtime_topology_is_ready(require_subscriptions=False):
            return {"state": "degraded", "reason": "provider_paused"}
        try:
            runtime, page_token, _page_revision = resolve_graph_runtime(connection)
            observation = _token_observation(graph_debug_token(runtime, page_token))
        except ProviderPausedError:
            return {
                "state": "authentication_required",
                "reason": "authentication_required",
            }
        page = connection.sudo().meta_webhook_page_id
        expired = bool(
            observation["expires_at"]
            and observation["expires_at"] <= datetime.datetime.utcnow()
        )
        if not observation["valid"] or expired:
            return {
                "state": "authentication_required",
                "reason": "authentication_required",
            }
        identity_matches = bool(
            observation["app_id"] == runtime.external_app_id
            and observation["profile_id"] == page.external_page_id
            and observation["token_type"] == "PAGE"
        )
        if not identity_matches:
            return {
                "state": "degraded",
                "reason": "identity_mismatch",
                "identity_matches": False,
            }
        required_scopes = _REQUIRED_SCOPES.get(connection.meta_transport_mode, ())
        asset_target = connection.meta_target_asset_id
        missing_scope = any(
            not _scope_is_valid(
                observation,
                scope,
                (
                    asset_target
                    if scope.startswith("instagram_")
                    else page.external_page_id
                ),
            )
            for scope in required_scopes
        )
        if missing_scope or not connection._meta_inbound_route_is_ready():
            return {
                "state": "degraded",
                "reason": "provider_paused",
                "identity_matches": True,
            }
        return {
            "state": "connected",
            "reason": "healthy",
            "identity_matches": True,
        }

    def is_provider_read_ready(self, connection, purpose):
        connection = _connection(connection)
        if purpose not in ("media_download", "identity_profile"):
            return super().is_provider_read_ready(connection, purpose)
        topology_ready = connection._meta_runtime_topology_is_ready(
            require_subscriptions=False
        )
        if purpose == "media_download":
            return topology_ready
        return bool(
            topology_ready
            and connection.state == "connected"
            and not connection.identity_mismatch_latched
        )

    def download_media(self, connection, media):
        return download_private_media(self.env, _connection(connection), media)

    def supports_identity_profile(self, connection):
        _connection(connection)
        return True

    def fetch_identity_profile(self, connection, address):
        return fetch_identity_profile(_connection(connection), address)

    def finalize_media_download(self, connection, media, *, succeeded):
        return finalize_private_media(
            self.env,
            _connection(connection),
            media,
            succeeded=succeeded,
        )
