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

import geo
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
from core.utils import query_bool, query_date, query_datetime, query_uuid
from core.validation import reject_unexpected, sanitize_text

from . import services
from .alerts import alert_detail, alert_item, find_alert, latest_dispatches, record_event, threat_alert_q  # noqa: F401
from .models import Media, Observation, Patrol, PositionPing, SafetyAlert, Species, TrackPoint
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
        body = dict(SafetyAlertSerializer(alert).data)
        warnings = getattr(alert, "details_warnings", None)
        if warnings:
            body["details_warnings"] = warnings
        return Response(body, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


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

_alert_item = alert_item  # backwards-compatible name


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
            # Safety alerts that carry an area (HWC) are filtered too; those without one always show,
            # because an SOS with no area is still everyone's problem (spec v1.5 §A5).
            safety = safety.filter(Q(area_id=area_id) | Q(area_id__isnull=True))
        if wanted == "active":
            safety, threats = safety.filter(status="active"), threats.filter(acknowledged_at__isnull=True)
        elif wanted == "acknowledged":
            safety, threats = safety.filter(status="acknowledged"), threats.filter(acknowledged_at__isnull=False)
        rows = list(safety[:limit]) + list(threats[:limit])
        dispatched = latest_dispatches(org, [r.pk for r in rows])
        items = [alert_item(r, dispatched.get(r.pk)) for r in rows]
        items.sort(key=lambda x: x["occurred_at"] or "", reverse=True)
        return Response(items[:limit])


class AlertDetailView(APIView):
    """GET alerts/{id}/ — full alert incl. ``timeline``, ``dispatched_at`` and ``responders`` (spec §7)."""

    permission_classes = [roles_allowed(read=MANAGERS)]

    def get(self, request, alert_id):
        alert = find_alert(request.user.organisation, alert_id)
        if alert is None:
            raise ApiError(404, "not_found", "Alert not found.")
        return Response(alert_detail(alert))


class AlertDispatchSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=5000)
    responder_ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=False, max_length=50)


class AlertDispatchView(APIView):
    """
    POST alerts/{id}/dispatch/ {note, responder_ids} — send responders to an alert (spec §7).
    Acknowledges an unacknowledged alert; notifies every responder by SMS (or push without a phone).
    """

    permission_classes = [roles_allowed(read=MANAGERS, write=MANAGERS)]

    def post(self, request, alert_id):
        from core.validation import sanitize_text
        from notify.services import notify_user

        reject_unexpected(request.data, {"note", "responder_ids"})
        s = AlertDispatchSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        org = request.user.organisation
        alert = find_alert(org, alert_id)
        if alert is None:
            raise ApiError(404, "not_found", "Alert not found.")
        if isinstance(alert, SafetyAlert) and alert.status in ("resolved", "cancelled"):
            raise ApiError(409, "alert_closed", f"The alert is already {alert.status}.")
        note = sanitize_text(s.validated_data.get("note") or "")[:1000]
        ids = list(dict.fromkeys(s.validated_data["responder_ids"]))
        responders = list(User.objects.filter(organisation=org, is_active=True, pk__in=ids))
        missing = sorted(str(i) for i in set(ids) - {u.pk for u in responders})
        if missing:
            raise ApiError(400, "validation_error", "Unknown or inactive responder(s).",
                           fields={"responder_ids": [f"{m} is not an active user of this organisation." for m in missing]})
        now = timezone.now()
        if isinstance(alert, SafetyAlert) and alert.status == "active":
            alert.status, alert.acknowledged_at, alert.acknowledged_by = "acknowledged", now, request.user
            alert.save(update_fields=["status", "acknowledged_at", "acknowledged_by", "updated_at"])
            record_event(alert, "acknowledged", actor=request.user, at=now)
        elif isinstance(alert, Observation) and alert.acknowledged_at is None:
            alert.acknowledged_at, alert.acknowledged_by = now, request.user
            alert.save(update_fields=["acknowledged_at", "acknowledged_by", "updated_at"])
            record_event(alert, "acknowledged", actor=request.user, at=now)
        record_event(alert, "dispatched", actor=request.user, note=note, responder_ids=[u.pk for u in responders], at=now)

        item = alert_item(alert)
        where = f"{item['lat']:.5f},{item['lon']:.5f}" if item["lat"] is not None and item["lon"] is not None else "no GPS fix"
        cell = f" [{alert.cell.label}]" if isinstance(alert, Observation) and alert.cell_id else ""
        body = (f"Respond to {item['kind'].replace('_', ' ')} ({item['severity'] or 'n/a'}) reported by "
                f"{item['ranger_name']} at {where}{cell}." + (f" Note: {note}" if note else ""))
        channels = {}
        for u in responders:
            channels[str(u.pk)] = notify_user(u, "PATROLIQ DISPATCH", body,
                                              {"type": "dispatch", "alert_id": str(alert.pk), "alert_type": item["type"]})
        audit(request, "alert.dispatch", target=alert, detail={
            "responder_ids": [str(u.pk) for u in responders], "channels": channels, "note": note})
        return Response(alert_detail(alert))


