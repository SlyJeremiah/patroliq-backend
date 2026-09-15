from __future__ import annotations

import hashlib
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery
from django.http import FileResponse
from django.utils import timezone
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.auth import SafetyTokenAuthentication
from accounts.licensing import module_enabled
from accounts.models import User
from accounts.serializers import LicenceSerializer
from areas.models import ApuBase, Area, Assignment, GrtsCell, RiskScore, Sector
from areas.serializers import (
    AreaSerializer,
    ApuBaseSerializer,
    AssignmentSerializer,
    GrtsCellSerializer,
    RiskScoreSerializer,
    SectorSerializer,
)
from areas.views import ranger_area_filter
from audit.utils import audit
from core.exceptions import ApiError
from core.models import Tombstone
from core.permissions import FIELD_ROLES, MANAGERS, ORG_ROLES, RANGER, IsOrgMember, roles_allowed
from core.tenancy import TenantScopedMixin
from core.utils import query_date, query_datetime, query_uuid
from core.validation import reject_unexpected

from . import services
from .models import Media, Observation, Patrol, PositionPing, SafetyAlert, Species
from .serializers import (
    MediaSerializer,
    ObservationListSerializer,
    PatrolSerializer,
    PositionPingInSerializer,
    SafetyAlertSerializer,
    SafetyCancelSerializer,
    SpeciesSerializer,
)


def _since(qs, since, field="updated_at"):
    return qs.filter(**{f"{field}__gte": since}) if since else qs


class BootstrapView(APIView):
    """GET sync/bootstrap/?since=<iso> — offline package for the signed-in field user."""

    permission_classes = [IsAuthenticated, roles_allowed(read=FIELD_ROLES)]

    def get(self, request):
        user = request.user
        org = user.organisation
        since = query_datetime(request, "since")
        server_time = timezone.now().replace(microsecond=0)
        today = server_time.date()

        areas = Area.objects.for_org(org).filter(status="active")
        if user.role == RANGER:
            areas = areas.filter(ranger_area_filter(user)).distinct()
        area_ids = list(areas.values_list("pk", flat=True))

        assignments = Assignment.objects.for_org(org).filter(area_id__in=area_ids, date__gte=today - timedelta(days=1))
        if user.role == RANGER:
            assignments = assignments.filter(team__members=user)

        risk = RiskScore.objects.none()
        if module_enabled(org, "ai_risk"):
            risk = RiskScore.objects.for_org(org).filter(area_id__in=area_ids, date__gte=today - timedelta(days=1),
                                                         date__lte=today + timedelta(days=1))

        members = User.objects.none()
        if user.team_id:
            members = User.objects.filter(team_id=user.team_id, is_active=True)
        if user.role in MANAGERS:
            members = User.objects.filter(Q(organisation=org, is_active=True, role=RANGER) | Q(pk__in=members.values("pk")))

        body = {
            "server_time": server_time,
            "areas": AreaSerializer(_since(Area.objects.filter(pk__in=area_ids), since), many=True).data,
            "apu_bases": ApuBaseSerializer(_since(ApuBase.objects.filter(area_id__in=area_ids), since), many=True).data,
            "sectors": SectorSerializer(_since(Sector.objects.filter(area_id__in=area_ids), since), many=True).data,
            "cells": GrtsCellSerializer(_since(GrtsCell.objects.filter(area_id__in=area_ids), since), many=True).data,
            "assignments": AssignmentSerializer(_since(assignments.prefetch_related("cells"), since), many=True).data,
            "risk_scores": RiskScoreSerializer(_since(risk, since), many=True).data,
            "species": SpeciesSerializer(_since(Species.objects.all(), since), many=True).data,
            "team_members": [
                {"id": str(u.pk), "full_name": u.full_name, "employee_id": u.employee_id, "role": u.role}
                for u in members.order_by("full_name").distinct()
            ],
            "licence": LicenceSerializer(org.licence).data if hasattr(org, "licence") else None,
            "deleted": self._deleted(org, area_ids, since),
        }
        from rest_framework.settings import api_settings

        body["server_time"] = server_time.strftime(api_settings.DATETIME_FORMAT)
        user.last_sync_at = timezone.now()
        user.save(update_fields=["last_sync_at"])
        return Response(body)

    @staticmethod
    def _deleted(org, area_ids, since):
        """Additive key: IDs removed since ``since`` (full bootstrap -> empty lists)."""
        out = {k: [] for k in ("areas", "apu_bases", "sectors", "cells", "assignments")}
        if not since:
            return out
        for kind, oid, aid in Tombstone.objects.for_org(org).filter(deleted_at__gte=since).values_list(
                "kind", "object_id", "area_id"):
            if kind in out and (kind == "areas" or aid in area_ids):
                out[kind].append(str(oid))
        # Areas that are no longer available to this user (archived / back to draft) are also "deleted".
        gone = Area.objects.for_org(org).filter(updated_at__gte=since).exclude(pk__in=area_ids)
        out["areas"].extend(str(pk) for pk in gone.values_list("pk", flat=True))
        return out


