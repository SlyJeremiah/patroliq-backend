"""
Manager dashboard computations (spec §7): ranger live status, summary, GRTS coverage, risk.

Everything is organisation-scoped by the caller passing ``org``; nothing here reads the request.
"""
from __future__ import annotations

import calendar
import zoneinfo
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.db.models import Avg, Count, Max, OuterRef, Q, Subquery
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework import serializers

from accounts.models import AuthToken, User
from areas.models import Area, Assignment, GrtsCell, RiskScore, Sector
from core.roles import RANGER
from field.alerts import alert_item, is_open, latest_dispatches, threat_alert_q
from field.models import Observation, Patrol, PositionPing, SafetyAlert, TrackPoint

ACTIVE_WINDOW = timedelta(minutes=15)


def online_window() -> timedelta:
    """Idle phones sync every 30 min, so any contact within this window counts as online."""
    return timedelta(minutes=settings.RANGER_ONLINE_MINUTES)
_dt = serializers.DateTimeField()


def fmt(value):
    return _dt.to_representation(value) if value else None


# --- time --------------------------------------------------------------------------------------------

def area_tz(area: Area | None, org=None) -> zoneinfo.ZoneInfo:
    name = area.timezone if area is not None else None
    if name is None and org is not None:
        name = (Area.objects.for_org(org).exclude(status="archived").order_by("name")
                .values_list("timezone", flat=True).first())
    try:
        return zoneinfo.ZoneInfo(name or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo("UTC")


def local_today(tz) -> date:
    return timezone.now().astimezone(tz).date()


def day_bounds(day: date, tz) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=tz)
    return start, datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)


def month_bounds(year: int, month: int, tz) -> tuple[datetime, datetime]:
    last = calendar.monthrange(year, month)[1]
    return day_bounds(date(year, month, 1), tz)[0], day_bounds(date(year, month, last), tz)[1]


# --- rangers --------------------------------------------------------------------------------------------

def rangers_in_scope(org, area_id=None, include_inactive=False):
    qs = User.objects.filter(organisation=org, role=RANGER)
    if not include_inactive:
        qs = qs.filter(is_active=True)
    if area_id:
        qs = qs.filter(pk__in=User.objects.filter(Q(areas=area_id) | Q(team__area_id=area_id)).values("pk"))
    return qs


@dataclass
class RangerLive:
    user: User
    status: str
    last_ping: PositionPing | None
    open_patrol: Patrol | None
    last_activity_at: datetime | None


def live_status(org, rangers: list[User], now=None) -> dict:
    """
    Batch status computation: {ranger_id: RangerLive}. Precedence sos > paused > active > online > offline.
    ``online``: no open patrol (or an open one gone quiet), but the phone reached the API recently (bootstrap sync,
    position ping or any authenticated call).
    """
    now = now or timezone.now()
    ids = [u.pk for u in rangers]
    latest_ping_ids = (User.objects.filter(pk__in=ids).annotate(
        ping_id=Subquery(PositionPing.objects.filter(ranger=OuterRef("pk")).order_by("-recorded_at").values("pk")[:1]))
        .values_list("pk", "ping_id"))
    ping_by_id = {p.pk: p for p in PositionPing.objects.filter(pk__in=[pid for _, pid in latest_ping_ids if pid])}
    pings = {uid: ping_by_id.get(pid) for uid, pid in latest_ping_ids}
    open_patrols: dict = {}
    for p in Patrol.objects.for_org(org).filter(ranger_id__in=ids, status__in=["active", "paused"]).order_by("started_at"):
        open_patrols[p.ranger_id] = p  # latest started wins
    sos = set(SafetyAlert.objects.for_org(org).filter(ranger_id__in=ids, status__in=["active", "acknowledged"])
              .values_list("ranger_id", flat=True))
    last_call = dict(AuthToken.objects.filter(user_id__in=ids).values("user_id").annotate(t=Max("last_used_at"))
                     .values_list("user_id", "t"))
    out = {}
    for u in rangers:
        ping, patrol = pings.get(u.pk), open_patrols.get(u.pk)
        last_activity = max([t for t in (ping.recorded_at if ping else None, u.last_sync_at) if t], default=None)
        last_seen = max([t for t in (last_activity, last_call.get(u.pk)) if t], default=None)
        if u.pk in sos:
            status = "sos"
        elif patrol is not None and patrol.status == "paused":
            status = "paused"
        elif patrol is not None and last_activity is not None and now - last_activity <= ACTIVE_WINDOW:
            status = "active"
        elif last_seen is not None and now - last_seen <= online_window():
            status = "online"
        else:
            status = "offline"
        out[u.pk] = RangerLive(u, status, ping, patrol, last_seen)
    return out


