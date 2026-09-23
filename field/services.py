"""
Sync ingestion (spec §5 ``sync/push/``) and safety alerts.

Push semantics
  * Items are validated and stored one by one; a bad item is listed in ``rejected`` and never blocks
    the rest of the batch.
  * Idempotent on ``client_uuid``: re-pushing a stored record returns it as accepted without creating
    a duplicate. Observations and safety alerts are insert-once. Patrols are upserted because they
    evolve on the device (active -> paused -> ended); an ``ended`` patrol never reverts.
  * Track points have no client id; they are de-duplicated on (patrol_client_uuid, recorded_at).
  * ``cell_id`` is always derived server-side from lat/lon (client value ignored).
  * ``distance_m`` / ``duration_s`` are computed from stored track points / timestamps when the
    client did not send them. ``distance_clean_m`` — the distance over the *sanitised* track
    (``geo.track``) — is recomputed either way, so a client-supplied figure is kept but is no
    longer the only number available. Raw track points are always stored untouched.
  * Suspended licence: everything except safety alerts is rejected with the *retryable* code
    ``licence_suspended`` (keep it queued); safety alerts are always stored.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers

import geo
from accounts.licensing import effective_status
from areas.models import ApuBase, Area, GrtsCell, Team
from areas.services import cell_index
from audit.utils import audit
from core.exceptions import ApiError, first_message, validation_code
from notify.services import notify_managers

from . import hwc
from .models import Observation, Patrol, SafetyAlert, Species, TrackPoint
from .notifications import SyncReport, alert_email, observation_alert_email, queue_sync_summary
from .serializers import (
    ObservationInSerializer,
    PatrolInSerializer,
    SafetyAlertInSerializer,
    TrackPointInSerializer,
)

logger = logging.getLogger("patroliq.sync")


class CellLocator:
    """Lazily builds one CellIndex per area for the duration of a push."""

    def __init__(self, organisation):
        self.organisation = organisation
        self._indexes: dict = {}
        self._area_bounds = None

    def _index(self, area_id):
        if area_id not in self._indexes:
            self._indexes[area_id] = cell_index(area_id)
        return self._indexes[area_id]

    def find(self, lon: float, lat: float, area_id=None):
        if area_id is not None:
            return self._index(area_id).find(lon, lat)
        if self._area_bounds is None:
            self._area_bounds = []
            for aid, b in Area.objects.for_org(self.organisation).exclude(boundary=None).values_list("pk", "boundary"):
                if GrtsCell.objects.filter(area_id=aid).exists():
                    self._area_bounds.append((aid, geo.shape_from_geojson(b).bounds))
        for aid, (minx, miny, maxx, maxy) in self._area_bounds:
            if minx <= lon <= maxx and miny <= lat <= maxy:
                cid = self._index(aid).find(lon, lat)
                if cid:
                    return cid
        return None


def _cu(item):
    return str(item.get("client_uuid")) if isinstance(item, dict) and item.get("client_uuid") else None


def _reject(rejected, client_uuid, code, message):
    rejected.append({"client_uuid": client_uuid, "code": code, "message": message})


def _invalid(rejected, client_uuid, errors, prefix=""):
    code = validation_code(errors)
    msg = "Unexpected fields: " + ", ".join(sorted(errors)) if code == "unexpected_fields" else first_message(errors)
    _reject(rejected, client_uuid, code, f"{prefix}{msg}")


def track_fixes(patrol: Patrol):
    """The patrol's stored track points, time-ordered, in ``geo.clean_track`` input shape."""
    return (TrackPoint.objects.filter(organisation_id=patrol.organisation_id, patrol_id=patrol.pk)
            .order_by("recorded_at").values_list("lat", "lon", "accuracy_m", "speed_mps", "recorded_at"))


def sanitised_track(patrol: Patrol) -> geo.CleanTrack:
    """Sanitise the patrol's stored track with its own patrol-type speed ceiling."""
    return geo.clean_track(track_fixes(patrol), patrol_type=patrol.patrol_type)


