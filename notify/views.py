"""Notification health API for the dashboard (spec v1.6 §4)."""
from __future__ import annotations

from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from audit.utils import audit
from core.permissions import MANAGERS, roles_allowed
from core.validation import StrictSerializer

from .services import notification_status, send_test


class NotifyStatusView(APIView):
    """GET notify/status/ — channel configuration, manager recipients (masked), recent failures."""

    permission_classes = [roles_allowed(read=MANAGERS, write=MANAGERS)]

    def get(self, request):
        return Response(notification_status(request.user.organisation_id))


class NotifyTestSerializer(StrictSerializer):
    channel = serializers.ChoiceField(choices=["sms", "email"])


class NotifyTestView(APIView):
    """POST notify/test/ {channel} — test message to the caller's own phone/email; 5 per hour per user."""

    permission_classes = [roles_allowed(read=MANAGERS, write=MANAGERS)]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "notify_test"

    def post(self, request):
        s = NotifyTestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        result = send_test(request.user, s.validated_data["channel"])
        audit(request, "notify.test", target=request.user,
              detail={"channel": result["channel"], "ok": result["ok"], "error": result["error"]})
        return Response(result)