class AlertAcknowledgeView(APIView):
    permission_classes = [roles_allowed(read=MANAGERS, write=MANAGERS)]

    def post(self, request, alert_id):
        reject_unexpected(request.data, {"note"})
        org = request.user.organisation
        now = timezone.now()
        alert = SafetyAlert.objects.for_org(org).select_related("ranger").filter(pk=alert_id).first()
        note = sanitize_text(str(request.data.get("note") or ""))[:1000]
        if alert:
            if alert.status == "active":
                alert.status, alert.acknowledged_at, alert.acknowledged_by = "acknowledged", now, request.user
                alert.save(update_fields=["status", "acknowledged_at", "acknowledged_by", "updated_at"])
                record_event(alert, "acknowledged", actor=request.user, note=note, at=now)
            audit(request, "safety_alert.acknowledge", target=alert)
            return Response(alert_item(alert, latest_dispatches(org, [alert.pk]).get(alert.pk)))
        obs = Observation.objects.for_org(org).filter(threat_alert_q()).select_related("observer").filter(pk=alert_id).first()
        if obs is None:
            raise ApiError(404, "not_found", "Alert not found.")
        if obs.acknowledged_at is None:
            obs.acknowledged_at, obs.acknowledged_by = now, request.user
            obs.save(update_fields=["acknowledged_at", "acknowledged_by", "updated_at"])
            record_event(obs, "acknowledged", actor=request.user, note=note, at=now)
        audit(request, "observation.acknowledge", target=obs)
        return Response(alert_item(obs, latest_dispatches(org, [obs.pk]).get(obs.pk)))


class AlertResolveView(APIView):
    """POST alerts/{id}/resolve/ {note?} — close a safety alert (additive to the spec)."""

    permission_classes = [roles_allowed(read=MANAGERS, write=MANAGERS)]

    def post(self, request, alert_id):
        reject_unexpected(request.data, {"note"})
        alert = SafetyAlert.objects.for_org(request.user.organisation).select_related("ranger").filter(pk=alert_id).first()
        if alert is None:
            raise ApiError(404, "not_found", "Safety alert not found.")
        if alert.status in ("active", "acknowledged"):
            alert.status, alert.resolved_at = "resolved", timezone.now()
            alert.resolution_note = sanitize_text(str(request.data.get("note") or ""))[:1000] or None
            alert.save(update_fields=["status", "resolved_at", "resolution_note", "updated_at"])
            record_event(alert, "resolved", actor=request.user, note=alert.resolution_note or "", at=alert.resolved_at)
            audit(request, "safety_alert.resolve", target=alert)
        org = request.user.organisation
        return Response(alert_item(alert, latest_dispatches(org, [alert.pk]).get(alert.pk)))


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


