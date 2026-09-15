from __future__ import annotations

import logging

from .models import AuditLog

logger = logging.getLogger("patroliq.audit")


def client_ip(request) -> str | None:
    """REMOTE_ADDR; X-Forwarded-For only when a trusted proxy count is configured."""
    if request is None:
        return None
    from django.conf import settings

    hops = getattr(settings, "TRUSTED_PROXY_HOPS", 0)
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if hops and xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    return request.META.get("REMOTE_ADDR")


def audit(request, action: str, target=None, detail: dict | None = None, *, actor=None, organisation_id=None,
          target_type: str | None = None, target_id=None) -> AuditLog:
    user = actor
    if user is None and request is not None:
        u = getattr(request, "user", None)
        user = u if u is not None and getattr(u, "is_authenticated", False) else None
    if organisation_id is None:
        organisation_id = getattr(target, "organisation_id", None) or getattr(user, "organisation_id", None)
    if target is not None:
        target_type = target_type or target._meta.label_lower
        target_id = target_id or target.pk
    entry = AuditLog(
        organisation_id=organisation_id,
        actor_id=getattr(user, "pk", None),
        actor_label=(str(user) if user is not None else "")[:255],
        action=action,
        target_type=target_type or "",
        target_id=str(target_id or ""),
        ip=client_ip(request),
        detail=detail or {},
    )
    if organisation_id is not None:
        # PostgreSQL RLS: pre-auth requests (login) have no app.org_id yet; scope just this INSERT.
        from core.db import tenant_context

        with tenant_context(organisation_id):
            entry.save()
    else:
        entry.save()
    return entry