def ranger_today(org, ids, tz) -> dict:
    start, end = day_bounds(local_today(tz), tz)
    today = defaultdict(lambda: {"distance_m": 0, "observations": 0, "patrols": 0, "cells_visited": set()})
    for rid, dist in Patrol.objects.for_org(org).filter(ranger_id__in=ids, started_at__gte=start, started_at__lt=end) \
            .values_list("ranger_id", "distance_m"):
        today[rid]["patrols"] += 1
        today[rid]["distance_m"] += dist or 0
    for rid, n in Observation.objects.for_org(org).filter(observer_id__in=ids, recorded_at__gte=start, recorded_at__lt=end) \
            .values("observer_id").annotate(n=Count("pk")).values_list("observer_id", "n"):
        today[rid]["observations"] = n
    for rid, label in TrackPoint.objects.filter(organisation_id=org.pk, patrol__ranger_id__in=ids, recorded_at__gte=start,
                                                recorded_at__lt=end, cell__isnull=False) \
            .values_list("patrol__ranger_id", "cell__label").distinct():
        today[rid]["cells_visited"].add(label)
    for rid, label in Observation.objects.for_org(org).filter(observer_id__in=ids, recorded_at__gte=start, recorded_at__lt=end,
                                                             cell__isnull=False).values_list("observer_id", "cell__label").distinct():
        today[rid]["cells_visited"].add(label)
    return {rid: {"distance_m": int(round(v["distance_m"])), "observations": v["observations"], "patrols": v["patrols"],
                  "cells_visited": sorted(v["cells_visited"])} for rid, v in today.items()}


def ranger_payloads(org, rangers: list[User], area: Area | None = None) -> list[dict]:
    live = live_status(org, rangers)
    tz = area_tz(area, org)
    today = ranger_today(org, [u.pk for u in rangers], tz)
    empty = {"distance_m": 0, "observations": 0, "patrols": 0, "cells_visited": []}
    out = []
    for u in rangers:
        lv = live[u.pk]
        ping, patrol = lv.last_ping, lv.open_patrol
        out.append({
            "id": str(u.pk), "full_name": u.full_name, "employee_id": u.employee_id, "phone": u.phone,
            "team_id": str(u.team_id) if u.team_id else None, "team_name": u.team.name if u.team_id else None,
            "apu_base_id": str(u.apu_base_id) if u.apu_base_id else None,
            "apu_base_code": u.apu_base.code if u.apu_base_id else None,
            "status": lv.status,
            "last_position": {"lat": ping.lat, "lon": ping.lon, "accuracy_m": ping.accuracy_m,
                              "battery_pct": ping.battery_pct, "recorded_at": fmt(ping.recorded_at)} if ping else None,
            "current_patrol": {"client_uuid": str(patrol.pk), "started_at": fmt(patrol.started_at), "status": patrol.status,
                               "distance_m": int(round(patrol.distance_m or 0)), "patrol_type": patrol.patrol_type}
            if patrol else None,
            "today": today.get(u.pk, empty),
            "last_sync_at": fmt(u.last_sync_at),
        })
    return out


