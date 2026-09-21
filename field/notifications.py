"""
Manager emails built from field records (spec v1.6 §2).

* :func:`alert_email` / :func:`observation_alert_email` — the fuller body of an immediate alert email
  (the SMS keeps its one-line text).
* :class:`SyncReport` + :func:`queue_sync_summary` — what a ``sync/push/`` or ``safety/alerts/``
  call newly created, and the summary email sent when any of it is *reportable*: patrols seen for
  the first time or that just ended, new observations, new safety alerts, first HWC details.
  Track points, pings and replays of unchanged records are never reportable.

Times are shown in the area's timezone. Nothing from the personal record (national ID, date of
birth, address, next of kin) is ever included — only name, employee ID and the field data.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from . import hwc
from .models import Observation, Patrol, SafetyAlert

logger = logging.getLogger("patroliq.notify")

DASH = "—"


def _tz(area=None, organisation=None):
    from dashboard.services import area_tz  # lazy: dashboard.services imports field modules

    return area_tz(area, organisation)


def fmt_local(value, tz) -> str:
    if not value:
        return DASH
    return timezone.localtime(value, tz).strftime("%Y-%m-%d %H:%M %Z").strip()


def fmt_duration(seconds) -> str:
    seconds = int(seconds or 0)
    hours, rest = divmod(seconds, 3600)
    return f"{hours} h {rest // 60:02d} min" if hours else f"{rest // 60} min"


def fmt_distance(metres) -> str:
    metres = float(metres or 0)
    return f"{metres / 1000:.2f} km" if metres >= 1000 else f"{metres:.0f} m"


def map_url(lat, lon) -> str | None:
    if lat is None or lon is None:
        return None
    return f"https://www.google.com/maps?q={lat:.6f},{lon:.6f}"


def _where(lat, lon, accuracy=None) -> str:
    if lat is None or lon is None:
        return "No GPS fix"
    text = f"{lat:.5f}, {lon:.5f}"
    return text + (f" (±{accuracy:.0f} m)" if accuracy is not None else "")


def _alert_cell_label(alert: SafetyAlert) -> str | None:
    """Safety alerts carry no cell; locate it on the area's GRTS grid when possible."""
    if not alert.area_id or alert.lat is None or alert.lon is None:
        return None
    try:
        from areas.models import GrtsCell
        from areas.services import cell_index

        cell_id = cell_index(alert.area_id).find(alert.lon, alert.lat)
        return GrtsCell.objects.filter(pk=cell_id).values_list("label", flat=True).first() if cell_id else None
    except Exception:  # noqa: BLE001 — cosmetic; never block an alert email
        logger.exception("cell lookup for alert %s failed", alert.pk)
        return None


_SIGNAL = {0: "none", 1: "poor", 2: "fair", 3: "good", 4: "excellent"}


def _hwc_rows(details: dict | None) -> list[tuple[str, str]]:
    d = details or {}
    rows = []
    if d.get("conflict_type"):
        rows.append(("Conflict", hwc.humanise(d["conflict_type"]).capitalize()))
    if d.get("species_name"):
        rows.append(("Species", str(d["species_name"])))
    for key, label in (("animal_count", "Animals"), ("people_affected", "People affected"),
                       ("people_injured", "People injured"), ("people_killed", "People killed"),
                       ("livestock_lost", "Livestock lost")):
        if d.get(key) is not None:
            rows.append((label, str(d[key])))
    for key, label in (("location_description", "Place"), ("action_taken", "Action taken"), ("notes", "Notes")):
        if d.get(key):
            rows.append((label, str(d[key])))
    return rows


