"""Manager dashboard endpoints (Platform Spec §7). Paths are registered in ``patroliq/api_urls.py``."""
from __future__ import annotations

import csv
import io
from datetime import timedelta

from django.conf import settings
from django.http import FileResponse, HttpResponse
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

import geo
from accounts.models import User
from areas.models import Area, Sector
from audit.utils import audit
from core.exceptions import ApiError
from core.permissions import MANAGERS, RANGER, RESEARCHER, VIEWER, require_module, roles_allowed
from core.tenancy import TenantPKField
from core.utils import query_date, query_uuid
from core.validation import SanitizedCharField, StrictSerializer
from field.models import Species
from notify.services import notify_user

from . import heatmap as heatmap_service
from . import reports as report_service
from . import services
from .models import REPORT_FORMATS, REPORT_TYPES, Report, ReportShare

VIEWERS = {RESEARCHER, VIEWER}
REPORT_ROLES = MANAGERS | VIEWERS


def get_area(request, pk) -> Area:
    area = Area.objects.for_org(request.user.organisation).filter(pk=pk).first()
    if area is None:
        raise ApiError(404, "not_found", "Area not found.")
    return area


def get_ranger(request, pk) -> User:
    ranger = (User.objects.filter(organisation=request.user.organisation, role=RANGER, pk=pk)
              .select_related("team", "apu_base").first())
    if ranger is None:
        raise ApiError(404, "not_found", "Ranger not found.")
    return ranger


def optional_area(request) -> Area | None:
    area_id = query_uuid(request, "area_id")
    return get_area(request, area_id) if area_id else None


# --- summary + rangers ---------------------------------------------------------------------------------

class SummaryView(APIView):
    """GET dashboard/summary/?area_id="""

    permission_classes = [roles_allowed(read=MANAGERS)]

    def get(self, request):
        return Response(services.summary(request.user.organisation, optional_area(request)))


class RangerListView(APIView):
    """GET rangers/?area_id= — live ops list of active rangers."""

    permission_classes = [roles_allowed(read=MANAGERS)]

    def get(self, request):
        area = optional_area(request)
        org = request.user.organisation
        rangers = list(services.rangers_in_scope(org, area.pk if area else None)
                       .select_related("team", "apu_base").order_by("full_name"))
        return Response(services.ranger_payloads(org, rangers, area))


class RangerDetailView(APIView):
    """
    GET rangers/{id}/ — list object + ``recent_observations`` (10) + ``alerts`` (5) + ``profile``.

    ``profile`` carries the ranger's personal details (spec v1.5 §B2). It is deliberately absent from
    ``rangers/`` (the live-ops list), which the dashboard polls continuously.
    """

    permission_classes = [roles_allowed(read=MANAGERS)]

    def get(self, request, pk):
        from accounts.serializers import personal_profile

        org = request.user.organisation
        ranger = get_ranger(request, pk)
        body = services.ranger_payloads(org, [ranger])[0]
        body.update(is_active=ranger.is_active, profile=personal_profile(ranger),
                    **services.ranger_detail_extras(org, ranger))
        return Response(body)


class MessageSerializer(StrictSerializer):
    text = SanitizedCharField(max_length=320, allow_blank=False)


class RangerMessageView(APIView):
    """POST rangers/{id}/message/ {text ≤ 320} → 202 {channel, queued}."""

    permission_classes = [roles_allowed(read=MANAGERS, write=MANAGERS)]

    def post(self, request, pk):
        ranger = get_ranger(request, pk)
        s = MessageSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        text = s.validated_data["text"]
        channel = notify_user(ranger, f"PATROLIQ message from {request.user.full_name}", text,
                              {"type": "message", "from_user_id": str(request.user.pk)})
        audit(request, "ranger.message", target=ranger, detail={"channel": channel, "text": text})
        return Response({"channel": channel, "queued": True}, status=status.HTTP_202_ACCEPTED)


# --- risk + coverage -----------------------------------------------------------------------------------

class AreaRiskView(APIView):
    """GET areas/{id}/risk/?date=YYYY-MM-DD"""

    permission_classes = [roles_allowed(read=MANAGERS)]

    def get(self, request, pk):
        area = get_area(request, pk)
        require_module(request.user.organisation, "ai_risk")
        return Response(services.risk_map(area, query_date(request, "date")))