def recompute_patrol_metrics(patrol: Patrol) -> None:
    """
    Refresh the derived distance/duration columns from the stored track points.

    ``distance_clean_m`` is always recomputed: even when the client sent ``distance_m`` (older app
    versions summed unfiltered GPS *and* network fixes), the sanitised figure is the one reports
    should use. It stays NULL while the patrol has no track points at all, because then there is
    nothing to measure — 0.0 would wrongly read as "walked nowhere". When the server owns the
    distance, ``distance_m`` is the sanitised total too.
    """
    fields = ["distance_clean_m"]
    track = sanitised_track(patrol)
    patrol.distance_clean_m = round(track.distance_m, 1) if track.total else None
    if not patrol.distance_from_client:
        patrol.distance_m = patrol.distance_clean_m or 0.0
        fields.append("distance_m")
    if not patrol.duration_from_client:
        end = patrol.ended_at
        if end is None:
            last = TrackPoint.objects.filter(patrol_id=patrol.pk).order_by("-recorded_at").values_list("recorded_at", flat=True).first()
            end = last
        patrol.duration_s = max(0, int((end - patrol.started_at).total_seconds())) if end else 0
        fields.append("duration_s")
    if fields:
        Patrol.objects.filter(pk=patrol.pk).update(**{f: getattr(patrol, f) for f in fields})