def ranger_detail_extras(org, ranger: User) -> dict:
    obs = (Observation.objects.for_org(org).filter(observer=ranger).select_related("cell").order_by("-recorded_at")[:10])
    recent = [{
        "client_uuid": str(o.pk), "category": o.category, "subtype": o.subtype, "species_name": o.species_name,
        "count": o.count, "sex": o.sex, "severity": o.severity, "alert_manager": o.alert_manager, "lat": o.lat,
        "lon": o.lon, "cell_id": str(o.cell_id) if o.cell_id else None, "cell_label": o.cell.label if o.cell_id else None,
        "area_id": str(o.area_id), "patrol_client_uuid": str(o.patrol_id) if o.patrol_id else None,
        "recorded_at": fmt(o.recorded_at),
    } for o in obs]
    rows = (list(SafetyAlert.objects.for_org(org).filter(ranger=ranger).select_related("ranger")[:5])
            + list(Observation.objects.for_org(org).filter(threat_alert_q(), observer=ranger).select_related("observer")[:5]))
    dispatched = latest_dispatches(org, [r.pk for r in rows])
    alerts = sorted((alert_item(r, dispatched.get(r.pk)) for r in rows), key=lambda a: a["occurred_at"] or "", reverse=True)[:5]
    return {"recent_observations": recent, "alerts": alerts}


# --- alerts -------------------------------------------------------------------------------------------

def open_alert_items(org, area_id=None, ranger_ids=None) -> list[dict]:
    safety = SafetyAlert.objects.for_org(org).filter(status__in=["active", "acknowledged"]).select_related("ranger")
    if ranger_ids is not None:
        safety = safety.filter(ranger_id__in=ranger_ids)
    threats = Observation.objects.for_org(org).filter(threat_alert_q(), acknowledged_at__isnull=True).select_related("observer")
    if area_id:
        threats = threats.filter(area_id=area_id)
    return [i for i in (alert_item(r) for r in list(safety) + list(threats)) if is_open(i)]


# --- GRTS coverage --------------------------------------------------------------------------------------

def parse_month(raw: str | None, tz) -> tuple[int, int]:
    if not raw:
        today = local_today(tz)
        return today.year, today.month
    try:
        y, m = raw.split("-")
        year, month = int(y), int(m)
        if not (1 <= month <= 12 and 2000 <= year <= 2100) or len(raw) != 7:
            raise ValueError
    except ValueError:
        raise serializers.ValidationError({"month": ["Expected YYYY-MM."]})
    return year, month


def season_start(year: int, month: int) -> tuple[int, int]:
    """Zimbabwe seasons (as in the risk engine): dry May–Oct, wet Nov–Apr."""
    if 5 <= month <= 10:
        return year, 5
    return (year, 11) if month >= 11 else (year - 1, 11)


def visit_days(area: Area, start: datetime, end: datetime, tz) -> dict:
    """{cell_id: set(local dates)} from track points and observations inside [start, end)."""
    days: dict = defaultdict(set)
    tp = (TrackPoint.objects.filter(organisation_id=area.organisation_id, cell__area=area, recorded_at__gte=start,
                                    recorded_at__lt=end)
          .annotate(d=TruncDate("recorded_at", tzinfo=tz)).values_list("cell_id", "d").distinct())
    ob = (Observation.objects.filter(organisation_id=area.organisation_id, area=area, cell__isnull=False,
                                     recorded_at__gte=start, recorded_at__lt=end)
          .annotate(d=TruncDate("recorded_at", tzinfo=tz)).values_list("cell_id", "d").distinct())
    for cid, d in list(tp) + list(ob):
        days[cid].add(d)
    return days


def default_visit_target(area: Area, start: datetime, end: datetime, tz) -> int:
    target = (Assignment.objects.filter(organisation_id=area.organisation_id, area=area,
                                        date__gte=start.astimezone(tz).date(), date__lt=end.astimezone(tz).date())
              .aggregate(v=Max("visit_target"))["v"])
    return int(target or 1)


