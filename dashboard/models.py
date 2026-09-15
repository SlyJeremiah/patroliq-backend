"""Generated reports and their share links (spec §7 "Reports")."""
import secrets
import uuid

from django.conf import settings
from django.db import models

from core.models import TenantModel, TimeStampedModel

REPORT_TYPES = ["patrol_summary", "incident_report", "wildlife_census", "threat_intelligence", "grts_survey",
                "ranger_performance", "donor_report", "zpwma_compliance"]
REPORT_FORMATS = ["pdf", "csv", "geojson"]
EXTENSIONS = {"pdf": ".pdf", "csv": ".csv", "geojson": ".geojson"}
CONTENT_TYPES = {"pdf": "application/pdf", "csv": "text/csv; charset=utf-8", "geojson": "application/geo+json"}


def report_upload_to(instance, filename):
    """<storage>/reports/<organisation id>/<report id>.<ext> — never a client-supplied name."""
    return f"reports/{instance.organisation_id}/{instance.id}{EXTENSIONS.get(instance.format, '')}"


class Report(TenantModel, TimeStampedModel):
    STATUSES = [("ready", "Ready"), ("failed", "Failed")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=32, choices=[(t, t) for t in REPORT_TYPES])
    format = models.CharField(max_length=8, choices=[(f, f) for f in REPORT_FORMATS])
    status = models.CharField(max_length=8, choices=STATUSES, default="ready")
    title = models.CharField(max_length=255)
    params = models.JSONField(default=dict, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    anonymised = models.BooleanField(default=False)
    file = models.FileField(upload_to=report_upload_to, max_length=255, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    error = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-created_at"]


def new_share_token() -> str:
    return secrets.token_urlsafe(32)


class ReportShare(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="shares")
    token = models.CharField(max_length=64, unique=True, default=new_share_token)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]