class PushView(APIView):
    """POST sync/push/. Licence check is per item (safety alerts always accepted)."""

    permission_classes = [IsAuthenticated, roles_allowed(read=FIELD_ROLES, allow_suspended=True)]

    def get_authenticators(self):
        # Idle token expiry is enforced for the batch unless it carries a safety alert, which must
        # always land. ``self.request`` is the Django request here (set in View.setup).
        from accounts.auth import ExpiringTokenAuthentication

        has_alerts = False
        try:
            import json

            raw = json.loads(self.request.body or b"{}")
            has_alerts = bool(isinstance(raw, dict) and raw.get("safety_alerts"))
        except Exception:  # unparsable bodies are rejected later by the JSON parser
            has_alerts = False
        return [SafetyTokenAuthentication() if has_alerts else ExpiringTokenAuthentication()]

    def post(self, request):
        if not isinstance(request.data, dict):
            raise ApiError(400, "validation_error", "Expected a JSON object.")
        reject_unexpected(request.data, {"patrols", "track_points", "observations", "safety_alerts"})
        for key in ("patrols", "track_points", "observations", "safety_alerts"):
            if request.data.get(key) is not None and not isinstance(request.data.get(key), list):
                raise ApiError(400, "validation_error", f"{key} must be a list.", fields={key: ["Expected a list."]})
        return Response(services.process_push(request, request.data))


# --- media -------------------------------------------------------------------------------------

_KIND_PREFIX = {"photo": "image/", "video": "video/", "audio": "audio/"}


def sniff_content_type(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"#!AMR"):
        return "audio/amr"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return "audio/mpeg"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand.startswith(b"M4A"):
            return "audio/mp4"
        if brand.startswith(b"3gp"):
            return "video/3gpp"
        return "video/mp4"
    return None


class MediaUploadView(APIView):
    """POST media/ (multipart: file, observation_client_uuid, kind) -> Media."""

    permission_classes = [IsAuthenticated, roles_allowed(read=FIELD_ROLES)]

    def post(self, request):
        reject_unexpected(request.data, {"file", "observation_client_uuid", "kind"})
        upload = request.FILES.get("file")
        fields = {}
        if upload is None:
            fields["file"] = ["This field is required."]
        kind = request.data.get("kind")
        if kind not in _KIND_PREFIX:
            fields["kind"] = ["Must be one of photo, video, audio."]
        obs_id = request.data.get("observation_client_uuid")
        try:
            import uuid

            obs_id = uuid.UUID(str(obs_id))
        except (ValueError, TypeError):
            fields["observation_client_uuid"] = ["Must be a UUID."]
        if fields:
            raise ApiError(400, "validation_error", "Invalid media upload.", fields=fields)
        if upload.size > settings.MEDIA_MAX_BYTES:
            raise ApiError(413, "file_too_large", f"File exceeds {settings.MEDIA_MAX_BYTES} bytes.")

        obs = Observation.objects.for_org(request.user.organisation).filter(pk=obs_id)
        if request.user.role == RANGER:
            obs = obs.filter(observer=request.user)
        obs = obs.first()
        if obs is None:
            raise ApiError(404, "observation_not_found", "Push the observation before uploading its media.")

        head = upload.read(64)
        upload.seek(0)
        declared = (upload.content_type or "").split(";")[0].strip().lower()
        content_type = declared if declared and declared != "application/octet-stream" else (sniff_content_type(head) or "")
        if not content_type.startswith(_KIND_PREFIX[kind]):
            raise ApiError(400, "unsupported_media_type", f"Content type '{content_type or 'unknown'}' does not match kind '{kind}'.")

        digest = hashlib.sha256()
        for chunk in upload.chunks():
            digest.update(chunk)
        sha = digest.hexdigest()
        upload.seek(0)

        existing = Media.objects.filter(observation=obs, sha256=sha).first()
        if existing:
            return Response(MediaSerializer(existing, context={"request": request}).data, status=status.HTTP_200_OK)
        import uuid

        media = Media(id=uuid.uuid4(), organisation=request.user.organisation, observation=obs, kind=kind,
                      content_type=content_type, size_bytes=upload.size, sha256=sha, uploaded_by=request.user)
        with transaction.atomic():
            media.file.save(upload.name or "upload", upload, save=False)
            media.save()
        audit(request, "media.upload", target=media, detail={"observation": str(obs.pk), "kind": kind,
                                                              "size_bytes": upload.size})
        return Response(MediaSerializer(media, context={"request": request}).data, status=status.HTTP_201_CREATED)