def process_push(request, payload: dict) -> dict:
    user = request.user
    org = user.organisation
    suspended = effective_status(org) == "suspended"
    accepted = {"patrols": [], "observations": [], "safety_alerts": []}
    rejected: list[dict] = []
    locator = CellLocator(org)
    area_ids = set(Area.objects.for_org(org).values_list("pk", flat=True))
    touched_patrols: set = set()
    alerts_to_notify: list[Observation] = []
    report = SyncReport()  # newly created / just-ended records, for the sync summary email

    # ---- patrols --------------------------------------------------------------------------------
    for item in payload.get("patrols") or []:
        cu = _cu(item)
        if suspended:
            _reject(rejected, cu, "licence_suspended", "Licence suspended; keep queued and retry later.")
            continue
        s = PatrolInSerializer(data=item)
        if not s.is_valid():
            _invalid(rejected, cu, s.errors)
            continue
        d = s.validated_data
        if d.get("ranger_id") and d["ranger_id"] != user.pk:
            _reject(rejected, cu, "forbidden", "ranger_id must be the signed-in user.")
            continue
        if d["area_id"] not in area_ids:
            _reject(rejected, cu, "invalid_area", "Unknown area_id.")
            continue
        if d.get("team_id") and not Team.objects.for_org(org).filter(pk=d["team_id"]).exists():
            _reject(rejected, cu, "invalid_team", "Unknown team_id.")
            continue
        if d.get("apu_base_id") and not ApuBase.objects.for_org(org).filter(pk=d["apu_base_id"]).exists():
            _reject(rejected, cu, "invalid_apu_base", "Unknown apu_base_id.")
            continue
        existing = Patrol.objects.filter(pk=d["client_uuid"]).first()
        if existing and (existing.organisation_id != org.pk or existing.ranger_id != user.pk):
            _reject(rejected, cu, "client_uuid_conflict", "client_uuid already used by another record.")
            continue
        values = dict(
            team_id=d.get("team_id"), area_id=d["area_id"], apu_base_id=d.get("apu_base_id"),
            patrol_type=d["patrol_type"], started_at=d["started_at"], ended_at=d.get("ended_at"),
            status=d["status"], notes=d.get("notes") or "", debrief_audio=d.get("debrief_audio") or None,
        )
        if d.get("distance_m") is not None:
            values.update(distance_m=d["distance_m"], distance_from_client=True)
        if d.get("duration_s") is not None:
            values.update(duration_s=d["duration_s"], duration_from_client=True)
        started = ended = False
        try:
            with transaction.atomic():
                if existing is None:
                    Patrol.objects.create(client_uuid=d["client_uuid"], organisation=org, ranger=user, **values)
                    started, ended = True, d["status"] == "ended"
                elif not (existing.status == "ended" and d["status"] != "ended"):
                    was_ended = existing.status == "ended"
                    for k, v in values.items():
                        setattr(existing, k, v)
                    existing.save()
                    ended = not was_ended and d["status"] == "ended"
        except IntegrityError:
            started = ended = False  # a concurrent push of the same patrol won the insert
            if not Patrol.objects.filter(pk=d["client_uuid"], organisation=org, ranger=user).exists():
                _reject(rejected, cu, "client_uuid_conflict", "client_uuid already used by another record.")
                continue
        if started or ended:
            report.patrol(d["client_uuid"], started=started, ended=ended)
        touched_patrols.add(d["client_uuid"])
        accepted["patrols"].append(str(d["client_uuid"]))

    # ---- track points ---------------------------------------------------------------------------
    tp_accepted = 0
    new_points: list[TrackPoint] = []
    patrol_cache: dict = {}
    suspended_groups: set = set()
    for i, item in enumerate(payload.get("track_points") or []):
        pcu = str(item.get("patrol_client_uuid")) if isinstance(item, dict) and item.get("patrol_client_uuid") else None
        if suspended:
            if pcu not in suspended_groups:
                suspended_groups.add(pcu)
                _reject(rejected, pcu, "licence_suspended", "Licence suspended; keep queued and retry later.")
            continue
        s = TrackPointInSerializer(data=item)
        if not s.is_valid():
            _invalid(rejected, pcu, s.errors, prefix=f"track_points[{i}]: ")
            continue
        d = s.validated_data
        pid = d["patrol_client_uuid"]
        if pid not in patrol_cache:
            patrol_cache[pid] = Patrol.objects.filter(pk=pid).values("organisation_id", "ranger_id", "area_id").first()
        p = patrol_cache[pid]
        if p and (p["organisation_id"] != org.pk or p["ranger_id"] != user.pk):
            _reject(rejected, pcu, "client_uuid_conflict", f"track_points[{i}]: patrol belongs to another user.")
            continue
        cell_id = locator.find(d["lon"], d["lat"], p["area_id"] if p else None)
        new_points.append(TrackPoint(organisation=org, patrol_id=pid, recorded_at=d["recorded_at"], lat=d["lat"],
                                     lon=d["lon"], accuracy_m=d.get("accuracy_m"), speed_mps=d.get("speed_mps"),
                                     cell_id=cell_id))
        touched_patrols.add(pid)
        tp_accepted += 1
    if new_points:
        TrackPoint.objects.bulk_create(new_points, ignore_conflicts=True, batch_size=500)

    for patrol in Patrol.objects.filter(pk__in=touched_patrols, organisation=org):
        recompute_patrol_metrics(patrol)

    # ---- observations ---------------------------------------------------------------------------
    for item in payload.get("observations") or []:
        cu = _cu(item)
        if suspended:
            _reject(rejected, cu, "licence_suspended", "Licence suspended; keep queued and retry later.")
            continue
        s = ObservationInSerializer(data=item)
        if not s.is_valid():
            _invalid(rejected, cu, s.errors)
            continue
        d = s.validated_data
        existing = Observation.objects.filter(pk=d["client_uuid"]).values("organisation_id", "observer_id").first()
        if existing:
            if existing["organisation_id"] == org.pk and existing["observer_id"] == user.pk:
                accepted["observations"].append(str(d["client_uuid"]))  # idempotent replay
            else:
                _reject(rejected, cu, "client_uuid_conflict", "client_uuid already used by another record.")
            continue
        if d.get("observer_id") and d["observer_id"] != user.pk:
            _reject(rejected, cu, "forbidden", "observer_id must be the signed-in user.")
            continue
        if d["area_id"] not in area_ids:
            _reject(rejected, cu, "invalid_area", "Unknown area_id.")
            continue
        pid = d.get("patrol_client_uuid")
        if pid:
            p = Patrol.objects.filter(pk=pid).values("organisation_id", "ranger_id").first()
            if p and p["organisation_id"] != org.pk:
                _reject(rejected, cu, "client_uuid_conflict", "patrol_client_uuid belongs to another organisation.")
                continue
        species_id = d.get("species_id")
        if species_id and not Species.objects.filter(pk=species_id).exists():
            species_id = None  # unknown catalogue id: keep species_name, drop the reference
        obs_fields = {k: d.get(k) for k in (
            "category", "subtype", "species_name", "count", "sex", "male_count", "female_count", "age_class",
            "behaviour", "severity", "direction_of_travel", "alert_manager", "lat", "lon", "accuracy_m",
            "ai_species_confidence", "recorded_at", "voice_transcript")}
        obs_fields["notes"] = d.get("notes") or ""
        try:
            with transaction.atomic():
                obs = Observation.objects.create(
                    client_uuid=d["client_uuid"], organisation=org, observer=user, area_id=d["area_id"],
                    patrol_id=pid, species_id=species_id,
                    cell_id=locator.find(d["lon"], d["lat"], d["area_id"]), **obs_fields,
                )
        except IntegrityError:
            if Observation.objects.filter(pk=d["client_uuid"], organisation=org, observer=user).exists():
                accepted["observations"].append(str(d["client_uuid"]))
            else:
                _reject(rejected, cu, "client_uuid_conflict", "client_uuid already used by another record.")
            continue
        accepted["observations"].append(str(obs.pk))
        report.observations.append(obs.pk)
        if obs.alert_manager or (obs.category in ("threat", "carcass") and obs.severity in ("high", "critical")):
            alerts_to_notify.append(obs)

    # ---- safety alerts (always accepted, even when suspended) -------------------------------
    for item in payload.get("safety_alerts") or []:
        cu = _cu(item)
        try:
            alert, _ = record_safety_alert(request, item if isinstance(item, dict) else {}, report=report)
            accepted["safety_alerts"].append(str(alert.pk))
        except serializers.ValidationError as exc:
            _invalid(rejected, cu, exc.detail)
        except ApiError as exc:
            _reject(rejected, cu, exc.code, exc.message)

    for obs in alerts_to_notify:
        cell_label = obs.cell.label if obs.cell_id else "outside grid"
        title = f"PATROLIQ {obs.category.upper()} ALERT"
        body = (f"{user.full_name}: {obs.subtype or obs.category} ({obs.severity or 'n/a'}) at "
                f"{obs.lat:.5f},{obs.lon:.5f} [{cell_label}] {obs.recorded_at:%Y-%m-%d %H:%M}Z")
        notify_managers(org.pk, title, body, {"type": "threat", "observation_client_uuid": str(obs.pk)},
                        email=_safe_email(observation_alert_email, obs, title, body))

    user.last_sync_at = timezone.now()
    user.save(update_fields=["last_sync_at"])
    audit(request, "sync.push", target=user, detail={
        "patrols": len(accepted["patrols"]), "observations": len(accepted["observations"]),
        "safety_alerts": len(accepted["safety_alerts"]), "track_points": tp_accepted, "rejected": len(rejected)})
    queue_sync_summary(request, report)
    return {"accepted": accepted, "track_points_accepted": tp_accepted, "rejected": rejected}


