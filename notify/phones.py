"""
Phone number normalisation to E.164 (spec v1.6 §3) and masking helpers for the notification health API.

Zimbabwe is the default country (``SMS_DEFAULT_COUNTRY_CODE``, default ``263``):

    0771234567        -> +263771234567   (national trunk prefix 0 replaced by the country code)
    +263 77 123 4567  -> +263771234567   (spaces / dashes / dots / brackets removed)
    263771234567      -> +263771234567   (country code without the plus)
    00263771234567    -> +263771234567   (international 00 prefix)
    +263 0771234567   -> +263771234567   (stray trunk 0 after the country code)
    771234567         -> +263771234567   (national number without trunk prefix)

Anything else that is not 8–15 digits after the ``+`` is invalid (``None``). Numbers in the default
country must have 8–9 national digits (Zimbabwe mobiles have 9).
"""
from __future__ import annotations

import re

_SEPARATORS = re.compile(r"[\s\-.()/ ]")
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def default_country_code() -> str:
    from django.conf import settings

    cc = str(getattr(settings, "SMS_DEFAULT_COUNTRY_CODE", "263") or "263").strip().lstrip("+")
    return cc if cc.isdigit() else "263"


def normalise_phone(raw, country_code: str | None = None) -> str | None:
    """
    Return the E.164 form of ``raw`` or ``None`` when it cannot be a valid number.
    Empty input returns ``""`` (no number is not an invalid number).
    """
    if raw is None:
        return ""
    text = _SEPARATORS.sub("", str(raw).strip())
    if not text:
        return ""
    cc = country_code or default_country_code()
    if text.startswith("+"):
        digits = text[1:]
    elif text.startswith("00"):
        digits = text[2:]
    elif text.startswith("0"):
        digits = cc + text[1:]
    elif text.startswith(cc) and len(text) > len(cc) + 7:
        digits = text
    else:
        digits = cc + text
    if not digits.isdigit():
        return None
    if digits.startswith(cc + "0"):  # "+263 0771234567": drop the trunk prefix
        digits = cc + digits[len(cc) + 1:]
    number = "+" + digits
    if not _E164.match(number):
        return None
    if digits.startswith(cc) and not (8 <= len(digits) - len(cc) <= 9):
        return None
    return number


def is_valid_phone(raw) -> bool:
    return bool(normalise_phone(raw))


def mask_phone(value) -> str | None:
    """``+263771234567`` -> ``+26377•••••67`` (normalised first when possible)."""
    raw = (value or "").strip()
    if not raw:
        return None
    text = normalise_phone(raw) or _SEPARATORS.sub("", raw)
    if len(text) <= 4:
        return "•" * len(text)
    keep = min(6, len(text) // 2)
    return text[:keep] + "•" * max(1, len(text) - keep - 2) + text[-2:]


def mask_email(value) -> str | None:
    """``grace@grtts.co.zw`` -> ``g•••@grtts.co.zw``."""
    raw = (value or "").strip()
    if not raw:
        return None
    local, sep, domain = raw.partition("@")
    if not sep:
        return raw[:1] + "•••"
    return f"{local[:1]}•••@{domain}"


def mask_recipient(channel: str, to: str) -> str | None:
    if not to:
        return None
    if channel == "sms":
        return mask_phone(to)
    if channel == "email":
        return mask_email(to)
    return to  # push topics carry no personal data
