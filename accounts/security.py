"""
Login hardening: lockout (PRD 7.2: 5 failures -> 15-minute lockout) and TOTP (RFC 6238).
"""
from __future__ import annotations

import time
from datetime import timedelta

import pyotp
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import LoginLockout


def ranger_identifier(org_code: str, employee_id: str) -> str:
    return f"ranger:{org_code.strip().upper()}:{employee_id.strip().upper()}"


def web_identifier(email: str) -> str:
    return f"web:{email.strip().lower()}"


def lockout_remaining(identifier: str) -> int:
    """Seconds left on an active lockout, else 0."""
    row = LoginLockout.objects.filter(identifier=identifier).first()
    if row and row.locked_until and row.locked_until > timezone.now():
        return int((row.locked_until - timezone.now()).total_seconds()) + 1
    return 0


def register_failure(identifier: str) -> int:
    """Count a failure; returns lockout seconds if this failure triggered a lockout, else 0."""
    now = timezone.now()
    with transaction.atomic():
        row, _ = LoginLockout.objects.select_for_update().get_or_create(identifier=identifier)
        if row.locked_until and row.locked_until <= now:
            row.failures, row.locked_until = 0, None  # previous lockout served
        row.failures += 1
        row.last_failure_at = now
        locked = 0
        if row.failures >= settings.LOGIN_MAX_FAILURES:
            row.locked_until = now + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
            locked = settings.LOGIN_LOCKOUT_MINUTES * 60
        row.save()
    return locked


def clear_failures(identifier: str) -> None:
    LoginLockout.objects.filter(identifier=identifier).delete()


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(user) -> str:
    label = user.email or user.employee_id or str(user.pk)
    return pyotp.TOTP(user.totp_secret).provisioning_uri(name=label, issuer_name=settings.TOTP_ISSUER)


def verify_totp(user, code: str) -> bool:
    """Accept the current step ±1 (clock drift); reject re-use of an already accepted step."""
    if not user.totp_secret or not code:
        return False
    code = str(code).strip().replace(" ", "")
    if not code.isdigit():
        return False
    totp = pyotp.TOTP(user.totp_secret)
    now_step = int(time.time()) // totp.interval
    for offset in (0, -1, 1):
        step = now_step + offset
        if totp.at(step * totp.interval) == code:
            if user.totp_last_step is not None and step <= user.totp_last_step:
                return False
            user.totp_last_step = step
            user.save(update_fields=["totp_last_step"])
            return True
    return False
