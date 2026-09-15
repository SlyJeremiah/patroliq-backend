"""
Licence enforcement (spec §1).

Effective status of an organisation:
    suspended  if zrGISsolutions set organisation.status or licence.status to ``suspended``,
               there is no licence, or now > expires_at + grace_days
    grace      if now > expires_at (sync works, app shows a banner), or status manually ``grace``
    active     otherwise

Seats (HTTP 402 ``licence_seat_limit``):
    ranger seats  = active users with role ``ranger``                     <= max_rangers
    manager seats = active users with any other organisation role
                    (org_admin, manager, researcher, viewer)               <= max_managers
Areas (HTTP 402 ``licence_area_limit``): non-archived areas <= max_areas.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from core.exceptions import ApiError
from core.permissions import RANGER


def get_licence(organisation):
    if organisation is None:
        return None
    try:
        return organisation.licence
    except Exception:  # RelatedObjectDoesNotExist
        return None


def effective_status(organisation, now=None) -> str:
    if organisation is None:
        return "active"
    now = now or timezone.now()
    licence = get_licence(organisation)
    if organisation.status == "suspended" or licence is None or licence.status == "suspended":
        return "suspended"
    if now > licence.expires_at + timedelta(days=licence.grace_days):
        return "suspended"
    if now > licence.expires_at or organisation.status == "grace" or licence.status == "grace":
        return "grace"
    return "active"


def module_enabled(organisation, module: str) -> bool:
    licence = get_licence(organisation)
    return bool(licence and module in (licence.modules or []))


def seat_kind(role: str) -> str:
    return "ranger" if role == RANGER else "manager"


def seat_usage(organisation) -> dict:
    from .models import User

    active = User.objects.filter(organisation=organisation, is_active=True)
    rangers = active.filter(role=RANGER).count()
    return {"rangers": rangers, "managers": active.count() - rangers}


def check_seat_available(organisation, role: str, exclude_user=None) -> None:
    """Raise 402 if adding one more active user with ``role`` would exceed the licence."""
    from .models import User

    licence = get_licence(organisation)
    if licence is None:
        raise ApiError(402, "licence_seat_limit", "The organisation has no licence.")
    qs = User.objects.filter(organisation=organisation, is_active=True)
    if exclude_user is not None:
        qs = qs.exclude(pk=exclude_user.pk)
    if seat_kind(role) == "ranger":
        used, limit, label = qs.filter(role=RANGER).count(), licence.max_rangers, "ranger"
    else:
        used, limit, label = qs.exclude(role=RANGER).count(), licence.max_managers, "manager"
    if used + 1 > limit:
        raise ApiError(402, "licence_seat_limit", f"Licence allows {limit} active {label} seat(s); all are in use.")


def check_area_available(organisation) -> None:
    from areas.models import Area

    licence = get_licence(organisation)
    limit = licence.max_areas if licence else 0
    used = Area.objects.for_org(organisation).exclude(status="archived").count()
    if used + 1 > limit:
        raise ApiError(402, "licence_area_limit", f"Licence allows {limit} area(s); all are in use.")
