"""
Input validation helpers (PRD 6.4 / 7.2).

* :class:`StrictSerializerMixin` — any request key the serializer does not declare is rejected
  with ``400 unexpected_fields``. Declared read-only keys (e.g. ``id`` echoed back by a client)
  are tolerated and ignored, because they are not *unknown*.
* :class:`SanitizedCharField` — strips HTML/script markup *before* enforcing max length, so
  ``notes`` is always plain text of at most 1000 characters.
"""
from __future__ import annotations

import re

from django.utils.html import strip_tags
from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(value: str) -> str:
    """Remove script/style blocks entirely, then all remaining tags and control characters."""
    if value is None:
        return value
    value = _SCRIPT_STYLE_RE.sub("", str(value))
    # strip_tags is iterative (handles nested/malformed tags); unescape nothing, output is plain text.
    value = strip_tags(value)
    value = _CONTROL_RE.sub("", value)
    return value.strip()


class SanitizedCharField(serializers.CharField):
    def __init__(self, **kwargs):
        kwargs.setdefault("allow_blank", True)
        kwargs.setdefault("trim_whitespace", True)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        if isinstance(data, bool) or not isinstance(data, (str, int, float)):
            self.fail("invalid")
        cleaned = sanitize_text(str(data))
        # max_length validators run after to_internal_value, i.e. on the stripped text.
        return super().to_internal_value(cleaned)


def unexpected_fields_error(keys) -> serializers.ValidationError:
    return serializers.ValidationError(
        {k: [ErrorDetail("Unexpected field.", code="unexpected_fields")] for k in sorted(keys)}
    )


class StrictSerializerMixin:
    """Reject keys that are not declared on the serializer."""

    def to_internal_value(self, data):
        if isinstance(data, dict) or hasattr(data, "keys"):
            allowed = set(self.fields.keys())
            unknown = set(data.keys()) - allowed
            if unknown:
                raise unexpected_fields_error(unknown)
        return super().to_internal_value(data)


class StrictSerializer(StrictSerializerMixin, serializers.Serializer):
    pass


class StrictModelSerializer(StrictSerializerMixin, serializers.ModelSerializer):
    pass


def validate_point(value) -> dict:
    """GeoJSON Point with WGS84 [lon, lat]."""
    if not isinstance(value, dict) or value.get("type") != "Point":
        raise serializers.ValidationError("Must be a GeoJSON Point.")
    coords = value.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        raise serializers.ValidationError("Point coordinates must be [lon, lat].")
    try:
        lon, lat = float(coords[0]), float(coords[1])
    except (TypeError, ValueError):
        raise serializers.ValidationError("Point coordinates must be numbers.")
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise serializers.ValidationError("Point coordinates out of range.")
    return {"type": "Point", "coordinates": [lon, lat]}


class PointField(serializers.JSONField):
    def to_internal_value(self, data):
        return validate_point(super().to_internal_value(data))


def reject_unexpected(data, allowed: set[str]):
    if hasattr(data, "keys"):
        unknown = set(data.keys()) - allowed
        if unknown:
            raise unexpected_fields_error(unknown)
