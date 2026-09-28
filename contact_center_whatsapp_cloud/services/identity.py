"""WhatsApp Cloud addresses aligned with the WuzAPI identity contract.

``whatsapp.pn`` uses exactly the WuzAPI normalization (``<digits>@s.whatsapp.net``,
company scope), so one phone converges on one identity across both transports.
A BSUID is portfolio-scoped: its normalized value is qualified by the WhatsApp
Business Account and stays inside the logical inbox (account scope), while the
raw BSUID remains in ``value`` for recipient-based sends.
"""

import re

from odoo.addons.contact_center_base.services.dto import AddressDTO

from .contracts import BSUID_NAMESPACE, PHONE_NAMESPACE, PHONE_SUFFIX

_PHONE_RE = re.compile(r"^[1-9][0-9]{5,19}$")
_BSUID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ID_RE = re.compile(r"^[0-9]{1,40}$")


def phone_value(wa_id):
    """Return the WuzAPI-compatible phone JID for one WhatsApp ID, or ``""``."""

    if not isinstance(wa_id, str):
        return ""
    digits = wa_id.strip()
    if digits.startswith("+"):
        digits = digits[1:]
    if not _PHONE_RE.fullmatch(digits):
        return ""
    return "%s%s" % (digits, PHONE_SUFFIX)


def phone_digits(value):
    """Return the E.164 digits of a normalized phone JID, or ``""``."""

    if not isinstance(value, str) or not value.endswith(PHONE_SUFFIX):
        return ""
    digits = value[: -len(PHONE_SUFFIX)]
    return digits if _PHONE_RE.fullmatch(digits) else ""


def bsuid_value(waba_id, bsuid):
    if (
        not isinstance(waba_id, str)
        or not _ID_RE.fullmatch(waba_id)
        or not isinstance(bsuid, str)
        or not _BSUID_RE.fullmatch(bsuid)
    ):
        return ""
    return "%s:%s" % (waba_id, bsuid)


def phone_address(wa_id, *, role, source_field):
    value = phone_value(wa_id)
    if not value:
        return None
    return AddressDTO(
        namespace=PHONE_NAMESPACE,
        value=value,
        value_normalized=value,
        role=role,
        source_field=source_field,
        confidence="protocol",
        resolution_scope="company",
    )


def bsuid_address(waba_id, bsuid, *, role, source_field):
    normalized = bsuid_value(waba_id, bsuid)
    if not normalized:
        return None
    return AddressDTO(
        namespace=BSUID_NAMESPACE,
        value=bsuid,
        value_normalized=normalized,
        role=role,
        source_field=source_field,
        confidence="protocol",
        resolution_scope="account",
    )


def remote_addresses(waba_id, wa_id, bsuid, *, phone_field, bsuid_field):
    """Return ``(conversation_ref, conversation_addresses, actor_addresses)``.

    The phone is the primary conversation address when present; otherwise the
    qualified BSUID is. A contact known only by BSUID is a valid conversation.
    """

    phone = (
        phone_address(wa_id, role="primary", source_field=phone_field)
        if wa_id
        else None
    )
    user = (
        bsuid_address(
            waba_id,
            bsuid,
            role="alternate" if phone else "primary",
            source_field=bsuid_field,
        )
        if bsuid
        else None
    )
    conversation = tuple(address for address in (phone, user) if address)
    if not conversation:
        return "", (), ()
    actors = tuple(
        AddressDTO(
            namespace=address.namespace,
            value=address.value,
            value_normalized=address.value_normalized,
            role="sender" if index == 0 else "alternate",
            source_field=address.source_field,
            confidence=address.confidence,
            resolution_scope=address.resolution_scope,
        )
        for index, address in enumerate(conversation)
    )
    return conversation[0].value_normalized, conversation, actors
