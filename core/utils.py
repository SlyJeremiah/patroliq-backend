from __future__ import annotations

from datetime import date, datetime, timezone as dt_timezone

from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import serializers


def query_date(request, name: str) -> date | None:
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return None
    value = parse_date(raw)
    if value is None:
        raise serializers.ValidationError({name: ["Expected YYYY-MM-DD."]})
    return value


def query_datetime(request, name: str) -> datetime | None:
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return None
    raw = raw.strip().replace(" ", "+")  # '+' in an unencoded query string arrives as a space
    try:
        value = parse_datetime(raw)
    except ValueError:
        value = None
    if value is None:
        d = parse_date(raw) if len(raw) == 10 else None
        if d is None:
            raise serializers.ValidationError({name: ["Expected an ISO-8601 datetime, e.g. 2026-09-15T08:00:00Z."]})
        value = datetime(d.year, d.month, d.day)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_timezone.utc)
    return value


def query_uuid(request, name: str):
    import uuid

    raw = request.query_params.get(name)
    if raw in (None, ""):
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise serializers.ValidationError({name: ["Expected a UUID."]})