class PatrolTrackView(APIView):
    """
    GET patrols/{client_uuid}/track/ → GeoJSON Feature (LineString) for the dashboard map (spec §7).
    ``geometry`` is null while the patrol has fewer than two track points. Additive properties:
    ``client_uuid``, ``ranger_name``, ``patrol_type``, ``duration_s``, ``point_count`` and ``times``
    (ISO timestamps parallel to the coordinates, for replay).

    ``?clean=true|false`` (default **true**) selects the sanitised track (``geo.track``): accuracy,
    jump and drift outliers from the old GPS+network recording are left out of ``geometry`` /
    ``times``, so the line does not zig-zag. ``clean=false`` returns every stored point. Either way
    the properties carry ``distance_m`` (as stored on the patrol), ``distance_clean_m`` (measured
    over the sanitised track, always, so the two can be compared) and ``points_dropped`` (how many
    stored points the sanitiser rejected).
    """

    permission_classes = [roles_allowed(read=MANAGERS)]

    def get(self, request, client_uuid):
        patrol = Patrol.objects.for_org(request.user.organisation).select_related("ranger").filter(pk=client_uuid).first()
        if patrol is None:
            raise ApiError(404, "not_found", "Patrol not found.")
        clean = query_bool(request, "clean", default=True)
        rows = list(services.track_fixes(patrol))
        track = geo.clean_track(rows, patrol_type=patrol.patrol_type)
        pts = track.points if clean else [geo.Fix(*r) for r in rows]
        dt = serializers.DateTimeField()
        coords = [[round(p.lon, 7), round(p.lat, 7)] for p in pts]
        return Response({
            "type": "Feature",
            "id": str(patrol.pk),
            "geometry": {"type": "LineString", "coordinates": coords} if len(coords) >= 2 else None,
            "properties": {
                "client_uuid": str(patrol.pk), "ranger_id": str(patrol.ranger_id),
                "ranger_name": patrol.ranger.full_name, "started_at": dt.to_representation(patrol.started_at),
                "ended_at": dt.to_representation(patrol.ended_at) if patrol.ended_at else None,
                "distance_m": int(round(patrol.distance_m or 0)),
                "distance_clean_m": int(round(track.distance_m)), "points_dropped": track.points_dropped,
                "clean": clean, "duration_s": patrol.duration_s,
                "status": patrol.status, "patrol_type": patrol.patrol_type, "area_id": str(patrol.area_id),
                "point_count": len(coords), "times": [dt.to_representation(p.recorded_at) for p in pts],
            },
        })


class PositionHistoryView(APIView):
    """GET positions/history/?ranger_id=&since=&until= (window ≤ 24 h) → [{recorded_at, lat, lon, battery_pct}]."""

    permission_classes = [roles_allowed(read=MANAGERS)]
    MAX_WINDOW = timedelta(hours=24)

    def get(self, request):
        org = request.user.organisation
        ranger_id = query_uuid(request, "ranger_id")
        if ranger_id is None:
            raise ApiError(400, "validation_error", "ranger_id is required.", fields={"ranger_id": ["This field is required."]})
        if not User.objects.filter(organisation=org, pk=ranger_id).exists():
            raise ApiError(404, "not_found", "Ranger not found.")
        since, until = query_datetime(request, "since"), query_datetime(request, "until")
        if since is None and until is None:
            until = timezone.now()
        if since is None:
            since = until - self.MAX_WINDOW
        if until is None:
            until = min(since + self.MAX_WINDOW, timezone.now()) if since < timezone.now() else since + self.MAX_WINDOW
        if until < since:
            raise ApiError(400, "validation_error", "until must not be before since.", fields={"until": ["Must be after since."]})
        if until - since > self.MAX_WINDOW:
            raise ApiError(400, "window_too_large", "The time window may be at most 24 hours.",
                           fields={"since": ["Window exceeds 24 hours."]})
        dt = serializers.DateTimeField()
        rows = (PositionPing.objects.for_org(org).filter(ranger_id=ranger_id, recorded_at__gte=since, recorded_at__lte=until)
                .order_by("recorded_at").values_list("recorded_at", "lat", "lon", "battery_pct"))
        return Response([{"recorded_at": dt.to_representation(t), "lat": lat, "lon": lon, "battery_pct": b}
                         for t, lat, lon, b in rows])


class SpeciesListView(APIView):
    permission_classes = [roles_allowed(read=ORG_ROLES)]

    def get(self, request):
        return Response(SpeciesSerializer(Species.objects.all(), many=True).data)

