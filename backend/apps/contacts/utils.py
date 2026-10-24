import re

from django.conf import settings

_E164_RE = re.compile(r"\+[1-9]\d{7,14}")

#: The longest national number each country issues, i.e. the digits after the
#: country code. This is what tells "a number that already carries the code"
#: from "a number that merely starts with the same digits": an Indian mobile is
#: ten digits, so a ten-digit 9112345678 is a contact's own number -- its
#: leading 91 is not a country code -- and only something longer than ten that
#: starts with 91 can be +91 written without the plus.
#:
#: The countries listed are the ones this product's customers actually import
#: from; anything else falls back to the general heuristic in
#: _carries_code(). Maximums, not exact lengths, because several of these
#: countries issue numbers of more than one length.
_NATIONAL_DIGITS = {
    "1": 10,     # US, Canada
    "7": 10,     # Russia, Kazakhstan
    "20": 10,    # Egypt
    "27": 9,     # South Africa
    "33": 9,     # France
    "34": 9,     # Spain
    "44": 10,    # UK
    "49": 11,    # Germany
    "52": 10,    # Mexico
    "55": 11,    # Brazil
    "60": 10,    # Malaysia
    "61": 9,     # Australia
    "62": 12,    # Indonesia
    "63": 10,    # Philippines
    "64": 10,    # New Zealand
    "65": 8,     # Singapore
    "66": 9,     # Thailand
    "84": 10,    # Vietnam
    "86": 11,    # China
    "91": 10,    # India
    "92": 10,    # Pakistan
    "94": 9,     # Sri Lanka
    "234": 10,   # Nigeria
    "254": 9,    # Kenya
    "880": 10,   # Bangladesh
    "965": 8,    # Kuwait
    "966": 9,    # Saudi Arabia
    "968": 8,    # Oman
    "971": 9,    # UAE
    "973": 8,    # Bahrain
    "974": 8,    # Qatar
    "977": 10,   # Nepal
}

#: Countries whose national numbers keep their leading zero inside E.164
#: (+39 06 ... is Rome). Everywhere else a leading 0 is the trunk prefix
#: dialled at home -- "09876543210" -- and is dropped when the code goes on.
_KEEPS_LEADING_ZERO = {"39"}

#: Fallback for a country code not in _NATIONAL_DIGITS: below this many digits
#: a number is read as national even if it begins with the code, and the part
#: after the code must be at least the shortest national number in E.164.
_MIN_INTERNATIONAL_DIGITS = 10
_MIN_NATIONAL_DIGITS = 7


def normalize_phone(raw: str) -> str:
    """Normalize a phone number to E.164, raising ValueError if it can't be."""
    normalized = "+" + re.sub(r"\D", "", raw or "")
    if not _E164_RE.fullmatch(normalized):
        raise ValueError(f"'{raw}' is not a valid phone number.")
    return normalized


def _already_international(digits: str, code: str) -> bool:
    """Whether ``digits`` (no plus) already includes a country code."""
    longest = _NATIONAL_DIGITS.get(code)
    if longest is not None:
        # Anything that fits inside a national number *is* a national number,
        # whatever it starts with -- 9112345678 is a contact's own ten digits.
        # Anything longer cannot be one of this country's numbers, so it is
        # already international: 919876543210 is +91, and 14155552671 is a US
        # number written without its plus, not a 91 to be bolted on.
        return len(digits) > longest
    return (
        digits.startswith(code)
        and len(digits) >= _MIN_INTERNATIONAL_DIGITS
        and len(digits) - len(code) >= _MIN_NATIONAL_DIGITS
    )


def apply_country_code(raw: str, default_country_code: str):
    """
    The digits of ``raw`` with a country code in front, ready for normalize_phone.

    The number is read as the contact's own number first and the code is
    added to it, rather than the other way round. A ``+`` or ``00`` prefix is
    the only thing that marks a number as already international; otherwise the
    code is assumed missing unless the number is too long to be a national one
    (see _already_international). Returns ``""`` for input with no digits, and
    ``None`` when the number has no code and no default was given -- the
    caller decides whether that is a refusal or a fallback.
    """
    # Quotes and a bracket can sit in front of the plus. In particular every
    # CSV this product writes -- exports, and the files the lead tools hand to
    # the importer -- carries phones as '+91..., the apostrophe being the
    # formula-injection escape from core.csv_utils. Reading that as "no plus"
    # would re-prefix numbers that are already complete.
    raw = (raw or "").strip().lstrip("'\"( ")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""

    # "00" is how much of the world writes "+".
    if raw.startswith("+") or digits.startswith("00"):
        return digits[2:] if digits.startswith("00") else digits

    code = re.sub(r"\D", "", default_country_code or "")
    if not code:
        return None

    if code not in _KEEPS_LEADING_ZERO:
        digits = digits.lstrip("0")
    if _already_international(digits, code):
        return digits
    return code + digits


def normalize_contact_phone(raw: str, default_country_code: str | None = None) -> str:
    """
    E.164 for a number typed or imported by a person, raising ValueError.

    Uses the deployment's default country code (CONTACTS_DEFAULT_COUNTRY_CODE)
    unless one is passed. Without it, a ten-digit Indian mobile in a CSV --
    "9876543210" -- went straight through normalize_phone as +98 76543210, a
    valid-looking number in Iran.
    """
    if default_country_code is None:
        default_country_code = getattr(settings, "CONTACTS_DEFAULT_COUNTRY_CODE", "")
    digits = apply_country_code(raw, default_country_code)
    if digits is None:
        digits = raw
    return normalize_phone(digits)
