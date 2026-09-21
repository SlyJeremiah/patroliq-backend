"""
Email channel (spec v1.6 §1) over Django's email framework.

SMTP settings come from the environment (``EMAIL_HOST`` … ``DEFAULT_FROM_EMAIL``, see settings.py).
Email counts as *configured* only when ``EMAIL_HOST`` is set; otherwise Django's console backend
prints the message (dev) and the attempt is logged as skipped. Messages are multipart: a plain-text
body plus a simple branded HTML body rendered from ``notify/templates/notify/email/<name>.{txt,html}``
(no external images, no personal-record fields).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string

logger = logging.getLogger("patroliq.notify")

SKIPPED = "skipped: email is not configured (EMAIL_HOST is not set)"


@dataclass(frozen=True)
class EmailContent:
    subject: str
    text: str
    html: str


def email_configured() -> bool:
    return bool((getattr(settings, "EMAIL_HOST", "") or "").strip())


def email_status() -> dict:
    return {
        "configured": email_configured(),
        "host": (settings.EMAIL_HOST or None) if email_configured() else None,
        "from_email": settings.DEFAULT_FROM_EMAIL,
        "alerts": bool(getattr(settings, "EMAIL_ALERTS", True)),
        "sync_summaries": bool(getattr(settings, "EMAIL_SYNC_SUMMARIES", True)),
    }


def render_email(template: str, subject: str, context: dict) -> EmailContent:
    ctx = {"subject": subject, "dashboard_url": getattr(settings, "DASHBOARD_URL", "") or "", **context}
    text = render_to_string(f"notify/email/{template}.txt", ctx).strip() + "\n"
    html = render_to_string(f"notify/email/{template}.html", ctx)
    return EmailContent(subject=" ".join(subject.split())[:200], text=text, html=html)


def simple_email(title: str, body: str) -> EmailContent:
    """Fallback for callers that only have an SMS-style title + body."""
    return render_email("alert", title, {"title": title, "headline": body, "rows": [], "critical": False})


def _error_text(exc: Exception) -> str:
    # SMTP exceptions carry the server's reply (never our password); keep it short.
    return f"{type(exc).__name__}: {exc}"[:500]


def send_emails(recipients: list[str], content: EmailContent) -> list[tuple[bool, str]]:
    """
    Send ``content`` to each address separately (recipients never see each other) over one
    connection. Returns ``[(ok, error)]`` in the same order. Never raises.
    """
    if not recipients:
        return []
    results: list[tuple[bool, str]] = []
    configured = email_configured()
    try:
        connection = get_connection(fail_silently=False)
        connection.open()
    except Exception as exc:  # noqa: BLE001 — e.g. SMTP connect/auth failure
        logger.error("email connection failed: %s", exc)
        return [(False, _error_text(exc))] * len(recipients)
    try:
        for address in recipients:
            msg = EmailMultiAlternatives(content.subject, content.text, settings.DEFAULT_FROM_EMAIL, [address],
                                         connection=connection)
            msg.attach_alternative(content.html, "text/html")
            try:
                msg.send()
                results.append((True, "") if configured else (False, SKIPPED))
            except Exception as exc:  # noqa: BLE001
                logger.error("email to %s failed: %s", address, exc)
                results.append((False, _error_text(exc)))
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass
    return results
