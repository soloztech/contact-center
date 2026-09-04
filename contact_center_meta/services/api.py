"""Contact Center boundary for the shared Meta Graph runtime."""

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
from odoo.addons.meta_webhook_base.services import META_WEBHOOK_RUNTIME_TOKEN


def _adapter_error(error):
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
    translated = error_class(str(error))
    for attribute in (
        "retry_after_seconds",
        "http_status",
        "provider_code",
        "provider_subcode",
    ):
        if hasattr(error, attribute):
            setattr(translated, attribute, getattr(error, attribute))
    return translated


def resolve_graph_runtime(connection):
    """Resolve one fenced App/Page runtime without persisting either secret."""

    connection.ensure_one()
    asset = connection.sudo().meta_webhook_asset_id
    if connection.adapter_key != "meta" or not asset:
        raise ProviderPausedError("Meta route is unavailable")
    page = asset.page_id.sudo()
    try:
        return page.with_context(
            meta_webhook_runtime=META_WEBHOOK_RUNTIME_TOKEN
        )._resolve_graph_runtime(
            expected_page_revision=page.revision,
            expected_app_revision=page.app_id.revision,
        )
    except MetaApiError as error:
        raise _adapter_error(error) from None


def graph_request(runtime, access_token, method, path, **kwargs):
    try:
        return shared_graph.graph_request(
            runtime,
            access_token,
            method,
            path,
            **kwargs,
        )
    except MetaApiError as error:
        raise _adapter_error(error) from None


def graph_debug_token(runtime, access_token):
    try:
        return shared_graph.graph_debug_token(runtime, access_token)
    except MetaApiError as error:
        raise _adapter_error(error) from None
