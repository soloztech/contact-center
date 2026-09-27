"""Contact Center boundary for the shared Graph client (WhatsApp Business owner)."""

import dataclasses

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    AmbiguousTimeoutError,
    ProviderPausedError,
    ProviderRateLimitError,
    TransientAdapterError,
)
from odoo.addons.meta_api_base.services import graph as shared_graph
from odoo.addons.meta_api_base.services.credentials import MetaCredentialResolutionError
from odoo.addons.meta_api_base.services.errors import (
    MetaApiError,
    MetaApiPausedError,
    MetaApiRateLimitError,
    MetaApiTransientError,
    MetaApiUncertainError,
)
from odoo.addons.meta_api_base.services.signature import validate_graph_version
from odoo.addons.meta_webhook_base.services import META_WEBHOOK_RUNTIME_TOKEN

from .contracts import (
    DEFAULT_GRAPH_VERSION,
    WHATSAPP_CLOUD_ADAPTER_KEY,
    WHATSAPP_OWNER_KIND,
)

# Provider codes documented for the Cloud API send endpoint.
_SEND_PERMANENT_CODES = frozenset(
    {131026, 131047, 131048, 131049, 131050, 131051, 131052, 131053, 131009, 131062}
)
_SEND_RATE_LIMIT_CODES = frozenset({4, 80007, 130429, 131056})
_SEND_PAUSE_CODES = frozenset({0, 190, 368, 131031})
_SEND_TRANSIENT_CODES = frozenset({1, 2, 131000, 131016, 133004})
_UNCERTAIN_STATUSES = (408, 425)
_DEFAULT_RATE_LIMIT_SECONDS = 60
# One message per six seconds for the same business/recipient pair (131056).
_PAIR_RATE_LIMIT_SECONDS = 6


def _copy_diagnostics(source, translated):
    for attribute in (
        "retry_after_seconds",
        "http_status",
        "provider_code",
        "provider_subcode",
    ):
        if hasattr(source, attribute):
            setattr(translated, attribute, getattr(source, attribute))
    return translated


def adapter_error(error):
    """Generic translation of a shared Graph failure (reads and uploads)."""

    if isinstance(error, MetaApiRateLimitError):
        error_class = ProviderRateLimitError
    elif isinstance(error, (MetaApiPausedError, MetaCredentialResolutionError)):
        error_class = ProviderPausedError
    elif isinstance(error, MetaApiUncertainError):
        error_class = AmbiguousTimeoutError
    elif isinstance(error, MetaApiTransientError):
        error_class = TransientAdapterError
    else:
        error_class = AdapterError
    return _copy_diagnostics(error, error_class(str(error)))


def send_error(error):
    """Classify one failed ``POST /messages`` before the generic translation.

    A result that cannot prove the provider refused the message is uncertain:
    network failures, timeouts, 5xx and a 429 without a provider code. A proven
    refusal is classified by its provider code (R01, R16).
    """

    code = int(getattr(error, "provider_code", 0) or 0)
    status = int(getattr(error, "http_status", 0) or 0)
    if isinstance(error, MetaCredentialResolutionError):
        return adapter_error(error)
    if (
        isinstance(error, MetaApiUncertainError)
        or status >= 500
        or status in _UNCERTAIN_STATUSES
    ):
        return _copy_diagnostics(
            error, AmbiguousTimeoutError("WhatsApp send outcome is uncertain")
        )
    if status == 429 and not code:
        return _copy_diagnostics(
            error, AmbiguousTimeoutError("WhatsApp send outcome is uncertain")
        )
    if code in _SEND_RATE_LIMIT_CODES or (status == 429 and code):
        translated = _copy_diagnostics(
            error, ProviderRateLimitError("WhatsApp refused the send: rate limited")
        )
        if not translated.retry_after_seconds:
            translated.retry_after_seconds = (
                _PAIR_RATE_LIMIT_SECONDS
                if code == 131056
                else _DEFAULT_RATE_LIMIT_SECONDS
            )
        return translated
    if code in _SEND_PAUSE_CODES or isinstance(error, MetaApiPausedError):
        return _copy_diagnostics(
            error, ProviderPausedError("WhatsApp refused the send: account paused")
        )
    if code in _SEND_PERMANENT_CODES or 132000 <= code <= 132999:
        return _copy_diagnostics(
            error, AdapterError("WhatsApp refused the send: code %s" % code)
        )
    if code in _SEND_TRANSIENT_CODES or isinstance(error, MetaApiTransientError):
        return _copy_diagnostics(
            error, TransientAdapterError("WhatsApp refused the send temporarily")
        )
    return adapter_error(error)


def resolve_graph_runtime(connection):
    """Resolve the App runtime and the WABA system-user token, in memory only.

    The shared credential guard refuses a Page owner here and any WABA owner in
    Page consumers before a secret is resolved (MC-03). The connection's Graph
    version replaces the App default in the immutable runtime snapshot.
    """

    connection.ensure_one()
    sudo_connection = connection.sudo()
    asset = sudo_connection.wa_webhook_asset_id
    if connection.adapter_key != WHATSAPP_CLOUD_ADAPTER_KEY or not asset:
        raise ProviderPausedError("WhatsApp Cloud route is unavailable")
    version = sudo_connection.wa_graph_version or DEFAULT_GRAPH_VERSION
    if not validate_graph_version(version):
        raise AdapterError("WhatsApp Cloud Graph version is invalid")
    page = asset.page_id.sudo()
    try:
        runtime, token, revision = page.with_context(
            meta_webhook_runtime=META_WEBHOOK_RUNTIME_TOKEN
        )._resolve_graph_runtime(
            expected_page_revision=page.revision,
            expected_app_revision=page.app_id.revision,
            expected_owner_kind=WHATSAPP_OWNER_KIND,
        )
    except MetaApiError as error:
        raise adapter_error(error) from None
    return dataclasses.replace(runtime, graph_version=version), token, revision


def graph_request(runtime, access_token, method, path, **kwargs):
    try:
        return shared_graph.graph_request(runtime, access_token, method, path, **kwargs)
    except MetaApiError as error:
        raise adapter_error(error) from None


def graph_send(runtime, access_token, path, payload, *, max_response_bytes):
    """Perform one message-creating POST with send-specific classification."""

    try:
        return shared_graph.graph_request(
            runtime,
            access_token,
            "POST",
            path,
            json_data=payload,
            mutating=True,
            max_response_bytes=max_response_bytes,
        )
    except MetaApiError as error:
        raise send_error(error) from None
