from rest_framework import generics, serializers

from core.permissions import MANAGERS, roles_allowed
from core.tenancy import request_org
from core.utils import query_date

from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = ["id", "organisation_id", "actor_id", "actor_label", "action", "target_type", "target_id", "ip",
                  "created_at", "detail"]


class AuditLogListView(generics.ListAPIView):
    """GET audit-log/ — org_admin + manager, read-only, own organisation only."""

    serializer_class = AuditLogSerializer
    permission_classes = [roles_allowed(MANAGERS)]

    def get_queryset(self):
        qs = AuditLog.objects.for_org(request_org(self.request))
        p = self.request.query_params
        if p.get("action"):
            qs = qs.filter(action=p["action"])
        if d := query_date(self.request, "date_from"):
            qs = qs.filter(created_at__date__gte=d)
        if d := query_date(self.request, "date_to"):
            qs = qs.filter(created_at__date__lte=d)
        return qs
