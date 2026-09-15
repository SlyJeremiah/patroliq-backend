"""
Application-layer tenant isolation (spec §1 rule 1, PRD 6.3 layer 2).

* :class:`TenantScopedMixin` — every viewset's queryset is filtered by the requesting user's
  organisation *before* any lookup, so an ID belonging to another organisation is simply a 404.
  New rows get ``organisation`` from the user, never from the request body.
* :class:`TenantPKField` — foreign-key inputs (``area_id``, ``team_id`` …) resolve only within the
  caller's organisation; a foreign ID fails validation exactly like a non-existent one.
"""
from __future__ import annotations

from rest_framework import serializers


def request_org(context_or_request):
    request = context_or_request.get("request") if isinstance(context_or_request, dict) else context_or_request
    user = getattr(request, "user", None)
    return getattr(user, "organisation", None) if user is not None and user.is_authenticated else None


class TenantScopedMixin:
    """Mix into GenericAPIView subclasses. Set ``queryset`` or override ``base_queryset()``."""

    def base_queryset(self):
        return self.queryset.all()

    def get_queryset(self):
        org = request_org(self.request)
        qs = self.base_queryset()
        if org is None:
            return qs.none()
        return self.scope_queryset(qs.filter(organisation_id=org.pk))

    def scope_queryset(self, qs):
        """Hook for role-based narrowing inside the organisation."""
        return qs

    def perform_create(self, serializer):
        serializer.save(organisation=self.request.user.organisation)


class TenantPKField(serializers.PrimaryKeyRelatedField):
    """PrimaryKeyRelatedField restricted to the request user's organisation."""

    def __init__(self, model=None, extra_filter=None, **kwargs):
        self.model = model
        self.extra_filter = extra_filter or {}
        if not kwargs.get("read_only"):
            kwargs.setdefault("queryset", model.objects.all())
        super().__init__(**kwargs)

    def get_queryset(self):
        org = request_org(self.context)
        qs = self.model.objects.filter(**self.extra_filter)
        return qs.filter(organisation_id=org.pk) if org is not None else qs.none()
