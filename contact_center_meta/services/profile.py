import hashlib
import re

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    ProviderPausedError,
    TransientAdapterError,
    UnsupportedEventError,
)
from odoo.addons.contact_center_base.services.dto import (
    AvatarResult,
    IdentityProfileResult,
)

from .api import graph_request, resolve_graph_runtime
from .media import download_profile_avatar

_GRAPH_ID_PATTERN = re.compile(r"^[0-9]{5,40}$")
_PROFILE_FIELDS = {
    "messenger_page": ("meta.messenger.psid", "first_name,last_name,profile_pic"),
    "instagram_page_linked": (
        "meta.instagram.igsid",
        "name,username,profile_pic",
    ),
}


def _profile_request(connection, address):
    expected = _PROFILE_FIELDS.get(connection.meta_transport_mode)
    if not expected or address.namespace != expected[0]:
        raise AdapterError("Meta profile address does not match the connection")
    external_id = str(address.value_normalized or address.value or "").strip()
    if not _GRAPH_ID_PATTERN.fullmatch(external_id):
        raise AdapterError("Meta profile address is invalid")
    runtime, page_token, _page_revision = resolve_graph_runtime(connection)
    try:
        payload = graph_request(
            runtime,
            page_token,
            "GET",
            external_id,
            params={"fields": expected[1]},
            max_response_bytes=64 * 1024,
        )
    except ProviderPausedError as error:
        if getattr(error, "provider_code", 0) in {10, 200}:
            raise UnsupportedEventError(
                "Meta profile access is not available"
            ) from None
        raise
    if str(payload.get("id") or "") != external_id:
        raise AdapterError("Meta profile response belongs to another identity")
    return payload


def _profile_name(connection, payload):
    if connection.meta_transport_mode == "messenger_page":
        values = (payload.get("first_name"), payload.get("last_name"))
        name = " ".join(value.strip() for value in values if isinstance(value, str))
    else:
        raw_name = payload.get("name") or payload.get("username") or ""
        name = raw_name.strip() if isinstance(raw_name, str) else ""
    return " ".join(name.split())[:255]


def fetch_identity_profile(connection, address):
    payload = _profile_request(connection, address)
    profile_picture = payload.get("profile_pic")
    if isinstance(profile_picture, str) and profile_picture:
        revision = hashlib.sha256(profile_picture.encode("utf-8")).hexdigest()
        try:
            avatar = download_profile_avatar(
                profile_picture,
                provider_revision=revision,
            )
        except TransientAdapterError:
            # A temporary CDN/rate-limit failure must keep the queue retryable;
            # otherwise the profile revision would be projected permanently
            # without the avatar even though the same URL may soon recover.
            raise
        except AdapterError:
            avatar = AvatarResult(
                state="unavailable",
                provider_revision=revision,
            )
    else:
        avatar = AvatarResult(state="absent")
    return IdentityProfileResult(
        display_name=_profile_name(connection, payload),
        avatar=avatar,
    )
