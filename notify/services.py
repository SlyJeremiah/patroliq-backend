"""
Outbound notifications: SMS + push (provider backends) and email (SMTP), spec v1.6.

Callers only *queue* work: recipients are resolved and content rendered inside the request (where
PostgreSQL's tenant context is set), then :func:`notify.jobs.dispatch` delivers after commit on a
background thread. Every attempt — success or failure — is written to ``NotificationLog``; nothing
here ever raises into the caller, so a provider outage cannot fail a ranger's SOS or sync.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.utils import timezone

from core.db import tenant_context
from core.permissions import MANAGERS

from .backends import get_backend
from .jobs import dispatch
from .mail import EmailContent, email_configured, email_status, render_email, send_emails, simple_email
from .models import NotificationLog
from .phones import mask_email, mask_phone, mask_recipient, normalise_phone

logger = logging.getLogger("patroliq.notify")

NO_PHONE = "no manager has a phone number"
NO_EMAIL = "no manager has an email address"
INVALID_PHONE = "invalid phone number"


def user_push_topic(user) -> str:
    return f"user-{user.pk}"


def _log(organisation_id, channel, to, title, body, data, backend, ok, err, recipient_id=None) -> None:
    try:
        with tenant_context(organisation_id):
            NotificationLog.objects.create(
                organisation_id=organisation_id, recipient_id=recipient_id, channel=channel, to=(to or "")[:255],
                title=(title or "")[:200], body=body, data=data or {}, backend=backend, success=ok, error=err or "",
            )
    except Exception:  # noqa: BLE001 — logging the attempt must not break delivery of the next one
        logger.exception("could not write NotificationLog (%s to %s)", channel, recipient_id)


def _email_ok(address) -> bool:
    if not address:
        return False
    try:
        validate_email(address)
    except DjangoValidationError:
        return False
    return True


def manager_recipients(organisation_id):
    from accounts.models import User

    return list(User.objects.filter(organisation_id=organisation_id, is_active=True, role__in=MANAGERS)
                .order_by("full_name"))


# --- delivery jobs (run after commit, possibly on a worker thread) -------------------------------------

def _deliver_emails(organisation_id, targets: list[tuple], content: EmailContent, data: dict) -> None:
    backend = "smtp" if email_configured() else "console"
    if not targets:
        _log(organisation_id, "email", "", content.subject, content.text, data, backend, False, NO_EMAIL)
        return
    results = send_emails([address for _, address in targets], content)
    for (recipient_id, address), (ok, err) in zip(targets, results):
        _log(organisation_id, "email", address, content.subject, content.text, data, backend, ok, err, recipient_id)


def _deliver_manager_alert(organisation_id, title, body, data, sms_targets, invalid_phones, email_targets,
                           email_content) -> None:
    backend = get_backend()
    if not sms_targets:
        _log(organisation_id, "sms", "", title, body, data, backend.name, False, NO_PHONE)
    else:
        for recipient_id, raw in invalid_phones:
            _log(organisation_id, "sms", raw, title, body, data, backend.name, False, INVALID_PHONE, recipient_id)
        for recipient_id, phone in sms_targets:
            ok, err = True, ""
            try:
                backend.send_sms(phone, f"{title}: {body}")
            except Exception as exc:  # noqa: BLE001 — deliberately broad, see module docstring
                ok, err = False, str(exc)
                logger.error("SMS to %s failed: %s", recipient_id, exc)
            _log(organisation_id, "sms", phone, title, body, data, backend.name, ok, err, recipient_id)

    topic = f"org-{organisation_id}-managers"
    ok, err = True, ""
    try:
        backend.send_push(topic, title, body, data)
    except Exception as exc:  # noqa: BLE001
        ok, err = False, str(exc)
        logger.error("Push to %s failed: %s", topic, exc)
    _log(organisation_id, "push", topic, title, body, data, backend.name, ok, err)

    if email_content is not None:
        _deliver_emails(organisation_id, email_targets, email_content, data)


def _deliver_user(organisation_id, recipient_id, channel, to, title, body, data) -> None:
    backend = get_backend()
    ok, err = True, ""
    try:
        if channel == "sms":
            backend.send_sms(to, f"{title}: {body}")
        else:
            backend.send_push(to, title, body, data)
    except Exception as exc:  # noqa: BLE001 — deliberately broad
        ok, err = False, str(exc)
        logger.error("%s to %s failed: %s", channel, recipient_id, exc)
    _log(organisation_id, channel, to, title, body, data, backend.name, ok, err, recipient_id)


# --- public API ------------------------------------------------------------------------------------------

def notify_user(user, title: str, body: str, data: dict | None = None) -> str:
    """
    Message one person: SMS when they have a valid phone number (normalised to E.164), otherwise a
    push to their personal topic ``user-<id>``. Delivery is queued; returns the channel chosen.
    """
    data = data or {}
    phone = normalise_phone(user.phone)
    channel = "sms" if phone else "push"
    to = phone if channel == "sms" else user_push_topic(user)
    dispatch(_deliver_user, user.organisation_id, user.pk, channel, to, title, body, data)
    return channel


def notify_managers(organisation_id, title: str, body: str, data: dict | None = None,
                    email: EmailContent | None = None) -> int:
    """
    Alert every active manager/org_admin of the organisation: SMS to each valid phone, a push to the
    organisation's manager topic and (``EMAIL_ALERTS``) an email to each address — ``email`` is the
    fuller rendered message; without it a plain one is built from ``title``/``body``.

    With no valid phone at all, one ``no manager has a phone number`` row is written instead of
    per-user noise (email still goes out). Returns the number of recipients queued.
    """
    data = data or {}
    managers = manager_recipients(organisation_id)
    sms_targets, invalid = [], []
    for m in managers:
        if not (m.phone or "").strip():
            continue
        phone = normalise_phone(m.phone)
        if phone:
            sms_targets.append((m.pk, phone))
        else:
            invalid.append((m.pk, m.phone))
    email_targets = [(m.pk, m.email) for m in managers if _email_ok(m.email)]
    email_content = None
    if getattr(settings, "EMAIL_ALERTS", True):
        try:
            email_content = email or simple_email(title, body)
        except Exception:  # noqa: BLE001 — a template problem must not stop the SMS
            logger.exception("could not render alert email")
    dispatch(_deliver_manager_alert, organisation_id, title, body, data, sms_targets, invalid, email_targets,
             email_content)
    return len(sms_targets) + len(email_targets)


def email_managers(organisation_id, content: EmailContent, data: dict | None = None) -> int:
    """Email only (sync summaries). Returns the number of recipients queued."""
    targets = [(m.pk, m.email) for m in manager_recipients(organisation_id) if _email_ok(m.email)]
    dispatch(_deliver_emails, organisation_id, targets, content, data or {})
    return len(targets)


# --- notification health (spec v1.6 §4) -------------------------------------------------------------

def _iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def recipient_row(user) -> dict:
    raw_phone = (user.phone or "").strip()
    phone_ok = bool(normalise_phone(raw_phone)) if raw_phone else False
    email_ok = _email_ok(user.email)
    problems = []
    if not raw_phone:
        problems.append("no_phone")
    elif not phone_ok:
        problems.append("invalid_phone")
    if not email_ok:
        problems.append("no_email")
    return {"id": str(user.pk), "full_name": user.full_name, "role": user.role,
            "phone_masked": mask_phone(raw_phone), "phone_ok": phone_ok,
            "email_masked": mask_email(user.email), "email_ok": email_ok, "problems": problems}


def notification_status(organisation_id) -> dict:
    backend = get_backend()
    sms = {"backend": backend.name, **backend.sms_status()}
    push_ok = backend.push_configured()
    recipients = [recipient_row(u) for u in manager_recipients(organisation_id)]
    email = email_status()

    problems = []
    if not any(r["phone_ok"] for r in recipients):
        problems.append("no_sms_recipient")
    if not any(r["email_ok"] for r in recipients):
        problems.append("no_email_recipient")
    if not sms["configured"]:
        problems.append("sms_not_configured")
    if not email["configured"]:
        problems.append("email_not_configured")

    logs = NotificationLog.objects.filter(organisation_id=organisation_id)
    last_success = {
        channel: _iso(logs.filter(channel=channel, success=True).order_by("-created_at")
                      .values_list("created_at", flat=True).first())
        for channel in ("sms", "email")
    }
    failures = logs.filter(success=False, created_at__gte=timezone.now() - timedelta(days=7)).exclude(
        error__startswith="skipped:")
    if not push_ok:
        failures = failures.exclude(channel="push")  # the FCM stub fails on every alert until configured
    recent = [
        {"at": _iso(f.created_at), "channel": f.channel, "title": f.title, "to_masked": mask_recipient(f.channel, f.to),
         "error": f.error}
        for f in failures.order_by("-created_at")[:10]
    ]
    return {"sms": sms, "email": email, "push": {"configured": push_ok}, "recipients": recipients,
            "problems": problems, "last_success": last_success, "recent_failures": recent}


def send_test(user, channel: str) -> dict:
    """Synchronous test message to the caller's own phone/email; always logged."""
    org_name = user.organisation.name if user.organisation_id else "PATROLIQ"
    data = {"type": "test"}
    title = "PATROLIQ test message"
    if channel == "sms":
        backend = get_backend()
        raw = (user.phone or "").strip()
        phone = normalise_phone(raw)
        to = phone or raw
        body = f"SMS alerts from {org_name} reach this number."
        ok, err = True, ""
        if not raw:
            ok, err = False, "recipient has no phone number"
        elif not phone:
            ok, err = False, INVALID_PHONE
        else:
            try:
                backend.send_sms(phone, f"{title}: {body}")
            except Exception as exc:  # noqa: BLE001
                ok, err = False, str(exc)
        _log(user.organisation_id, "sms", to, title, body, data, backend.name, ok, err, user.pk)
        return {"ok": ok, "channel": "sms", "to_masked": mask_phone(to), "error": err or None}

    address = (user.email or "").strip()
    backend = "smtp" if email_configured() else "console"
    content = render_email("test", title, {"title": title, "org_name": org_name, "full_name": user.full_name})
    if not _email_ok(address):
        ok, err = False, "recipient has no email address"
    else:
        ok, err = send_emails([address], content)[0]
    _log(user.organisation_id, "email", address, content.subject, content.text, data, backend, ok, err, user.pk)
    return {"ok": ok, "channel": "email", "to_masked": mask_email(address), "error": err or None}