class AreaRiskTrendView(APIView):
    """GET areas/{id}/risk/trend/?days=30 (1–366)"""

    permission_classes = [roles_allowed(read=MANAGERS)]

    def get(self, request, pk):
        area = get_area(request, pk)
        require_module(request.user.organisation, "ai_risk")
        raw = request.query_params.get("days", "30")
        try:
            days = int(raw)
            if not 1 <= days <= 366:
                raise ValueError
        except ValueError:
            raise ApiError(400, "validation_error", "days must be an integer between 1 and 366.",
                           fields={"days": ["Expected 1-366."]})
        return Response(services.risk_trend(area, days))


class AreaHeatmapView(APIView):
    """
    GET areas/{id}/heatmap/?source=&days=&kernel=&bandwidth_m=&species_id= (spec v1.5 §C).

    Kernel density surface over the area, computed on demand and cached. Same roles and licence
    module as the risk map.
    """

    permission_classes = [roles_allowed(read=MANAGERS)]

    def get(self, request, pk):
        area = get_area(request, pk)
        require_module(request.user.organisation, "ai_risk")
        if not area.boundary:
            raise ApiError(400, "boundary_required", "The area has no boundary yet.")
        p = request.query_params
        source = p.get("source") or "incidents"
        if source not in heatmap_service.SOURCES:
            raise ApiError(400, "validation_error", "Unknown source.",
                           fields={"source": [f"Expected one of {', '.join(heatmap_service.SOURCES)}."]})
        kernel = p.get("kernel") or "quartic"
        if kernel not in geo.KERNELS:
            raise ApiError(400, "validation_error", "Unknown kernel.",
                           fields={"kernel": [f"Expected one of {', '.join(geo.KERNELS)}."]})
        days = _bounded_int(p.get("days"), heatmap_service.MIN_DAYS, heatmap_service.MAX_DAYS,
                            heatmap_service.DEFAULT_DAYS, "days")
        low, high = heatmap_service.MANUAL_BANDWIDTH_RANGE
        bandwidth = _bounded_float(p.get("bandwidth_m"), low, high, None, "bandwidth_m")
        species_id = query_uuid(request, "species_id")
        if species_id is not None and not Species.objects.filter(pk=species_id).exists():
            raise ApiError(404, "not_found", "Species not found.")
        return Response(heatmap_service.heatmap(area, source=source, days=days, kernel=kernel,
                                                bandwidth_m=bandwidth, species_id=species_id))


def _bounded_int(raw, low, high, default, name):
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or not low <= value <= high:
        raise ApiError(400, "validation_error", f"{name} must be an integer between {low} and {high}.",
                       fields={name: [f"Expected {low}-{high}."]})
    return value


def _bounded_float(raw, low, high, default, name):
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or not low <= value <= high:
        raise ApiError(400, "validation_error", f"{name} must be a number between {low:g} and {high:g}.",
                       fields={name: [f"Expected {low:g}-{high:g}."]})
    return value


def _visit_target(request) -> int | None:
    raw = request.query_params.get("visit_target")
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
        if not 1 <= value <= 100:
            raise ValueError
    except ValueError:
        raise ApiError(400, "validation_error", "visit_target must be an integer between 1 and 100.",
                       fields={"visit_target": ["Expected 1-100."]})
    return value


class AreaCoverageView(APIView):
    """GET areas/{id}/coverage/?month=YYYY-MM[&visit_target=]"""

    permission_classes = [roles_allowed(read=MANAGERS)]

    def get(self, request, pk):
        area = get_area(request, pk)
        require_module(request.user.organisation, "grts")
        return Response(services.coverage(area, request.query_params.get("month"), _visit_target(request)))


