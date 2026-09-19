"""
Human–wildlife conflict (HWC) details log — spec v1.5 §A2.

The HWC alert travels the same always-accepted path as an SOS, so **nothing here may reject the
alert**: :func:`clean_details` is deliberately lenient. Unknown keys are dropped, a value that does
not fit its type/range drops *only that key*, and every drop is reported back to the phone as a
``details_warnings`` entry (``"field: reason"``) so the ranger can see what did not stick. Strings
are HTML-sanitised with :func:`core.validation.sanitize_text` before length checks.
"""
from __future__ import annotations

from rest_framework import serializers

from core.validation import sanitize_text

CONFLICT_TYPES = [
    "crop_raiding", "livestock_attack", "human_injury", "human_death", "property_damage",
    "animal_in_settlement", "animal_injured", "other",
]

#: Conflict types that make an HWC alert ``critical`` on their own (spec v1.5 §A5).
CRITICAL_CONFLICT_TYPES = {"human_injury", "human_death"}

#: key -> (kind, limit). ``limit`` is max_length for strings and (min, max) for integers.
_STRINGS = {
    "species_name": 120,
    "location_description": 200,
    "reporter_name": 120,
    "reporter_phone": 32,
    "action_taken": 500,
    "notes": 1000,
}
_INTEGERS = {
    "animal_count": (0, 100000),
    "people_affected": (0, 1_000_000),
    "people_injured": (0, 1_000_000),
    "people_killed": (0, 1_000_000),
    "livestock_lost": (0, 1_000_000),
}

DETAIL_FIELDS = ["conflict_type", "species_id", *_STRINGS, *_INTEGERS, "logged_at"]

_dt_field = serializers.DateTimeField()


def _clean_string(key: str, value, warnings: list[str]):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        warnings.append(f"{key}: expected a string")
        return None
    text = sanitize_text(str(value))
    if not text:
        return None
    limit = _STRINGS[key]
    if len(text) > limit:
        warnings.append(f"{key}: truncated to {limit} characters")
        text = text[:limit].rstrip()
    return text or None


def _clean_integer(key: str, value, warnings: list[str]):
    low, high = _INTEGERS[key]
    if isinstance(value, bool):
        warnings.append(f"{key}: expected an integer")
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        warnings.append(f"{key}: expected an integer")
        return None
    if not (low <= number <= high):
        warnings.append(f"{key}: must be between {low} and {high}")
        return None
    return number


def clean_details(raw, warnings: list[str] | None = None) -> tuple[dict, list[str]]:
    """
    Validate an incoming ``details`` object leniently.

    Returns ``(cleaned, warnings)``. ``cleaned`` only contains keys that survived and are not
    ``None``; a key present but null in the request is simply absent from the result (so a merge
    never wipes a previously logged value). ``warnings`` are human-readable ``"field: reason"``
    strings for the API's additive ``details_warnings``.
    """
    warnings = warnings if warnings is not None else []
    if raw is None:
        return {}, warnings
    if not isinstance(raw, dict):
        warnings.append("details: expected an object")
        return {}, warnings

    cleaned: dict = {}
    for key in sorted(set(raw) - set(DETAIL_FIELDS)):
        warnings.append(f"{key}: unknown field")

    for key in DETAIL_FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        if value is None:
            continue
        if key == "conflict_type":
            text = value if isinstance(value, str) else ""
            text = text.strip().lower().replace(" ", "_").replace("-", "_")
            if text in CONFLICT_TYPES:
                cleaned[key] = text
            else:
                warnings.append(f"{key}: must be one of {', '.join(CONFLICT_TYPES)}")
        elif key == "species_id":
            import uuid as _uuid

            try:
                cleaned[key] = str(_uuid.UUID(str(value)))
            except (TypeError, ValueError, AttributeError):
                warnings.append(f"{key}: expected a UUID")
        elif key == "logged_at":
            try:
                cleaned[key] = _dt_field.to_representation(_dt_field.to_internal_value(value))
            except (serializers.ValidationError, TypeError, ValueError):
                warnings.append(f"{key}: expected an ISO-8601 datetime")
        elif key in _STRINGS:
            text = _clean_string(key, value, warnings)
            if text is not None:
                cleaned[key] = text
        else:
            number = _clean_integer(key, value, warnings)
            if number is not None:
                cleaned[key] = number
    return cleaned, warnings


def merge_details(stored: dict | None, incoming: dict) -> dict:
    """Non-null keys of ``incoming`` win; everything already logged is preserved."""
    merged = dict(stored or {})
    merged.update(incoming)
    return merged


def resolve_species(details: dict) -> dict:
    """Fill ``species_name`` from the catalogue when only ``species_id`` was sent (and vice versa is left alone)."""
    species_id = details.get("species_id")
    if not species_id or details.get("species_name"):
        return details
    from .models import Species

    name = Species.objects.filter(pk=species_id).values_list("common_name", flat=True).first()
    if name:
        details = {**details, "species_name": name}
    return details


def is_critical(details: dict | None) -> bool:
    """Spec v1.5 §A5: human injury/death — by conflict type or by the casualty counts."""
    d = details or {}
    if d.get("conflict_type") in CRITICAL_CONFLICT_TYPES:
        return True
    return bool((d.get("people_injured") or 0) > 0 or (d.get("people_killed") or 0) > 0)


def humanise(conflict_type: str | None) -> str:
    return (conflict_type or "").replace("_", " ").strip()


def alert_title(kind: str, details: dict | None) -> str:
    """``alerts/`` item title. HWC: ``"Human–wildlife conflict · African Elephant · crop raiding"``."""
    if kind == "panic":
        return "Panic button"
    if kind == "dead_mans_switch":
        return "Dead man's switch"
    if kind != "human_wildlife_conflict":
        return kind.replace("_", " ").capitalize()
    parts = ["Human–wildlife conflict"]
    d = details or {}
    if d.get("species_name"):
        parts.append(str(d["species_name"]))
    if d.get("conflict_type"):
        parts.append(humanise(d["conflict_type"]))
    return " · ".join(parts)


def details_summary(details: dict | None) -> str:
    """One-line summary used in the ``PATROLIQ HWC DETAILS`` notification (spec v1.5 §A4)."""
    d = details or {}
    bits = []
    if d.get("conflict_type"):
        bits.append(humanise(d["conflict_type"]))
    if d.get("species_name"):
        bits.append(str(d["species_name"]))
    if d.get("animal_count") is not None:
        bits.append(f"{d['animal_count']} animal(s)")
    casualties = []
    for key, label in (("people_injured", "injured"), ("people_killed", "killed")):
        if d.get(key):
            casualties.append(f"{d[key]} {label}")
    if d.get("livestock_lost"):
        casualties.append(f"{d['livestock_lost']} livestock lost")
    if casualties:
        bits.append(", ".join(casualties))
    if d.get("location_description"):
        bits.append(str(d["location_description"]))
    return "; ".join(bits) if bits else "no further detail given"
