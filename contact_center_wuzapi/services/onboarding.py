import base64
import binascii
import hmac
import json
import re

import requests

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    ProviderRateLimitError,
    TransientAdapterError,
)

_MAX_ADMIN_RESPONSE_BYTES = 512 * 1024
_MAX_SESSION_RESPONSE_BYTES = 64 * 1024
_MAX_QR_BYTES = 1024 * 1024
_REQUEST_TIMEOUT = (5, 20)
_MAX_SESSION_JID_LENGTH = 255
_SESSION_JID_PATTERN = re.compile(r"^[0-9]+(?::[0-9]+)?@(s\.whatsapp\.net|lid)$")


def is_valid_session_jid(value):
    return bool(
        isinstance(value, str)
        and 0 < len(value) <= _MAX_SESSION_JID_LENGTH
        and _SESSION_JID_PATTERN.fullmatch(value)
    )


def _lookup(values, *names):
    if not isinstance(values, dict):
        return None
    normalized = {
        "".join(
            character for character in str(key).lower() if character.isalnum()
        ): value
        for key, value in values.items()
    }
    for name in names:
        candidate = "".join(
            character for character in str(name).lower() if character.isalnum()
        )
        if candidate in normalized:
            return normalized[candidate]
    return None


def _bounded_json(response, maximum_bytes, label):
    declared_length = None
    try:
        content_length = response.headers.get("Content-Length")
        declared_length = int(content_length) if content_length else None
    except (TypeError, ValueError):
        declared_length = None
    if declared_length is not None and declared_length > maximum_bytes:
        response.close()
        raise AdapterError("%s response is too large" % label)
    chunks = []
    total = 0
    try:
        iterator = getattr(response, "iter_content", None)
        if callable(iterator):
            for chunk in iterator(chunk_size=16 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > maximum_bytes:
                    raise AdapterError("%s response is too large" % label)
                chunks.append(chunk)
            raw = b"".join(chunks)
            payload = json.loads(raw.decode("utf-8"))
        else:
            payload = response.json()
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if len(encoded) > maximum_bytes:
                raise AdapterError("%s response is too large" % label)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise AdapterError("%s response is not valid JSON" % label) from error
    except requests.RequestException as error:
        raise TransientAdapterError(
            "%s response stream was interrupted" % label
        ) from error
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if not isinstance(payload, dict):
        raise AdapterError("%s response is not a JSON object" % label)
    return payload


class WuzapiOnboardingClient:
    """Bounded provider client used only by the administrative setup workflow."""

    def __init__(self, base_url, *, admin_token=None, api_token=None):
        self.base_url = base_url.rstrip("/")
        self.admin_token = admin_token or ""
        self.api_token = api_token or ""

    def _request(
        self,
        method,
        path,
        *,
        admin=False,
        payload=None,
        accepted=(200,),
        maximum_bytes=_MAX_SESSION_RESPONSE_BYTES,
    ):
        token = self.admin_token if admin else self.api_token
        header_name = "Authorization" if admin else "Token"
        if not token:
            raise AdapterError("WuzAPI setup credential is missing")
        try:
            response = requests.request(
                method,
                "%s%s" % (self.base_url, path),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    header_name: token,
                },
                json=payload,
                timeout=_REQUEST_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as error:
            raise TransientAdapterError(
                "WuzAPI setup endpoint did not respond"
            ) from error
        if response.status_code not in accepted:
            status = response.status_code
            response.close()
            if status == 429:
                raise ProviderRateLimitError("WuzAPI setup was rate limited")
            if status >= 500:
                raise TransientAdapterError("WuzAPI setup failed with HTTP %s" % status)
            raise AdapterError("WuzAPI setup failed with HTTP %s" % status)
        return _bounded_json(response, maximum_bytes, "WuzAPI setup")

    @staticmethod
    def _validated_data(payload, expected_type):
        if _lookup(payload, "success") is not True:
            raise AdapterError("WuzAPI rejected the setup request")
        data = _lookup(payload, "data")
        if not isinstance(data, expected_type):
            raise AdapterError("WuzAPI setup response data is invalid")
        return data

    def list_users(self):
        payload = self._request(
            "GET",
            "/admin/users",
            admin=True,
            maximum_bytes=_MAX_ADMIN_RESPONSE_BYTES,
        )
        users = self._validated_data(payload, list)
        if len(users) > 200 or any(not isinstance(item, dict) for item in users):
            raise AdapterError("WuzAPI user inventory is invalid")
        return users

    def find_user_by_token(self, token):
        for user in self.list_users():
            observed = _lookup(user, "token")
            if isinstance(observed, str) and hmac.compare_digest(observed, token):
                return {
                    "id": str(_lookup(user, "id") or "").strip(),
                    "name": str(_lookup(user, "name") or "").strip(),
                    "jid": str(_lookup(user, "jid") or "").strip(),
                    "webhook": str(_lookup(user, "webhook") or "").strip(),
                    "events": str(_lookup(user, "events") or "").strip(),
                }
        return None

    def create_or_find_user(self, *, name, token, webhook_url, events, hmac_key):
        try:
            payload = self._request(
                "POST",
                "/admin/users",
                admin=True,
                payload={
                    "name": name,
                    "token": token,
                    "webhook": webhook_url,
                    "events": ",".join(events),
                    "hmacKey": hmac_key,
                },
                accepted=(200, 201, 409),
                maximum_bytes=_MAX_ADMIN_RESPONSE_BYTES,
            )
        except TransientAdapterError:
            existing = self.find_user_by_token(token)
            if existing:
                return existing
            raise
        if _lookup(payload, "code") == 409 or _lookup(payload, "success") is False:
            existing = self.find_user_by_token(token)
            if existing:
                return existing
            raise AdapterError("WuzAPI could not reconcile the managed instance")
        user = self._validated_data(payload, dict)
        user_id = str(_lookup(user, "id") or "").strip()
        if not user_id:
            raise AdapterError("WuzAPI did not return the managed instance id")
        return {
            "id": user_id,
            "name": str(_lookup(user, "name") or name).strip(),
            "jid": str(_lookup(user, "jid") or "").strip(),
            "webhook": str(_lookup(user, "webhook") or "").strip(),
            "events": str(_lookup(user, "events") or "").strip(),
        }

    def connect(self, events):
        try:
            payload = self._request(
                "POST",
                "/session/connect",
                payload={"Subscribe": list(events), "Immediate": True},
                accepted=(200, 409),
            )
        except AdapterError as error:
            if "HTTP 409" in str(error):
                return {"already_connected": True}
            raise
        if _lookup(payload, "code") == 409:
            return {"already_connected": True}
        if _lookup(payload, "success") is not True:
            raise AdapterError("WuzAPI rejected the session connection request")
        return {"already_connected": False}

    def status(self):
        payload = self._request("GET", "/session/status")
        data = self._validated_data(payload, dict)
        connected = _lookup(data, "connected")
        logged_in = _lookup(data, "loggedIn")
        hmac_configured = _lookup(data, "hmac_configured", "hmacConfigured")
        if not isinstance(connected, bool) or not isinstance(logged_in, bool):
            raise AdapterError("WuzAPI returned an invalid session status")
        if hmac_configured is not None and not isinstance(hmac_configured, bool):
            raise AdapterError("WuzAPI returned an invalid HMAC status")
        jid = str(_lookup(data, "jid") or "").strip()
        if jid and not is_valid_session_jid(jid):
            raise AdapterError("WuzAPI returned an invalid session identity")
        return {
            "connected": connected,
            "logged_in": logged_in,
            "jid": jid,
            "hmac_configured": hmac_configured,
        }

    def qr_png(self):
        payload = self._request("GET", "/session/qr")
        data = self._validated_data(payload, dict)
        encoded = _lookup(data, "QRCode", "qrCode")
        if not isinstance(encoded, str) or not encoded.startswith(
            "data:image/png;base64,"
        ):
            raise AdapterError("WuzAPI did not return a PNG QR code")
        try:
            raw = base64.b64decode(encoded.split(",", 1)[1], validate=True)
        except (ValueError, binascii.Error) as error:
            raise AdapterError("WuzAPI returned an invalid QR code") from error
        if not 64 <= len(raw) <= _MAX_QR_BYTES or not raw.startswith(
            b"\x89PNG\r\n\x1a\n"
        ):
            raise AdapterError("WuzAPI returned an invalid QR image")
        return raw
