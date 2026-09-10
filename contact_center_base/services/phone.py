"""Strict user-input normalization for explicitly starting a direct conversation.

Formatting is tolerated; identity changes are not. In particular, Brazil's ninth
mobile digit is never inserted or removed here. Only the provider can establish
the registered destination, after this pure normalization step.
"""

import re

import phonenumbers

_INPUT_PATTERN = re.compile(r"[0-9+ ().\-\u00a0\u202f]+")
_DIGITS_PATTERN = re.compile(r"[1-9][0-9]{6,14}")
_INVALID_PHONE = "Informe um telefone válido com DDD. Para outro país, use + e o DDI."


def _valid_brazil_legacy_mobile(digits):
    """Accept an old eight-digit mobile without silently changing its identity."""

    if not re.fullmatch(r"55[1-9][0-9][6-9][0-9]{7}", digits):
        return False
    # Metadata validates the DDD and mobile range, but the returned value remains
    # the original eight-digit form. A registration lookup is still mandatory.
    probe = phonenumbers.parse("+%s9%s" % (digits[:4], digits[4:]), None)
    return phonenumbers.is_valid_number(probe)


def normalize_start_phone(value, country_code="BR"):
    """Return E.164 digits (without ``+``), or raise ``ValueError``.

    BR national input must contain DDD plus eight/nine subscriber digits. A
    national DDD 55 is not confused with the international country code 55.
    Explicit ``+`` or ``00`` enables international numbers; other countries
    require an explicit valid ISO region supplied by the trusted caller.
    """

    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or not _INPUT_PATTERN.fullmatch(value)
        or not isinstance(country_code, str)
        or country_code.upper() not in phonenumbers.SUPPORTED_REGIONS
    ):
        raise ValueError(_INVALID_PHONE)
    region = country_code.upper()
    compact = re.sub(r"[ ().\-\u00a0\u202f]", "", value)
    if not compact or "+" in compact[1:]:
        raise ValueError(_INVALID_PHONE)
    if compact.startswith("+"):
        digits = compact[1:]
        parse_value = "+%s" % digits
    elif compact.startswith("00"):
        digits = compact[2:]
        parse_value = "+%s" % digits
    elif region == "BR":
        if len(compact) in (10, 11):
            digits = "55%s" % compact
        elif len(compact) in (12, 13) and compact.startswith("55"):
            digits = compact
        else:
            raise ValueError(_INVALID_PHONE)
        parse_value = "+%s" % digits
    else:
        digits = compact
        parse_value = compact
    if not _DIGITS_PATTERN.fullmatch(digits):
        raise ValueError(_INVALID_PHONE)
    try:
        parsed = phonenumbers.parse(parse_value, region)
        normalized = phonenumbers.format_number(
            parsed, phonenumbers.PhoneNumberFormat.E164
        )[1:]
    except phonenumbers.NumberParseException:
        raise ValueError(_INVALID_PHONE) from None
    if not _DIGITS_PATTERN.fullmatch(normalized) or not (
        phonenumbers.is_valid_number(parsed) or _valid_brazil_legacy_mobile(normalized)
    ):
        raise ValueError(_INVALID_PHONE)
    # Do not let permissive library parsing strip a trunk/carrier prefix or
    # reinterpret explicit international input as a different destination.
    if parse_value.startswith("+") and normalized != digits:
        raise ValueError(_INVALID_PHONE)
    return normalized
