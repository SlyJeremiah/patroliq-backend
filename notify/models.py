import uuid

from django.db import models


class NotificationLog(models.Model):
    """Every outbound SMS/push attempt (delivery evidence for safety alerts)."""

    CHANNELS = [("sms", "SMS"), ("push", "Push")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation_id = models.UUIDField(null=True, blank=True, db_index=True)
    recipient_id = models.UUIDField(null=True, blank=True)
    channel = models.CharField(max_length=8, choices=CHANNELS)
    to = models.CharField(max_length=255, blank=True, default="")
    title = models.CharField(max_length=200, blank=True, default="")
    body = models.TextField()
    data = models.JSONField(default=dict, blank=True)
    backend = models.CharField(max_length=32)
    success = models.BooleanField(default=True)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