def safety_alert_item(alert: SafetyAlert, tz=None) -> dict:
    """Title + rows describing a safety alert (used by the alert email and the sync summary)."""
    from .alerts import safety_severity

    tz = tz or _tz(alert.area if alert.area_id else None, alert.organisation)
    ranger = alert.ranger
    rows = [
        ("Ranger", ranger.full_name),
        ("Employee ID", ranger.employee_id or DASH),
        ("Kind", alert.get_kind_display()),
        ("Status", alert.status.capitalize()),
        ("Raised at", fmt_local(alert.started_at, tz)),
        ("Location", _where(alert.lat, alert.lon, alert.accuracy_m)),
        ("Area", alert.area.name if alert.area_id else DASH),
        ("Cell", _alert_cell_label(alert) or DASH),
    ]
    rows.append(("Battery", f"{alert.battery_pct}%" if alert.battery_pct is not None else "unknown"))
    if alert.signal_level is not None:
        rows.append(("Signal", f"{alert.signal_level} ({_SIGNAL.get(alert.signal_level, 'n/a')})"))
    if alert.kind == SafetyAlert.HWC:
        rows += _hwc_rows(alert.details)
    return {"title": hwc.alert_title(alert.kind, alert.details), "rows": rows,
            "map_url": map_url(alert.lat, alert.lon), "critical": safety_severity(alert) == "critical"}


def alert_email(alert: SafetyAlert, title: str, headline: str):
    from notify.mail import render_email

    item = safety_alert_item(alert)
    return render_email("alert", title, {"title": title, "headline": headline, "rows": item["rows"],
                                         "map_url": item["map_url"], "critical": item["critical"]})


def observation_item(obs: Observation, tz=None) -> dict:
    tz = tz or _tz(obs.area, obs.organisation)
    rows = [("Category", obs.category.capitalize())]
    if obs.subtype:
        rows.append(("Type", obs.subtype.replace("_", " ")))
    if obs.species_name:
        rows.append(("Species", obs.species_name))
    if obs.count is not None:
        rows.append(("Count", str(obs.count)))
    if obs.sex:
        sex = obs.sex
        if obs.male_count is not None or obs.female_count is not None:
            sex += f" ({obs.male_count or 0} male, {obs.female_count or 0} female)"
        rows.append(("Sex", sex))
    if obs.severity:
        rows.append(("Severity", obs.severity))
    rows += [
        ("Cell", obs.cell.label if obs.cell_id else "outside grid"),
        ("Area", obs.area.name),
        ("Recorded at", fmt_local(obs.recorded_at, tz)),
        ("Location", _where(obs.lat, obs.lon, obs.accuracy_m)),
    ]
    if obs.notes:
        rows.append(("Notes", obs.notes))
    title = " · ".join(x for x in [obs.category.capitalize(), (obs.subtype or "").replace("_", " "),
                                   obs.species_name or ""] if x)
    return {"title": title, "rows": rows, "map_url": map_url(obs.lat, obs.lon),
            "critical": obs.severity == "critical"}


def observation_alert_email(obs: Observation, title: str, headline: str):
    from notify.mail import render_email

    item = observation_item(obs)
    rows = [("Ranger", obs.observer.full_name), ("Employee ID", obs.observer.employee_id or DASH)] + item["rows"]
    return render_email("alert", title, {"title": title, "headline": headline, "rows": rows,
                                         "map_url": item["map_url"], "critical": item["critical"]})


def patrol_item(patrol: Patrol, started: bool, ended: bool, tz=None) -> dict:
    tz = tz or _tz(patrol.area, patrol.organisation)
    state = "started and ended" if (started and ended) else "ended" if ended else "started"
    rows = [
        ("Type", patrol.get_patrol_type_display()),
        ("Team", patrol.team.name if patrol.team_id else DASH),
        ("Base", patrol.apu_base.name if patrol.apu_base_id else DASH),
        ("Area", patrol.area.name),
        ("Start", fmt_local(patrol.started_at, tz)),
    ]
    if patrol.ended_at or ended:
        rows.append(("End", fmt_local(patrol.ended_at, tz)))
    rows += [("Duration", fmt_duration(patrol.duration_s)), ("Distance", fmt_distance(patrol.distance_m))]
    if patrol.notes:
        rows.append(("Notes / debrief", patrol.notes))
    rows.append(("Debrief audio", "yes" if patrol.debrief_audio else "no"))
    return {"title": f"{patrol.get_patrol_type_display()} patrol {state}", "rows": rows, "map_url": None,
            "critical": False}