class AreaCoverageExportView(APIView):
    """GET areas/{id}/coverage/export/?month= → text/csv attachment."""

    permission_classes = [roles_allowed(read=MANAGERS)]

    def get(self, request, pk):
        area = get_area(request, pk)
        require_module(request.user.organisation, "grts")
        cov = services.coverage(area, request.query_params.get("month"), _visit_target(request))
        sectors = {s["id"]: s["name"] for s in cov["sectors"]}
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["cell_label", "cell_id", "sector_id", "sector_name", "visits", "visit_target", "status",
                    "last_visit_at", "observations", "month"])
        for c in cov["cells"]:
            w.writerow([c["label"], c["cell_id"], c["sector_id"] or "", sectors.get(c["sector_id"], ""), c["visits"],
                        cov["visit_target"], c["status"], c["last_visit_at"] or "", c["observations"], cov["month"]])
        resp = HttpResponse(buf.getvalue().encode("utf-8"), content_type="text/csv; charset=utf-8")
        slug = "".join(ch if ch.isalnum() else "-" for ch in area.name.lower()).strip("-")
        resp["Content-Disposition"] = f'attachment; filename="coverage-{slug}-{cov["month"]}.csv"'
        audit(request, "coverage.export", target=area, detail={"month": cov["month"]})
        return resp


# --- reports ---------------------------------------------------------------------------------------------

class ReportCreateSerializer(StrictSerializer):
    type = serializers.ChoiceField(choices=REPORT_TYPES)
    format = serializers.ChoiceField(choices=REPORT_FORMATS)
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    area_id = TenantPKField(model=Area, required=False, allow_null=True)
    sector_id = TenantPKField(model=Sector, required=False, allow_null=True)
    ranger_id = TenantPKField(model=User, extra_filter={"role": RANGER}, required=False, allow_null=True)
    species_id = serializers.PrimaryKeyRelatedField(queryset=Species.objects.all(), required=False, allow_null=True)

    def validate(self, attrs):
        if attrs["date_to"] < attrs["date_from"]:
            raise serializers.ValidationError({"date_to": ["Must not be before date_from."]})
        area, sector = attrs.get("area_id"), attrs.get("sector_id")
        if area is not None and sector is not None and sector.area_id != area.pk:
            raise serializers.ValidationError({"sector_id": ["Sector does not belong to this area."]})
        return attrs


def report_body(report: Report) -> dict:
    dt = serializers.DateTimeField()
    body = {
        "id": str(report.pk), "type": report.type, "format": report.format, "status": report.status,
        "title": report.title, "params": report.params, "size_bytes": report.size_bytes,
        "created_at": dt.to_representation(report.created_at),
        "created_by_name": report.created_by.full_name if report.created_by_id else None,
        "anonymised": report.anonymised, "summary": report.summary,
        "download_url": f"/api/v1/reports/{report.pk}/download/" if report.status == "ready" else None,
    }
    if report.status == "failed":
        body["error"] = report.error
    return body


def visible_reports(request):
    qs = Report.objects.for_org(request.user.organisation).select_related("created_by")
    if request.user.role in VIEWERS:
        qs = qs.filter(anonymised=True)
    return qs


def get_report(request, pk) -> Report:
    report = visible_reports(request).filter(pk=pk).first()
    if report is None:
        raise ApiError(404, "not_found", "Report not found.")
    return report


class ReportListCreateView(APIView):
    """GET reports/ (recent 50) · POST reports/ (synchronous generation)."""

    permission_classes = [roles_allowed(read=REPORT_ROLES, write=REPORT_ROLES)]

    def get(self, request):
        require_module(request.user.organisation, "reports")
        qs = visible_reports(request)
        if t := request.query_params.get("type"):
            qs = qs.filter(type=t)
        return Response([report_body(r) for r in qs.order_by("-created_at")[:50]])

    def post(self, request):
        org = request.user.organisation
        require_module(org, "reports")
        s = ReportCreateSerializer(data=request.data, context={"request": request})
        s.is_valid(raise_exception=True)
        d = s.validated_data
        anonymised = request.user.role in VIEWERS
        if anonymised and d["format"] == "pdf":
            raise ApiError(403, "format_not_allowed", "Researchers and viewers can generate CSV or GeoJSON reports only.")
        if anonymised and d.get("ranger_id") is not None:
            raise ApiError(400, "validation_error", "Anonymised reports cannot be filtered by ranger.",
                           fields={"ranger_id": ["Not available for anonymised reports."]})
        if (d["date_to"] - d["date_from"]).days + 1 > report_service.MAX_RANGE_DAYS:
            raise ApiError(400, "date_range_too_large", "The date range may cover at most 366 days.",
                           fields={"date_to": ["Range exceeds 366 days."]})
        params = {
            "type": d["type"], "format": d["format"], "date_from": d["date_from"].isoformat(),
            "date_to": d["date_to"].isoformat(),
            "area_id": str(d["area_id"].pk) if d.get("area_id") else None,
            "sector_id": str(d["sector_id"].pk) if d.get("sector_id") else None,
            "ranger_id": str(d["ranger_id"].pk) if d.get("ranger_id") else None,
            "species_id": str(d["species_id"].pk) if d.get("species_id") else None,
        }
        report = report_service.generate(org, request.user, params, anonymised, request=request)
        return Response(report_body(report), status=status.HTTP_201_CREATED)


