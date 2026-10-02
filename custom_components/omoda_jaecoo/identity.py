"""Canonical phone identities, with no account data retained."""

from __future__ import annotations

import re

import phonenumbers


class InvalidPhoneNumber(ValueError):
    """A safe validation error, without the supplied number."""


def normalize_phone(value: str, country_code: str) -> str:
    """Return national significant digits (including Italy's significant zero).

    Accept national formatting or explicit +/00 international notation. A
    possible number is sufficient: this does not test assignment or ownership.
    """
    if (
        not isinstance(value, str)
        or not isinstance(country_code, str)
        or not re.fullmatch(r"[1-9][0-9]{0,2}", country_code)
        or not re.fullmatch(r"\+?[0-9 ().\-]+", value.strip())
        or len(value) > 80
    ):
        raise InvalidPhoneNumber("Invalid phone number.")
    country = int(country_code)
    region = phonenumbers.region_code_for_country_code(country)
    if region == "ZZ":
        raise InvalidPhoneNumber("Invalid phone number.")
    number = value.strip()
    if number.startswith("00"):
        number = "+" + number[2:]
    # Non-geographical calling codes have no national parsing region.
    if region == "001" and not number.startswith("+"):
        number = "+" + country_code + number
    try:
        parsed = phonenumbers.parse(number, region)
    except phonenumbers.NumberParseException:
        raise InvalidPhoneNumber("Invalid phone number.") from None
    if (
        parsed.country_code != country
        or parsed.extension
        or not phonenumbers.is_possible_number(parsed)
    ):
        raise InvalidPhoneNumber("Invalid phone number.")
    return phonenumbers.national_significant_number(parsed)


def phone_identity(value: str, country_code: str) -> str:
    """Return canonical E.164 for namespaced account identity hashing."""
    return "+" + country_code + normalize_phone(value, country_code)