def coverage(area: Area, month: str | None = None, visit_target: int | None = None) -> dict:
    tz = area_tz(area)
    year, mon = parse_month(month, tz)
    start, end = month_bounds(year, mon, tz)
    target = visit_target or default_visit_target(area, start, end, tz)
    cells = list(GrtsCell.objects.filter(area=area).order_by("grts_order").values("pk", "label", "sector_id"))
    month_days = visit_days(area, start, end, tz)
    sy, sm = season_start(year, mon)
    season_days = visit_days(area, month_bounds(sy, sm, tz)[0], end, tz)

    org_id = area.organisation_id
    before = set(TrackPoint.objects.filter(organisation_id=org_id, cell__area=area, recorded_at__lt=start)
                 .values_list("cell_id", flat=True).distinct())
    before |= set(Observation.objects.filter(organisation_id=org_id, area=area, cell__isnull=False, recorded_at__lt=start)
                  .values_list("cell_id", flat=True).distinct())
    last_visit: dict = {}
    for model_qs in (
        TrackPoint.objects.filter(organisation_id=org_id, cell__area=area, recorded_at__lt=end),
        Observation.objects.filter(organisation_id=org_id, area=area, cell__isnull=False, recorded_at__lt=end),
    ):
        for cid, last in model_qs.values("cell_id").annotate(last=Max("recorded_at")).values_list("cell_id", "last"):
            if cid not in last_visit or last > last_visit[cid]:
                last_visit[cid] = last
    obs_counts = dict(Observation.objects.filter(organisation_id=org_id, area=area, cell__isnull=False,
                                                 recorded_at__gte=start, recorded_at__lt=end)
                      .values("cell_id").annotate(n=Count("pk")).values_list("cell_id", "n"))

    out_cells, sectors = [], defaultdict(lambda: {"cells": 0, "complete": 0, "partial": 0, "pending": 0, "never": 0})
    total_visits = complete = never = season_visited = 0
    for c in cells:
        visits = len(month_days.get(c["pk"], ()))
        if visits >= target:
            status = "complete"
        elif visits > 0:
            status = "partial"
        elif c["pk"] in before:
            status = "pending"
        else:
            status = "never"
        total_visits += visits
        complete += status == "complete"
        never += status == "never"
        season_visited += bool(season_days.get(c["pk"]))
        s = sectors[c["sector_id"]]
        s["cells"] += 1
        s[status] += 1
        out_cells.append({"cell_id": str(c["pk"]), "label": c["label"],
                          "sector_id": str(c["sector_id"]) if c["sector_id"] else None, "visits": visits,
                          "status": status, "last_visit_at": fmt(last_visit.get(c["pk"])),
                          "observations": obs_counts.get(c["pk"], 0)})
    n = len(cells)
    sector_rows = []
    for sector in Sector.objects.filter(area=area).order_by("name"):
        s = sectors.get(sector.pk, {"cells": 0, "complete": 0, "partial": 0, "pending": 0, "never": 0})
        sector_rows.append({"id": str(sector.pk), "name": sector.name, **s,
                            "coverage_pct": round(s["complete"] / s["cells"], 4) if s["cells"] else 0.0})
    return {
        "area_id": str(area.pk), "month": f"{year:04d}-{mon:02d}", "visit_target": target,
        "coverage_pct": round(complete / n, 4) if n else 0.0,
        "season_coverage_pct": round(season_visited / n, 4) if n else 0.0,
        "season_start": f"{sy:04d}-{sm:02d}", "never_surveyed": never,
        "mean_visits": round(total_visits / n, 2) if n else 0.0,
        "sectors": sector_rows, "cells": out_cells,
    }


# --- risk -------------------------------------------------------------------------------------------------

def model_confidence(area: Area) -> str:
    since = timezone.now() - timedelta(days=90)
    n = (Patrol.objects.filter(organisation_id=area.organisation_id, area=area, started_at__gte=since).count()
         + Observation.objects.filter(organisation_id=area.organisation_id, area=area, recorded_at__gte=since).count())
    return "low" if n < 20 else "moderate" if n < 200 else "high"


def risk_map(area: Area, day: date | None) -> dict:
    tz = area_tz(area)
    requested = day or local_today(tz)
    scored = (RiskScore.objects.filter(organisation_id=area.organisation_id, area=area, date__lte=requested)
              .order_by("-date").values_list("date", flat=True).first())
    rows = []
    if scored is not None:
        rows = list(RiskScore.objects.filter(organisation_id=area.organisation_id, area=area, date=scored)
                    .select_related("cell").order_by("cell__grts_order"))
    return {
        "area_id": str(area.pk), "date": (scored or requested).isoformat(), "requested_date": requested.isoformat(),
        "engine": "heuristic", "model_confidence": model_confidence(area),
        "cells": [{"cell_id": str(r.cell_id), "label": r.cell.label,
                   "sector_id": str(r.cell.sector_id) if r.cell.sector_id else None, "score": r.score, "level": r.level,
                   "factors": r.factors, "centroid": r.cell.centroid} for r in rows],
    }