class ReportDetailView(APIView):
    permission_classes = [roles_allowed(read=REPORT_ROLES)]

    def get(self, request, pk):
        require_module(request.user.organisation, "reports")
        return Response(report_body(get_report(request, pk)))


class ReportDownloadView(APIView):
    """GET reports/{id}/download/ — streams the file through the API (never a public storage URL)."""

    permission_classes = [roles_allowed(read=REPORT_ROLES)]

    def get(self, request, pk):
        require_module(request.user.organisation, "reports")
        report = get_report(request, pk)
        if report.status != "ready":
            raise ApiError(409, "report_not_ready", "The report failed to generate; create it again.")
        handle, regenerated = report_service.open_file(report, request)
        p = report.params
        filename = f"patroliq-{report.type}-{p.get('date_from')}-{p.get('date_to')}{'.' + report.format}"
        resp = FileResponse(handle, as_attachment=True, filename=filename, content_type=report_service.content_type(report))
        resp["Cache-Control"] = "private, no-store"
        audit(request, "report.download", target=report, detail={"format": report.format, "regenerated": regenerated})
        return resp


class ReportShareView(APIView):
    """POST reports/{id}/share/ → {url, token, expires_at} (48 h; the link still requires sign-in)."""

    permission_classes = [roles_allowed(read=REPORT_ROLES, write=REPORT_ROLES)]

    def post(self, request, pk):
        from core.validation import reject_unexpected

        require_module(request.user.organisation, "reports")
        reject_unexpected(request.data, set())
        report = get_report(request, pk)
        share = ReportShare.objects.create(organisation=report.organisation, report=report, created_by=request.user,
                                           expires_at=timezone.now() + timedelta(hours=settings.REPORT_SHARE_HOURS))
        if settings.DASHBOARD_URL:
            url = f"{settings.DASHBOARD_URL}/reports/shared/{share.token}"
        else:
            url = request.build_absolute_uri(f"/api/v1/reports/shared/{share.token}/")
        audit(request, "report.share", target=report, detail={"share_id": str(share.pk),
                                                             "expires_at": share.expires_at.isoformat()})
        return Response({"url": url, "token": share.token,
                         "expires_at": serializers.DateTimeField().to_representation(share.expires_at)},
                        status=status.HTTP_201_CREATED)


class SharedReportView(APIView):
    """GET reports/shared/{token}/ — the shared report, for a signed-in member of the same organisation."""

    permission_classes = [roles_allowed(read=REPORT_ROLES)]

    def get(self, request, token):
        require_module(request.user.organisation, "reports")
        share = ReportShare.objects.select_related("report", "report__created_by", "created_by").filter(token=token).first()
        if share is None or share.organisation_id != request.user.organisation_id:
            raise ApiError(404, "not_found", "Shared report not found.")
        if share.expires_at <= timezone.now():
            raise ApiError(410, "share_expired", "This share link has expired.")
        report = share.report
        if request.user.role in VIEWERS and not report.anonymised:
            raise ApiError(403, "anonymised_only", "Researchers and viewers can only open anonymised reports.")
        audit(request, "report.share_open", target=report, detail={"share_id": str(share.pk)})
        dt = serializers.DateTimeField()
        return Response({**report_body(report), "shared_by_name": share.created_by.full_name if share.created_by_id else None,
                         "share_expires_at": dt.to_representation(share.expires_at)})