class MediaViewSet(TenantScopedMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = Media.objects.all()
    serializer_class = MediaSerializer
    permission_classes = [roles_allowed(read=FIELD_ROLES)]

    def scope_queryset(self, qs):
        if self.request.user.role == RANGER:
            qs = qs.filter(observation__observer=self.request.user)
        return qs

    def file(self, request, pk=None):
        media = self.get_object()
        try:
            handle = media.file.open("rb")
        except FileNotFoundError:
            raise ApiError(404, "not_found", "Media file missing.")
        resp = FileResponse(handle, content_type=media.content_type)
        resp["Cache-Control"] = "private, max-age=3600"
        return resp


# --- positions -----------------------------------------------------------------------------------

class PositionsView(APIView):
    """POST positions/ {pings: [...]} -> 202. Accepted for suspended licences too (safety relevant)."""

    authentication_classes = [SafetyTokenAuthentication]
    permission_classes = [IsAuthenticated, roles_allowed(read=FIELD_ROLES, allow_suspended=True)]

    def post(self, request):
        if not isinstance(request.data, dict):
            raise ApiError(400, "validation_error", "Expected a JSON object.")
        reject_unexpected(request.data, {"pings"})
        pings = request.data.get("pings")
        if not isinstance(pings, list):
            raise ApiError(400, "validation_error", "pings must be a list.", fields={"pings": ["Expected a list."]})
        rows, rejected = [], []
        for i, item in enumerate(pings):
            s = PositionPingInSerializer(data=item)
            if not s.is_valid():
                from core.exceptions import first_message, validation_code

                rejected.append({"index": i, "code": validation_code(s.errors), "message": first_message(s.errors)})
                continue
            d = s.validated_data
            if d.get("ranger_id") and d["ranger_id"] != request.user.pk:
                rejected.append({"index": i, "code": "forbidden", "message": "ranger_id must be the signed-in user."})
                continue
            rows.append(PositionPing(organisation_id=request.user.organisation_id, ranger=request.user,
                                     patrol_client_uuid=d.get("patrol_client_uuid"), recorded_at=d["recorded_at"],
                                     lat=d["lat"], lon=d["lon"], accuracy_m=d.get("accuracy_m"),
                                     battery_pct=d.get("battery_pct")))
        PositionPing.objects.bulk_create(rows)
        return Response({"accepted": len(rows), "rejected": rejected}, status=status.HTTP_202_ACCEPTED)


class LatestPositionsView(APIView):
    """GET positions/latest/?area_id= — last known position per user (managers)."""

    permission_classes = [roles_allowed(read=MANAGERS)]

    def get(self, request):
        org = request.user.organisation
        latest = PositionPing.objects.filter(ranger=OuterRef("ranger")).order_by("-recorded_at").values("pk")[:1]
        qs = PositionPing.objects.for_org(org).filter(pk=Subquery(latest)).select_related("ranger")
        if area_id := query_uuid(request, "area_id"):
            qs = qs.filter(Q(ranger__areas=area_id) | Q(ranger__team__area_id=area_id)).distinct()
        if since := query_datetime(request, "since"):
            qs = qs.filter(recorded_at__gte=since)
        out = []
        for p in qs.order_by("ranger__full_name"):
            out.append({
                "ranger_id": str(p.ranger_id), "full_name": p.ranger.full_name, "employee_id": p.ranger.employee_id,
                "team_id": str(p.ranger.team_id) if p.ranger.team_id else None,
                "patrol_client_uuid": str(p.patrol_client_uuid) if p.patrol_client_uuid else None,
                "recorded_at": serializers.DateTimeField().to_representation(p.recorded_at),
                "lat": p.lat, "lon": p.lon, "accuracy_m": p.accuracy_m, "battery_pct": p.battery_pct,
            })
        return Response(out)


# --- safety --------------------------------------------------------------------------------------

class SafetyAlertCreateView(APIView):
    """POST safety/alerts/ — never blocked by licence state or idle token expiry."""

    authentication_classes = [SafetyTokenAuthentication]
    permission_classes = [IsAuthenticated, IsOrgMember]

    def post(self, request):
        if not isinstance(request.data, dict):
            raise ApiError(400, "validation_error", "Expected a JSON object.")
        alert, created = services.record_safety_alert(request, request.data)
        return Response(SafetyAlertSerializer(alert).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class SafetyAlertCancelView(APIView):
    """POST safety/alerts/{client_uuid}/cancel/ {pin_verified: true, note?}."""

    authentication_classes = [SafetyTokenAuthentication]
    permission_classes = [IsAuthenticated, IsOrgMember]

    def post(self, request, client_uuid):
        s = SafetyCancelSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        qs = SafetyAlert.objects.for_org(request.user.organisation).select_related("ranger")
        if request.user.role not in MANAGERS:
            qs = qs.filter(ranger=request.user)
        alert = qs.filter(pk=client_uuid).first()
        if alert is None:
            raise ApiError(404, "not_found", "Safety alert not found.")
        if not s.validated_data["pin_verified"]:
            raise ApiError(400, "pin_not_verified", "The device PIN must be verified to cancel an SOS.")
        alert = services.cancel_safety_alert(request, alert, s.validated_data.get("note"))
        return Response(SafetyAlertSerializer(alert).data)


# --- manager views -------------------------------------------------------------------------------

def _alert_item(obj) -> dict:
    dt = serializers.DateTimeField()
    fmt = lambda v: dt.to_representation(v) if v else None  # noqa: E731
    if isinstance(obj, SafetyAlert):
        return {
            "id": str(obj.pk), "type": "safety", "kind": obj.kind, "status": obj.status, "severity": "critical",
            "ranger_id": str(obj.ranger_id), "ranger_name": obj.ranger.full_name, "area_id": None, "cell_id": None,
            "lat": obj.lat, "lon": obj.lon, "accuracy_m": obj.accuracy_m, "battery_pct": obj.battery_pct,
            "occurred_at": fmt(obj.started_at), "acknowledged_at": fmt(obj.acknowledged_at),
            "resolved_at": fmt(obj.resolved_at), "note": obj.resolution_note,
        }
    return {
        "id": str(obj.pk), "type": "threat", "kind": obj.subtype or obj.category,
        "status": "acknowledged" if obj.acknowledged_at else "active", "severity": obj.severity,
        "ranger_id": str(obj.observer_id), "ranger_name": obj.observer.full_name, "area_id": str(obj.area_id),
        "cell_id": str(obj.cell_id) if obj.cell_id else None, "lat": obj.lat, "lon": obj.lon,
        "accuracy_m": obj.accuracy_m, "battery_pct": None, "occurred_at": fmt(obj.recorded_at),
        "acknowledged_at": fmt(obj.acknowledged_at), "resolved_at": None, "note": obj.notes,
    }


def threat_alert_q() -> Q:
    return Q(alert_manager=True) | Q(category__in=["threat", "carcass"], severity__in=["high", "critical"])


class AlertListView(APIView):
    """GET alerts/?status=active|acknowledged&area_id=&limit= — safety + threat alerts, newest first."""

    permission_classes = [roles_allowed(read=MANAGERS)]

    def get(self, request):
        org = request.user.organisation
        wanted = request.query_params.get("status")
        try:
            limit = min(int(request.query_params.get("limit", 200)), 1000)
        except ValueError:
            raise ApiError(400, "validation_error", "limit must be an integer.")
        safety = SafetyAlert.objects.for_org(org).select_related("ranger")
        threats = Observation.objects.for_org(org).filter(threat_alert_q()).select_related("observer")
        if area_id := query_uuid(request, "area_id"):
            threats = threats.filter(area_id=area_id)
        if wanted == "active":
            safety, threats = safety.filter(status="active"), threats.filter(acknowledged_at__isnull=True)
        elif wanted == "acknowledged":
            safety, threats = safety.filter(status="acknowledged"), threats.filter(acknowledged_at__isnull=False)
        items = [_alert_item(a) for a in safety[:limit]] + [_alert_item(o) for o in threats[:limit]]
        items.sort(key=lambda x: x["occurred_at"] or "", reverse=True)
        return Response(items[:limit])


class AlertAcknowledgeView(APIView):
    permission_classes = [roles_allowed(read=MANAGERS, write=MANAGERS)]

    def post(self, request, alert_id):
        reject_unexpected(request.data, {"note"})
        org = request.user.organisation
        now = timezone.now()
        alert = SafetyAlert.objects.for_org(org).select_related("ranger").filter(pk=alert_id).first()
        if alert:
            if alert.status == "active":
                alert.status, alert.acknowledged_at, alert.acknowledged_by = "acknowledged", now, request.user
                alert.save(update_fields=["status", "acknowledged_at", "acknowledged_by", "updated_at"])
            audit(request, "safety_alert.acknowledge", target=alert)
            return Response(_alert_item(alert))
        obs = Observation.objects.for_org(org).filter(threat_alert_q()).select_related("observer").filter(pk=alert_id).first()
        if obs is None:
            raise ApiError(404, "not_found", "Alert not found.")
        if obs.acknowledged_at is None:
            obs.acknowledged_at, obs.acknowledged_by = now, request.user
            obs.save(update_fields=["acknowledged_at", "acknowledged_by", "updated_at"])
        audit(request, "observation.acknowledge", target=obs)
        return Response(_alert_item(obs))


class AlertResolveView(APIView):
    """POST alerts/{id}/resolve/ {note?} — close a safety alert (additive to the spec)."""

    permission_classes = [roles_allowed(read=MANAGERS, write=MANAGERS)]

    def post(self, request, alert_id):
        reject_unexpected(request.data, {"note"})
        alert = SafetyAlert.objects.for_org(request.user.organisation).select_related("ranger").filter(pk=alert_id).first()
        if alert is None:
            raise ApiError(404, "not_found", "Safety alert not found.")
        if alert.status in ("active", "acknowledged"):
            from core.validation import sanitize_text

            alert.status, alert.resolved_at = "resolved", timezone.now()
            alert.resolution_note = sanitize_text(str(request.data.get("note") or ""))[:1000] or None
            alert.save(update_fields=["status", "resolved_at", "resolution_note", "updated_at"])
            audit(request, "safety_alert.resolve", target=alert)
        return Response(_alert_item(alert))


class _FieldListMixin(TenantScopedMixin):
    date_field = "recorded_at"
    person_field = "observer"

    def scope_queryset(self, qs):
        user, p = self.request.user, self.request
        if user.role == RANGER:
            qs = qs.filter(**{self.person_field: user})
        if area_id := query_uuid(p, "area_id"):
            qs = qs.filter(area_id=area_id)
        if d := query_date(p, "date"):
            qs = qs.filter(**{f"{self.date_field}__date": d})
        if d := query_date(p, "date_from"):
            qs = qs.filter(**{f"{self.date_field}__date__gte": d})
        if d := query_date(p, "date_to"):
            qs = qs.filter(**{f"{self.date_field}__date__lte": d})
        if uid := query_uuid(p, "ranger_id"):
            qs = qs.filter(**{f"{self.person_field}_id": uid})
        return qs


class ObservationViewSet(_FieldListMixin, viewsets.ReadOnlyModelViewSet):
    """GET observations/ — managers: organisation-wide; rangers: their own."""

    queryset = Observation.objects.prefetch_related("media")
    serializer_class = ObservationListSerializer
    permission_classes = [roles_allowed(read=FIELD_ROLES)]

    def scope_queryset(self, qs):
        qs = super().scope_queryset(qs)
        if cat := self.request.query_params.get("category"):
            qs = qs.filter(category=cat)
        return qs


class PatrolViewSet(_FieldListMixin, viewsets.ReadOnlyModelViewSet):
    queryset = Patrol.objects.all()
    serializer_class = PatrolSerializer
    permission_classes = [roles_allowed(read=FIELD_ROLES)]
    date_field = "started_at"
    person_field = "ranger"


class SpeciesListView(APIView):
    permission_classes = [roles_allowed(read=ORG_ROLES)]

    def get(self, request):
        return Response(SpeciesSerializer(Species.objects.all(), many=True).data)