def risk_trend(area: Area, days: int) -> list[dict]:
    tz = area_tz(area)
    end = local_today(tz)
    start = end - timedelta(days=days - 1)
    agg = {r["date"]: r for r in RiskScore.objects.filter(organisation_id=area.organisation_id, area=area,
                                                          date__gte=start, date__lte=end)
           .values("date").annotate(mean=Avg("score"), max=Max("score"),
                                    high=Count("pk", filter=Q(level="high")),
                                    critical=Count("pk", filter=Q(level="critical")))}
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        r = agg.get(d)
        out.append({"date": d.isoformat(),
                    "mean_score": round(r["mean"], 2) if r else None,
                    "max_score": round(r["max"], 2) if r else None,
                    "high_cells": r["high"] if r else 0,
                    "critical_cells": r["critical"] if r else 0})
    return out


# --- summary ------------------------------------------------------------------------------------------------

def summary(org, area: Area | None = None) -> dict:
    now = timezone.now()
    tz = area_tz(area, org)
    area_id = area.pk if area else None
    rangers = list(rangers_in_scope(org, area_id).select_related("team", "apu_base"))
    ids = [u.pk for u in rangers]
    live = live_status(org, rangers, now)
    counts = defaultdict(int)
    for lv in live.values():
        counts[lv.status] += 1

    week_ago, day_ago = now - timedelta(days=7), now - timedelta(hours=24)
    active_7d = {u.pk for u in rangers if u.last_sync_at and u.last_sync_at >= week_ago}
    active_7d |= set(Patrol.objects.for_org(org).filter(ranger_id__in=ids, started_at__gte=week_ago).values_list("ranger_id", flat=True))
    active_7d |= set(Observation.objects.for_org(org).filter(observer_id__in=ids, recorded_at__gte=week_ago)
                     .values_list("observer_id", flat=True))
    active_7d |= set(PositionPing.objects.for_org(org).filter(ranger_id__in=ids, recorded_at__gte=week_ago)
                     .values_list("ranger_id", flat=True))
    synced_24h = {u.pk for u in rangers if u.last_sync_at and u.last_sync_at >= day_ago}

    alerts = open_alert_items(org, area_id, ids if area_id else None)
    start, end = day_bounds(local_today(tz), tz)
    obs_today = Observation.objects.for_org(org).filter(recorded_at__gte=start, recorded_at__lt=end)
    patrols_today = Patrol.objects.for_org(org).filter(started_at__gte=start, started_at__lt=end)
    if area_id:
        obs_today, patrols_today = obs_today.filter(area_id=area_id), patrols_today.filter(area_id=area_id)

    areas = [area] if area else list(Area.objects.for_org(org).filter(status="active"))
    cells_total = cells_complete = 0
    for a in areas:
        cov = coverage(a)
        cells_total += len(cov["cells"])
        cells_complete += sum(1 for c in cov["cells"] if c["status"] == "complete")
    last_sync = max([u.last_sync_at for u in rangers if u.last_sync_at], default=None)
    return {
        "area_id": str(area_id) if area_id else None,
        "rangers_total": len(rangers),
        "rangers_active": counts["active"], "rangers_paused": counts["paused"], "rangers_online": counts["online"],
        "rangers_offline": counts["offline"],
        "rangers_sos": counts["sos"],
        "open_alerts": len(alerts),
        "critical_alerts": sum(1 for a in alerts if a["severity"] == "critical"),
        "sos_active": sum(1 for a in alerts if a["type"] == "safety"),
        "sync_rate_24h": round(len(synced_24h) / len(active_7d), 4) if active_7d else 0.0,
        "grts_coverage_month": round(cells_complete / cells_total, 4) if cells_total else 0.0,
        "observations_today": obs_today.count(),
        "patrols_today": patrols_today.count(),
        "last_ranger_sync_at": fmt(last_sync),
        "server_time": fmt(now),
    }