def _safe_email(builder, *args):
    """Render an alert email; on any error fall back to the plain one (the SMS must still go out)."""
    try:
        return builder(*args)
    except Exception:  # noqa: BLE001
        logger.exception("could not build the alert email")
        return None


# --- safety ----------------------------------------------------------------------------------------

def _alert_message(alert: SafetyAlert) -> tuple[str, str]:
    titles = {"panic": "PATROLIQ PANIC ALERT", "dead_mans_switch": "PATROLIQ DEAD MAN'S SWITCH ALERT",
              SafetyAlert.HWC: "PATROLIQ HUMAN-WILDLIFE CONFLICT"}
    title = titles.get(alert.kind, "PATROLIQ SAFETY ALERT")
    ranger = alert.ranger
    where = f"{alert.lat:.5f},{alert.lon:.5f}" if alert.lat is not None and alert.lon is not None else "no GPS fix"
    battery = f"{alert.battery_pct}%" if alert.battery_pct is not None else "unknown"
    body = (f"{ranger.full_name} ({ranger.employee_id or ranger.email}) at {where}, battery {battery}, "
            f"{alert.started_at:%Y-%m-%d %H:%M}Z")
    if alert.kind == SafetyAlert.HWC:
        area_name = alert.area.name if alert.area_id else None
        body = (f"{ranger.full_name} ({ranger.employee_id or ranger.email}) at {where}"
                + (f" [{area_name}]" if area_name else "")
                + f", {alert.started_at:%Y-%m-%d %H:%M}Z")
        if alert.details:
            body += f" — {hwc.details_summary(alert.details)}"
    return title, body