# --- sync summary ----------------------------------------------------------------------------------

@dataclass
class SyncReport:
    """Records a push *newly* created or changed in a reportable way (ids only)."""

    patrols: dict = field(default_factory=dict)  # client_uuid -> {"started": bool, "ended": bool}
    observations: list = field(default_factory=list)
    alerts: list = field(default_factory=list)  # new safety alerts
    hwc_details: list = field(default_factory=list)  # alerts whose HWC details were logged for the first time

    def patrol(self, pk, *, started=False, ended=False) -> None:
        entry = self.patrols.setdefault(pk, {"started": False, "ended": False})
        entry["started"] |= started
        entry["ended"] |= ended

    def __len__(self) -> int:
        details_only = [a for a in self.hwc_details if a not in self.alerts]
        return len(self.patrols) + len(self.observations) + len(self.alerts) + len(details_only)


def build_sync_summary(user, report: SyncReport, device_id: str | None):
    from notify.mail import render_email

    org = user.organisation
    alert_ids = list(dict.fromkeys([*report.alerts, *report.hwc_details]))
    alerts = SafetyAlert.objects.filter(organisation=org, pk__in=alert_ids).select_related("ranger", "area")
    patrols = Patrol.objects.filter(organisation=org, pk__in=list(report.patrols)).select_related(
        "team", "apu_base", "area")
    observations = Observation.objects.filter(organisation=org, pk__in=report.observations).select_related(
        "area", "cell")

    tz_cache: dict = {}

    def tz_for(area):
        key = area.pk if area is not None else None
        if key not in tz_cache:
            tz_cache[key] = _tz(area, org)
        return tz_cache[key]

    alert_items = []
    for a in sorted(alerts, key=lambda a: a.started_at):
        item = safety_alert_item(a, tz_for(a.area if a.area_id else None))
        if a.pk not in report.alerts:
            item["title"] += " · details logged"
        item["critical"] = True  # every safety alert is highlighted in a summary
        alert_items.append(item)
    patrol_items = [patrol_item(p, report.patrols[p.pk]["started"], report.patrols[p.pk]["ended"], tz_for(p.area))
                    for p in sorted(patrols, key=lambda p: p.started_at)]
    obs_items = [observation_item(o, tz_for(o.area)) for o in sorted(observations, key=lambda o: o.recorded_at)]

    count = len(alert_items) + len(patrol_items) + len(obs_items)
    if not count:
        return None
    urgent = bool(alert_items) or any(i["critical"] for i in obs_items)
    area_names = sorted({p.area.name for p in patrols} | {o.area.name for o in observations}
                        | {a.area.name for a in alerts if a.area_id})
    first_area = next(iter([*patrols, *observations]), None)
    tz = tz_for(first_area.area) if first_area is not None else tz_for(None)
    meta = [("Ranger", user.full_name), ("Employee ID", user.employee_id or DASH),
            ("Synced at", fmt_local(timezone.now(), tz)), ("Device", device_id or DASH),
            ("Area(s)", ", ".join(area_names) or DASH)]
    subject = f"{'⚠ ' if urgent else ''}PATROLIQ sync · {user.full_name} · {count} new record{'s' if count != 1 else ''}"
    return render_email("sync_summary", subject, {
        "ranger_name": user.full_name, "count": count, "meta": meta, "alerts": alert_items,
        "patrols": patrol_items, "observations": obs_items})


def queue_sync_summary(request, report: SyncReport) -> None:
    """Queue the sync summary email when the call produced reportable events. Never raises."""
    if not len(report) or not getattr(settings, "EMAIL_SYNC_SUMMARIES", True):
        return
    try:
        from notify.services import email_managers

        user = request.user
        device_id = getattr(getattr(request, "auth", None), "device_id", "") or None
        content = build_sync_summary(user, report, device_id)
        if content is not None:
            email_managers(user.organisation_id, content, {"type": "sync_summary", "ranger_id": str(user.pk)})
    except Exception:  # noqa: BLE001 — a summary email must never fail the ranger's sync
        logger.exception("could not queue the sync summary email")
