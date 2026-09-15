from __future__ import annotations

import logging

from core.permissions import MANAGERS

from .backends import get_backend
from .models import NotificationLog

logger = logging.getLogger("patroliq.notify")


def user_push_topic(user) -> str:
    return f"user-{user.pk}"


def notify_user(user, title: str, body: str, data: dict | None = None) -> str:
    """
    Message one person: SMS when they have a phone number, otherwise a push to their personal topic
    ``user-<id>``. Like :func:`notify_managers`, provider failures are logged/recorded, never raised.
    Returns the channel used (``sms`` | ``push``).
    """
    backend = get_backend()
    data = data or {}
    channel = "sms" if (user.phone or "").strip() else "push"
    to = user.phone if channel == "sms" else user_push_topic(user)
    ok, err = True, ""
    try:
        if channel == "sms":
            backend.send_sms(user.phone, f"{title}: {body}")
        else:
            backend.send_push(to, title, body, data)
    except Exception as exc:  # noqa: BLE001 — deliberately broad
        ok, err = False, str(exc)
        logger.error("%s to %s failed: %s", channel, user.pk, exc)
    NotificationLog.objects.create(
        organisation_id=user.organisation_id, recipient_id=user.pk, channel=channel, to=to or "",
        title=title, body=body, data=data, backend=backend.name, success=ok, error=err,
    )
    return channel


def notify_managers(organisation_id, title: str, body: str, data: dict | None = None) -> int:
    """
    SMS every active manager/org_admin with a phone number and push to the organisation's manager
    topic. Failures are logged and recorded but never raised: a provider outage must not make the
    ranger's SOS request fail. Returns the number of NotificationLog rows written.
    """
    from accounts.models import User

    backend = get_backend()
    data = data or {}
    written = 0
    managers = User.objects.filter(organisation_id=organisation_id, is_active=True, role__in=MANAGERS)
    for manager in managers:
        ok, err = True, ""
        try:
            backend.send_sms(manager.phone, f"{title}: {body}")
        except Exception as exc:  # noqa: BLE001 — deliberately broad, see docstring
            ok, err = False, str(exc)
            logger.error("SMS to %s failed: %s", manager.pk, exc)
        NotificationLog.objects.create(
            organisation_id=organisation_id, recipient_id=manager.pk, channel="sms", to=manager.phone,
            title=title, body=body, data=data, backend=backend.name, success=ok, error=err,
        )
        written += 1

    topic = f"org-{organisation_id}-managers"
    ok, err = True, ""
    try:
        backend.send_push(topic, title, body, data)
    except Exception as exc:  # noqa: BLE001
        ok, err = False, str(exc)
        logger.error("Push to %s failed: %s", topic, exc)
    NotificationLog.objects.create(
        organisation_id=organisation_id, channel="push", to=topic, title=title, body=body, data=data,
        backend=backend.name, success=ok, error=err,
    )
    return written + 1