def _resolve_alert_area(user, area_id):
    """Unknown / other-tenant area ids are dropped, never rejected (safety path)."""
    if not area_id:
        return None
    return Area.objects.for_org(user.organisation).filter(pk=area_id).values_list("pk", flat=True).first()


def record_safety_alert(request, data: dict, report: SyncReport | None = None) -> tuple[SafetyAlert, bool]:
    """
    Create (or refresh) a safety alert; returns ``(alert, created)``.

    What was *new* (the alert itself, or a first HWC details log) is added to ``report``. Called
    without a report (``POST safety/alerts/``) the call is its own sync and queues the sync summary
    email itself; ``process_push`` passes its batch report and queues one summary at the end.

    Replaying the same ``client_uuid`` with a ``details`` block is how the phone logs an HWC details
    log after the alert was raised (spec v1.5 §A3): non-null keys are merged into the stored object,
    ``details_updated_at`` is set, and an ``AlertEvent`` note is written. Details are ignored once the
    alert is ``cancelled`` and for non-HWC kinds. The collected ``details_warnings`` are attached to
    the returned instance as ``alert.details_warnings`` for the view to echo.

    Raises serializers.ValidationError / ApiError.
    """
    own_report = report is None
    report = SyncReport() if own_report else report
    result = _record_safety_alert(request, data, report)
    if own_report:
        queue_sync_summary(request, report)
    return result


def _record_safety_alert(request, data: dict, report: SyncReport) -> tuple[SafetyAlert, bool]:
    user = request.user
    s = SafetyAlertInSerializer(data=data)
    s.is_valid(raise_exception=True)
    d = s.validated_data
    ignored = sorted(set(data.keys()) - set(s.fields.keys()))
    details_in, warnings = hwc.clean_details(d.get("details"))
    if details_in:
        details_in = hwc.resolve_species(details_in)
    existing = SafetyAlert.objects.select_related("ranger", "area").filter(pk=d["client_uuid"]).first()
    if existing:
        if existing.organisation_id != user.organisation_id or existing.ranger_id != user.pk:
            raise ApiError(409, "client_uuid_conflict", "client_uuid already used by another record.")
        if existing.status == "active":
            changed = [f for f in ("lat", "lon", "accuracy_m", "battery_pct", "signal_level") if d.get(f) is not None]
            for f in changed:
                setattr(existing, f, d[f])
            if changed:
                existing.save(update_fields=changed + ["updated_at"])
        _merge_hwc_details(request, existing, details_in, warnings, report)
        existing.details_warnings = warnings
        return existing, False
    try:
        with transaction.atomic():
            alert = SafetyAlert.objects.create(
                client_uuid=d["client_uuid"], organisation_id=user.organisation_id, ranger=user, kind=d["kind"],
                status="active", lat=d.get("lat"), lon=d.get("lon"), accuracy_m=d.get("accuracy_m"),
                battery_pct=d.get("battery_pct"), signal_level=d.get("signal_level"),
                started_at=d.get("started_at") or timezone.now(),
                area_id=_resolve_alert_area(user, d.get("area_id")),
                details=details_in or None if d["kind"] == SafetyAlert.HWC else None,
                details_updated_at=timezone.now() if (details_in and d["kind"] == SafetyAlert.HWC) else None,
            )
    except IntegrityError:
        # Concurrent replay of our own alert, or (under PostgreSQL RLS, where other tenants' rows are
        # invisible to the lookup above) a client_uuid owned by someone else.
        replay = SafetyAlert.objects.select_related("ranger", "area").filter(pk=d["client_uuid"], ranger=user).first()
        if replay is None:
            raise ApiError(409, "client_uuid_conflict", "client_uuid already used by another record.")
        _merge_hwc_details(request, replay, details_in, warnings, report)
        replay.details_warnings = warnings
        return replay, False
    alert.details_warnings = warnings
    audit(request, "safety_alert.create", target=alert, detail={
        "kind": alert.kind, "lat": alert.lat, "lon": alert.lon, "battery_pct": alert.battery_pct,
        "area_id": str(alert.area_id) if alert.area_id else None,
        "details_fields": sorted(alert.details or {}), "details_warnings": warnings,
        "ranger_id_mismatch": bool(d.get("ranger_id") and d["ranger_id"] != user.pk), "ignored_fields": ignored})
    report.alerts.append(alert.pk)
    title, body = _alert_message(alert)
    notify_managers(user.organisation_id, title, body, {"type": "safety", "kind": alert.kind,
                                                         "client_uuid": str(alert.pk)},
                    email=_safe_email(alert_email, alert, title, body))
    return alert, True


