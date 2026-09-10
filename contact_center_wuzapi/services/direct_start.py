"""Registration-only direct addressing for the pinned WuzAPI v1.0.8 boundary."""

import re

from odoo.addons.contact_center_base.services.adapter import AdapterError
from odoo.addons.contact_center_base.services.dto import AddressDTO, DirectAddressResult

_PHONE_PATTERN = re.compile(r"[1-9][0-9]{6,14}")
_PN_PATTERN = re.compile(r"([1-9][0-9]{6,14})@s\.whatsapp\.net")
_LID_PATTERN = re.compile(r"[1-9][0-9]{0,19}@lid")
_MAX_DIRECT_CHECK_BYTES = 32 * 1024
_MAX_DIRECT_LID_BYTES = 8 * 1024


def _same_registered_phone(query, registered):
    """Accept only an exact number or provider-proven Brazilian mobile ninth digit.

    This is not a normalizer and must never be used to invent an alias without a
    registration response for the exact query. Fixed lines, other DDIs, changed
    DDDs and arbitrary last-eight-digit coincidences are not equivalent.
    """

    if query == registered:
        return True
    short, long = sorted((query, registered), key=len)
    return bool(
        re.fullmatch(r"55[1-9][0-9][6-9][0-9]{7}", short)
        and len(long) == 13
        and long == "%s9%s" % (short[:4], short[4:])
    )


def _address(jid, *, role, source_field, confidence="protocol", scope="account"):
    return AddressDTO(
        namespace="whatsapp.pn" if _PN_PATTERN.fullmatch(jid) else "whatsapp.lid",
        value=jid,
        value_normalized=jid,
        role=role,
        source_field=source_field,
        confidence=confidence,
        resolution_scope=scope,
    )


class WuzapiDirectStartMixin:
    """Provider reads only; core remains responsible for access/readiness/mutation."""

    def supports_direct_conversation_start(self, connection):
        return True

    def resolve_direct_address(self, connection, normalized_phone):
        if not isinstance(normalized_phone, str) or not _PHONE_PATTERN.fullmatch(
            normalized_phone
        ):
            raise AdapterError("WuzAPI direct start requires normalized E.164 digits")
        data = self._identity_profile_request(
            connection,
            "POST",
            "/user/check",
            "direct registration check",
            payload={"Phone": [normalized_phone]},
            maximum_bytes=_MAX_DIRECT_CHECK_BYTES,
        )
        users = self._provider_lookup(data, "Users")
        if (
            not isinstance(users, list)
            or len(users) != 1
            or not isinstance(users[0], dict)
        ):
            raise AdapterError("WuzAPI direct registration requires exactly one result")
        user = users[0]
        if self._provider_lookup(user, "Query") != normalized_phone:
            raise AdapterError("WuzAPI direct registration query does not match")
        registered = self._provider_lookup(user, "IsInWhatsapp")
        if not isinstance(registered, bool):
            raise AdapterError("WuzAPI direct registration flag is invalid")
        raw_jid = self._provider_lookup(user, "JID")
        if not registered:
            # A false registration flag is authoritative, but must not carry an
            # unrelated recipient. Empty JIDs are the provider's normal absence.
            if raw_jid not in (None, ""):
                match = (
                    _PN_PATTERN.fullmatch(raw_jid) if isinstance(raw_jid, str) else None
                )
                if not match or match.group(1) != normalized_phone:
                    raise AdapterError(
                        "WuzAPI unregistered response JID does not match"
                    )
            return DirectAddressResult(state="not_registered")
        if not isinstance(raw_jid, str):
            raise AdapterError("WuzAPI direct registration JID is invalid")
        phone_match = _PN_PATTERN.fullmatch(raw_jid)
        if phone_match:
            canonical_phone = phone_match.group(1)
            if not _same_registered_phone(normalized_phone, canonical_phone):
                raise AdapterError("WuzAPI registered phone does not match the query")
            addresses = [
                _address(
                    raw_jid,
                    role="primary",
                    source_field="user.check.Users.JID",
                    scope="company",
                )
            ]
            if canonical_phone != normalized_phone:
                addresses.append(
                    _address(
                        "%s@s.whatsapp.net" % normalized_phone,
                        role="alternate",
                        source_field="user.check.Users.Query",
                        confidence="observed",
                    )
                )
            lid_data = self._identity_profile_request(
                connection,
                "GET",
                "/user/lid/%s" % raw_jid,
                "direct LID mapping",
                maximum_bytes=_MAX_DIRECT_LID_BYTES,
                allow_not_found=True,
            )
            if lid_data is not None:
                lid = self._provider_lookup(lid_data, "lid")
                if (
                    self._provider_lookup(lid_data, "jid") != raw_jid
                    or not isinstance(lid, str)
                    or not _LID_PATTERN.fullmatch(lid)
                ):
                    raise AdapterError("WuzAPI direct LID mapping does not match")
                addresses.append(
                    _address(
                        lid,
                        role="alternate",
                        source_field="user.lid.lid",
                    )
                )
        elif _LID_PATTERN.fullmatch(raw_jid):
            # IsOnWhatsApp can return an opaque LID. Its exact Query proves only
            # this account-local relation, not a globally authoritative phone.
            addresses = [
                _address(raw_jid, role="primary", source_field="user.check.Users.JID"),
                _address(
                    "%s@s.whatsapp.net" % normalized_phone,
                    role="alternate",
                    source_field="user.check.Users.Query",
                    confidence="observed",
                ),
            ]
        else:
            raise AdapterError("WuzAPI direct registration JID is invalid")
        return DirectAddressResult(
            state="ready", conversation_ref=raw_jid, addresses=tuple(addresses)
        )