def _merge_hwc_details(request, alert: SafetyAlert, details_in: dict, warnings: list[str],
                       report: SyncReport | None = None) -> None:
    """Merge a details log into an existing HWC alert (spec v1.5 §A3/§A4)."""
    from .alerts import record_event

    if not details_in:
        return
    if alert.kind != SafetyAlert.HWC:
        warnings.append("details: ignored for this alert kind")
        return
    if alert.status == "cancelled":
        warnings.append("details: ignored because the alert is cancelled")
        return
    first = alert.details_updated_at is None
    alert.details = hwc.merge_details(alert.details, details_in)
    alert.details_updated_at = timezone.now()
    alert.save(update_fields=["details", "details_updated_at", "updated_at"])
    record_event(alert, "note", actor=request.user, note="Details logged" if first else "Details updated",
                 at=alert.details_updated_at)
    audit(request, "safety_alert.details", target=alert,
          detail={"fields": sorted(details_in), "first": first, "details_warnings": warnings})
    if first:
        if report is not None:
            report.hwc_details.append(alert.pk)
        title, body = "PATROLIQ HWC DETAILS", f"{alert.ranger.full_name}: {hwc.details_summary(alert.details)}"
        notify_managers(alert.organisation_id, title, body,
                        {"type": "safety", "kind": alert.kind, "client_uuid": str(alert.pk)},
                        email=_safe_email(alert_email, alert, title, body))


def cancel_safety_alert(request, alert: SafetyAlert, note: str | None) -> SafetyAlert:
    if alert.status in ("cancelled", "resolved"):
        return alert
    alert.status = "cancelled"
    alert.resolved_at = timezone.now()
    alert.resolution_note = note or None
    alert.save(update_fields=["status", "resolved_at", "resolution_note", "updated_at"])
    from .alerts import record_event

    record_event(alert, "cancelled", actor=request.user, note=note or "", at=alert.resolved_at)
    audit(request, "safety_alert.cancel", target=alert, detail={"note": note or ""})
    title = "PATROLIQ SOS CANCELLED"
    body = f"{alert.ranger.full_name} cancelled the {alert.get_kind_display().lower()} alert (PIN verified)."
    notify_managers(alert.organisation_id, title, body, {"type": "safety_cancelled", "client_uuid": str(alert.pk)},
                    email=_safe_email(alert_email, alert, title, body))
    return alert
