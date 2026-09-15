"""
Synchronous report generation (spec §7 "Reports").

    collect()      -> ReportData (metrics, tables, features) from the organisation's data
    render_pdf()   -> A4 PDF (reportlab: header, key metrics, charts, tables, restricted footer, page numbers)
    render_csv()   -> the report's main table as flat CSV rows
    render_geojson -> FeatureCollection of observations / incidents / tracks / cells

Anonymised reports (always for researcher/viewer): no ranger identity anywhere (names, ids, employee
ids; ranger tables use stable pseudonyms "Ranger 01"), and no coordinates — locations are given as the
GRTS cell label; GeoJSON features use the cell polygon (the same geometry ``areas/{id}/cells/`` exposes)
or ``null`` outside the grid, and patrol tracks are omitted.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable
from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.core.files.base import ContentFile
from django.db.models import Avg, Count, Max, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from areas.models import Area, GrtsCell, RiskScore, Sector
from audit.utils import audit
from field.alerts import threat_alert_q
from field.models import Observation, Patrol, Species, TrackPoint

from . import services
from .models import CONTENT_TYPES, Report

logger = logging.getLogger("patroliq.reports")

TYPE_LABELS = {
    "patrol_summary": "Patrol summary",
    "incident_report": "Incident report",
    "wildlife_census": "Wildlife census",
    "threat_intelligence": "Threat intelligence",
    "grts_survey": "GRTS survey coverage",
    "ranger_performance": "Ranger performance",
    "donor_report": "Donor report",
    "zpwma_compliance": "ZimParks (ZPWMA) compliance",
}
PDF_ROW_LIMIT = 300
MAX_RANGE_DAYS = 366


@dataclass
class Table:
    title: str
    columns: list[str]
    rows: list[list]
    widths: list[float] | None = None  # relative column weights for the PDF


@dataclass
class ReportData:
    title: str
    period: str
    filters: list[tuple[str, str]]
    metrics: list[tuple[str, str]]
    summary: dict
    tables: list[Table]
    main: Table
    features: Callable[[], list[dict]] = field(default=lambda: [])  # built lazily (GeoJSON only)
    report_type: str = ""


# --- helpers -------------------------------------------------------------------------------------------

def _local(dt, tz, fmt="%Y-%m-%d %H:%M"):
    return dt.astimezone(tz).strftime(fmt) if dt else ""


def _pseudonyms(user_ids) -> dict:
    """Stable, non-reversible pseudonyms: ordered by a hash of the id, not by name."""
    ordered = sorted({str(u) for u in user_ids}, key=lambda s: hashlib.sha256(s.encode()).hexdigest())
    return {uid: f"Ranger {i:02d}" for i, uid in enumerate(ordered, start=1)}


def _week_label(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _is_incident(o: Observation) -> bool:
    return o.category in ("threat", "carcass")


def _months(start: date, end: date) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# --- data collection -------------------------------------------------------------------------------------

def collect(org, params: dict, anonymised: bool) -> ReportData:
    rtype = params["type"]
    date_from, date_to = date.fromisoformat(params["date_from"]), date.fromisoformat(params["date_to"])
    area = Area.objects.for_org(org).filter(pk=params["area_id"]).first() if params.get("area_id") else None
    sector = Sector.objects.for_org(org).filter(pk=params["sector_id"]).first() if params.get("sector_id") else None
    if sector is not None and area is None:
        area = sector.area
    ranger_id = uuid.UUID(str(params["ranger_id"])) if params.get("ranger_id") else None
    species = Species.objects.filter(pk=params["species_id"]).first() if params.get("species_id") else None
    tz = services.area_tz(area, org)
    start, end = services.day_bounds(date_from, tz)[0], services.day_bounds(date_to, tz)[1]

    patrols = Patrol.objects.for_org(org).filter(started_at__gte=start, started_at__lt=end).select_related("ranger", "team")
    obs = (Observation.objects.for_org(org).filter(recorded_at__gte=start, recorded_at__lt=end)
           .select_related("observer", "cell", "cell__sector", "species"))
    if area:
        patrols, obs = patrols.filter(area=area), obs.filter(area=area)
    if sector:
        patrols = patrols.filter(pk__in=TrackPoint.objects.filter(organisation_id=org.pk, cell__sector=sector)
                                 .values("patrol_id"))
        obs = obs.filter(cell__sector=sector)
    if ranger_id:
        patrols, obs = patrols.filter(ranger_id=ranger_id), obs.filter(observer_id=ranger_id)
    if species:
        obs = obs.filter(species=species)
    patrol_pks = patrols.values("pk")
    patrols = list(patrols.order_by("started_at"))
    obs = list(obs.order_by("recorded_at"))

    person_ids = {p.ranger_id for p in patrols} | {o.observer_id for o in obs}
    pseudo = _pseudonyms(person_ids) if anonymised else {}
    names = {p.ranger_id: p.ranger.full_name for p in patrols} | {o.observer_id: o.observer.full_name for o in obs}

    def who(uid):
        return pseudo.get(str(uid), "Ranger") if anonymised else names.get(uid, "")

    obs_per_patrol = Counter(o.patrol_id for o in obs if o.patrol_id)
    incidents = [o for o in obs if _is_incident(o)]
    high_incidents = [o for o in incidents if o.severity in ("high", "critical")]
    wildlife = [o for o in obs if o.category == "wildlife"]

    # Cells in scope and cells visited in the period.
    cells_qs = GrtsCell.objects.for_org(org)
    if sector:
        cells_qs = cells_qs.filter(sector=sector)
    elif area:
        cells_qs = cells_qs.filter(area=area)
    else:
        cells_qs = cells_qs.filter(area__status="active")
    cells = list(cells_qs.select_related("sector").order_by("area__name", "grts_order"))
    cell_ids = {c.pk for c in cells}
    patrol_ids = patrol_pks
    tp_qs = TrackPoint.objects.filter(organisation_id=org.pk, patrol_id__in=patrol_pks, cell__isnull=False)
    tp_days: dict = defaultdict(set)
    for cid, day in tp_qs.annotate(d=TruncDate("recorded_at", tzinfo=tz)).values_list("cell_id", "d").distinct():
        tp_days[cid].add(day)
    ranger_cells: dict = defaultdict(set)
    for cid, rid in tp_qs.values_list("cell_id", "patrol__ranger_id").distinct():
        ranger_cells[rid].add(cid)
    for o in obs:
        if o.cell_id:
            tp_days[o.cell_id].add(o.recorded_at.astimezone(tz).date())
            ranger_cells[o.observer_id].add(o.cell_id)
    visited = {cid for cid in tp_days if cid in cell_ids}
    coverage_pct = round(len(visited) / len(cells), 4) if cells else 0.0

    distance_km = round(sum(p.distance_m or 0 for p in patrols) / 1000, 1)
    hours = round(sum(p.duration_s or 0 for p in patrols) / 3600, 1)

    weeks: dict = {}
    d = date_from
    while d <= date_to:
        weeks.setdefault(_week_label(d), 0)
        d += timedelta(days=1)
    for o in obs:
        weeks[_week_label(o.recorded_at.astimezone(tz).date())] = weeks.get(_week_label(o.recorded_at.astimezone(tz).date()), 0) + 1

    species_counts: Counter = Counter()
    species_stats: dict = defaultdict(lambda: {"sightings": 0, "individuals": 0, "males": 0, "females": 0, "juveniles": 0})
    for o in wildlife:
        name = o.species.common_name if o.species_id else (o.species_name or "Unidentified")
        n = o.count or 1
        species_counts[name] += n
        st = species_stats[name]
        st["sightings"] += 1
        st["individuals"] += n
        if o.sex == "male":
            st["males"] += n
        elif o.sex == "female":
            st["females"] += n
        elif o.sex == "mixed":
            st["males"] += o.male_count or 0
            st["females"] += o.female_count or 0
        if o.age_class == "juvenile":
            st["juveniles"] += n

    risk_qs = RiskScore.objects.for_org(org).filter(date__gte=date_from, date__lte=date_to)
    if sector:
        risk_qs = risk_qs.filter(cell__sector=sector)
    elif area:
        risk_qs = risk_qs.filter(area=area)
    risk_trend = [{"date": r["date"].isoformat(), "mean_score": round(r["m"], 2)}
                  for r in risk_qs.values("date").annotate(m=Avg("score")).order_by("date")]

    summary = {
        "patrols": len(patrols), "observations": len(obs), "high_severity_incidents": len(high_incidents),
        "incidents": len(incidents), "wildlife_sightings": len(wildlife), "grts_coverage_pct": coverage_pct,
        "cells_visited": len(visited), "cells_total": len(cells), "distance_km": distance_km, "patrol_hours": hours,
        "rangers": len(person_ids),
        "by_week": [{"week": w, "observations": n} for w, n in sorted(weeks.items())],
        "species": [{"name": n, "count": c} for n, c in species_counts.most_common(15)],
        "incidents_by_type": [{"type": t, "count": c} for t, c in
                              Counter((o.subtype or o.category) for o in incidents).most_common()],
        "risk_trend": risk_trend,
    }

    # --- tables ---------------------------------------------------------------------------------------
    def patrol_table():
        cols = ["Date", "Ranger", "Team", "Type", "Status", "Start", "Hours", "Distance km", "Observations"]
        rows = [[_local(p.started_at, tz, "%Y-%m-%d"), who(p.ranger_id), "" if anonymised else (p.team.name if p.team_id else ""),
                 p.patrol_type, p.status, _local(p.started_at, tz, "%H:%M"), round((p.duration_s or 0) / 3600, 1),
                 round((p.distance_m or 0) / 1000, 2), obs_per_patrol.get(p.pk, 0)] for p in patrols]
        if anonymised:
            cols.remove("Team")
            rows = [r[:2] + r[3:] for r in rows]
        return Table("Patrols", cols, rows)

    def incident_table(items=None, title="Incidents"):
        items = incidents if items is None else items
        if anonymised:
            # Free-text notes are omitted: they can name people or places.
            cols = ["Recorded", "Category", "Type", "Severity", "Cell", "Acknowledged"]
            rows = [[_local(o.recorded_at, tz), o.category, o.subtype or "", o.severity or "",
                     o.cell.label if o.cell_id else "outside grid", "yes" if o.acknowledged_at else "no"]
                    for o in items]
            return Table(title, cols, rows)
        cols = ["Recorded", "Category", "Type", "Severity", "Cell", "Lat", "Lon", "Reporter", "Ack.", "Notes"]
        rows = [[_local(o.recorded_at, tz), o.category, o.subtype or "", o.severity or "",
                 o.cell.label if o.cell_id else "outside grid", round(o.lat, 5), round(o.lon, 5), who(o.observer_id),
                 "yes" if o.acknowledged_at else "no", o.notes[:200]] for o in items]
        return Table(title, cols, rows, [1.3, 0.9, 1, 0.8, 0.9, 0.9, 0.9, 1.2, 0.5, 2.2])

    def wildlife_table():
        cols = ["Recorded", "Species", "Count", "Sex", "Males", "Females", "Age class", "Behaviour", "Cell"]
        if not anonymised:
            cols += ["Lat", "Lon", "Observer"]
        rows = []
        for o in wildlife:
            row = [_local(o.recorded_at, tz), o.species.common_name if o.species_id else (o.species_name or ""), o.count or "",
                   o.sex or "", o.male_count if o.male_count is not None else "", o.female_count if o.female_count is not None else "",
                   o.age_class or "", o.behaviour or "", o.cell.label if o.cell_id else "outside grid"]
            if not anonymised:
                row += [round(o.lat, 5), round(o.lon, 5), who(o.observer_id)]
            rows.append(row)
        return Table("Wildlife observations", cols, rows)

    def species_table():
        rows = [[name, st["sightings"], st["individuals"], st["males"], st["females"], st["juveniles"]]
                for name, st in sorted(species_stats.items(), key=lambda kv: -kv[1]["individuals"])]
        return Table("Species", ["Species", "Sightings", "Individuals", "Males", "Females", "Juveniles"], rows,
                     [2.5, 1, 1, 1, 1, 1])

    def cell_table():
        obs_by_cell = Counter(o.cell_id for o in obs if o.cell_id)
        rows = []
        for c in cells:
            days = sorted(tp_days.get(c.pk, ()))
            rows.append([c.label, c.sector.name if c.sector_id else "", len(days), obs_by_cell.get(c.pk, 0),
                         days[-1].isoformat() if days else "never in period"])
        return Table("GRTS cells", ["Cell", "Sector", "Visit days", "Observations", "Last visit"], rows, [1, 2, 1, 1, 1.4])

    def ranger_table():
        stats: dict = defaultdict(lambda: {"patrols": 0, "km": 0.0, "hours": 0.0, "obs": 0, "inc": 0, "team": ""})
        for p in patrols:
            s = stats[p.ranger_id]
            s["patrols"] += 1
            s["km"] += (p.distance_m or 0) / 1000
            s["hours"] += (p.duration_s or 0) / 3600
            s["team"] = p.team.name if p.team_id else s["team"]
        for o in obs:
            s = stats[o.observer_id]
            s["obs"] += 1
            s["inc"] += _is_incident(o)
        rows = [[who(uid), "" if anonymised else s["team"], s["patrols"], round(s["km"], 1), round(s["hours"], 1),
                 s["obs"], s["inc"], len(ranger_cells.get(uid, ()))] for uid, s in stats.items()]
        rows.sort(key=lambda r: (-r[3], r[0]))
        cols = ["Ranger", "Team", "Patrols", "Distance km", "Hours", "Observations", "Incidents", "Cells visited"]
        if anonymised:
            cols.remove("Team")
            rows = [[r[0]] + r[2:] for r in rows]
        return Table("Ranger performance", cols, rows)

    def monthly_table():
        months = {m: {"patrols": 0, "days": set(), "km": 0.0, "obs": 0, "inc": 0, "ack": 0, "alerts": 0}
                  for m in _months(date_from, date_to)}
        for p in patrols:
            m = months.setdefault(_local(p.started_at, tz, "%Y-%m"), {"patrols": 0, "days": set(), "km": 0.0, "obs": 0,
                                                                        "inc": 0, "ack": 0, "alerts": 0})
            m["patrols"] += 1
            m["days"].add((p.ranger_id, p.started_at.astimezone(tz).date()))
            m["km"] += (p.distance_m or 0) / 1000
        alert_ids = set(Observation.objects.for_org(org).filter(threat_alert_q(), pk__in=[o.pk for o in obs])
                        .values_list("pk", flat=True))
        for o in obs:
            m = months.get(_local(o.recorded_at, tz, "%Y-%m"))
            if m is None:
                continue
            m["obs"] += 1
            m["inc"] += _is_incident(o)
            if o.pk in alert_ids:
                m["alerts"] += 1
                m["ack"] += bool(o.acknowledged_at)
        rows = [[k, v["patrols"], len(v["days"]), round(v["km"], 1), v["obs"], v["inc"], f"{v['ack']}/{v['alerts']}"]
                for k, v in sorted(months.items())]
        return Table("Monthly statistics", ["Month", "Patrols", "Ranger-days", "Distance km", "Observations",
                                             "Incidents", "Alerts acknowledged"], rows)

    def risk_table():
        rows = [[r["cell__label"], round(r["m"], 2), round(r["x"], 2), r["hc"]] for r in
                risk_qs.values("cell__label").annotate(m=Avg("score"), x=Max("score"),
                                                        hc=Count("pk", filter=Q(level__in=["high", "critical"])))
                .order_by("-m")[:25]]
        return Table("Highest-risk cells", ["Cell", "Mean score", "Max score", "Days high/critical"], rows)

    def incident_types_table():
        return Table("Incidents by type", ["Type", "Count"],
                     [[t["type"], t["count"]] for t in summary["incidents_by_type"]], [3, 1])

    # --- features ----------------------------------------------------------------------------------
    dt_iso = lambda v: v.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if v else None  # noqa: E731

    def obs_features(items):
        feats = []
        for o in items:
            props = {"client_uuid": str(o.pk), "kind": "observation", "category": o.category, "subtype": o.subtype,
                     "species_name": o.species.common_name if o.species_id else o.species_name, "count": o.count,
                     "sex": o.sex, "age_class": o.age_class, "severity": o.severity, "recorded_at": dt_iso(o.recorded_at),
                     "cell_label": o.cell.label if o.cell_id else None, "area_id": str(o.area_id)}
            if anonymised:
                geom = o.cell.geometry if o.cell_id else None
            else:
                geom = {"type": "Point", "coordinates": [round(o.lon, 7), round(o.lat, 7)]}
                props.update(observer_id=str(o.observer_id), observer_name=who(o.observer_id), notes=o.notes,
                             accuracy_m=o.accuracy_m, patrol_client_uuid=str(o.patrol_id) if o.patrol_id else None)
            feats.append({"type": "Feature", "id": str(o.pk), "geometry": geom, "properties": props})
        return feats

    def track_features():
        if anonymised or not patrols:
            return []
        coords: dict = defaultdict(list)
        for pid, lon, lat in (TrackPoint.objects.filter(organisation_id=org.pk, patrol_id__in=patrol_ids)
                              .order_by("patrol_id", "recorded_at").values_list("patrol_id", "lon", "lat")):
            coords[pid].append([round(lon, 7), round(lat, 7)])
        return [{"type": "Feature", "id": str(p.pk),
                 "geometry": {"type": "LineString", "coordinates": coords[p.pk]} if len(coords[p.pk]) >= 2 else None,
                 "properties": {"kind": "track", "client_uuid": str(p.pk), "ranger_id": str(p.ranger_id),
                                "ranger_name": who(p.ranger_id), "started_at": dt_iso(p.started_at),
                                "ended_at": dt_iso(p.ended_at), "distance_m": int(round(p.distance_m or 0)),
                                "status": p.status, "patrol_type": p.patrol_type}} for p in patrols]

    def cell_features():
        obs_by_cell = Counter(o.cell_id for o in obs if o.cell_id)
        return [{"type": "Feature", "id": str(c.pk), "geometry": c.geometry,
                 "properties": {"kind": "cell", "label": c.label, "grts_order": c.grts_order,
                                "sector_id": str(c.sector_id) if c.sector_id else None,
                                "visit_days": len(tp_days.get(c.pk, ())), "observations": obs_by_cell.get(c.pk, 0)}}
                for c in cells]

    if rtype == "patrol_summary":
        tables, main, features = [patrol_table(), species_table()], patrol_table(), lambda: track_features() + obs_features(obs)
    elif rtype == "incident_report":
        t = incident_table()
        tables, main, features = [incident_types_table(), t], t, lambda: obs_features(incidents)
    elif rtype == "wildlife_census":
        tables, main, features = [species_table(), wildlife_table()], wildlife_table(), lambda: obs_features(wildlife)
    elif rtype == "threat_intelligence":
        t = incident_table()
        tables, main, features = [incident_types_table(), risk_table(), t], t, lambda: obs_features(incidents)
    elif rtype == "grts_survey":
        t = cell_table()
        tables, main, features = [t], t, lambda: cell_features()
    elif rtype == "ranger_performance":
        t = ranger_table()
        tables, main, features = [t], t, lambda: track_features() + obs_features(obs)
    elif rtype == "donor_report":
        t = monthly_table()
        tables, main, features = [t, species_table(), incident_types_table()], t, lambda: obs_features(obs)
    else:  # zpwma_compliance
        t = monthly_table()
        tables, main, features = [t, incident_table(high_incidents, "High-severity incidents")], t, lambda: obs_features(incidents)

    filters = []
    filters.append(("Area", area.name if area else "All active areas"))
    if sector:
        filters.append(("Sector", sector.name))
    if ranger_id:
        filters.append(("Ranger", names.get(ranger_id) or str(ranger_id)))
    if species:
        filters.append(("Species", species.common_name))
    if anonymised:
        filters.append(("Anonymised", "yes (no ranger identity, locations as GRTS cells)"))

    metrics = [
        ("Patrols", str(summary["patrols"])), ("Patrol distance", f"{distance_km:,.1f} km"),
        ("Patrol hours", f"{hours:,.1f} h"), ("Observations", str(summary["observations"])),
        ("Incidents (high/critical)", f"{len(incidents)} ({len(high_incidents)})"),
        ("Wildlife sightings", str(len(wildlife))),
        ("GRTS cells visited", f"{len(visited)} of {len(cells)} ({coverage_pct * 100:.0f}%)"),
    ]
    title = TYPE_LABELS[rtype] + (f": {area.name}" if area else "")
    period = f"{date_from:%d %b %Y} to {date_to:%d %b %Y} ({tz.key})"
    return ReportData(title, period, filters, metrics, summary, tables, main, features, rtype)


# --- renderers -------------------------------------------------------------------------------------------

def render_csv(data: ReportData) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(data.main.columns)
    for row in data.main.rows:
        w.writerow(["" if v is None else v for v in row])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM: opens cleanly in Excel


def render_geojson(data: ReportData) -> bytes:
    body = {"type": "FeatureCollection", "name": data.title,
            "properties": {"title": data.title, "period": data.period, "summary": {
                k: v for k, v in data.summary.items() if not isinstance(v, list)}},
            "features": data.features()}
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def render_pdf(data: ReportData, organisation_name: str, generated_by: str) -> bytes:
    from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
    from reportlab.graphics.charts.lineplots import LinePlot
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table as RLTable, TableStyle
    from xml.sax.saxutils import escape

    green, dark, grey = colors.HexColor("#1F5A3A"), colors.HexColor("#1B1F1D"), colors.HexColor("#6B736E")
    page_w, page_h = A4
    margin = 16 * mm
    usable = page_w - 2 * margin
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], alignment=TA_LEFT, fontSize=17, leading=21, textColor=dark,
                        spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11.5, leading=14, textColor=green, spaceBefore=8,
                        spaceAfter=4, keepWithNext=1)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=8.5, leading=11, textColor=dark)
    small = ParagraphStyle("small", parent=body, fontSize=7.2, leading=8.8)
    muted = ParagraphStyle("muted", parent=body, textColor=grey)

    generated = timezone.now().strftime("%Y-%m-%d %H:%M UTC")

    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved = []

        def showPage(self):
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved)
            for state in self._saved:
                self.__dict__.update(state)
                self._decorate(total)
                super().showPage()
            super().save()

        def _decorate(self, total):
            self.saveState()
            self.setFillColor(green)
            self.rect(0, page_h - 14 * mm, page_w, 14 * mm, stroke=0, fill=1)
            self.setFillColor(colors.white)
            self.setFont("Helvetica-Bold", 13)
            self.drawString(margin, page_h - 9.3 * mm, "PATROLIQ")
            self.setFont("Helvetica", 9)
            self.drawRightString(page_w - margin, page_h - 9.3 * mm, organisation_name)
            self.setStrokeColor(colors.HexColor("#C9D1CC"))
            self.line(margin, 12 * mm, page_w - margin, 12 * mm)
            self.setFillColor(grey)
            self.setFont("Helvetica", 7.5)
            self.drawString(margin, 8 * mm, f"Restricted \u00b7 {organisation_name} management use")
            self.drawCentredString(page_w / 2, 8 * mm, f"Generated {generated}")
            self.drawRightString(page_w - margin, 8 * mm, f"Page {self._pageNumber} of {total}")
            self.restoreState()

    def cell_text(v):
        if v is None:
            return ""
        if isinstance(v, float):
            return f"{v:,.5f}".rstrip("0").rstrip(".")
        text = str(v)
        return text.replace("_", " ") if text.islower() and " " not in text else text  # snake_case codes read better

    def make_table(t: Table):
        if not t.rows:
            return [Paragraph(escape(t.title), h2), Paragraph("No records for this period and filter.", muted)]
        rows = t.rows[:PDF_ROW_LIMIT]
        weights = t.widths or [max(len(c), *(min(len(cell_text(r[i])), 40) for r in rows[:50])) + 2
                               for i, c in enumerate(t.columns)]
        total = sum(weights)
        widths = [usable * w / total for w in weights]
        data_rows = [[Paragraph(f"<b>{escape(c)}</b>", small) for c in t.columns]]
        data_rows += [[Paragraph(escape(cell_text(v)), small) for v in r] for r in rows]
        tbl = RLTable(data_rows, colWidths=widths, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E3EDE6")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F8F7")]),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, green),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ]))
        out = [Paragraph(escape(t.title), h2), tbl]
        if len(t.rows) > PDF_ROW_LIMIT:
            out.append(Paragraph(f"Showing the first {PDF_ROW_LIMIT} of {len(t.rows)} rows; generate the CSV for all rows.", muted))
        return out

    def chart_title(d, text):
        d.add(String(0, d.height - 10, text, fontName="Helvetica-Bold", fontSize=8.5, fillColor=dark))

    def week_chart():
        weeks = data.summary["by_week"]
        if not weeks or not any(w["observations"] for w in weeks):
            return None
        d = Drawing(usable / 2 - 4, 150)
        chart_title(d, "Observations per week")
        bc = VerticalBarChart()
        bc.x, bc.y, bc.width, bc.height = 28, 30, d.width - 38, 95
        bc.data = [[w["observations"] for w in weeks]]
        bc.categoryAxis.categoryNames = [w["week"][-3:] for w in weeks]
        bc.categoryAxis.labels.fontSize = 6
        bc.categoryAxis.labels.fontName = "Helvetica"
        bc.categoryAxis.labels.angle = 45 if len(weeks) > 8 else 0
        bc.categoryAxis.labels.dy = -6 if len(weeks) > 8 else -2
        bc.valueAxis.valueMin = 0
        bc.valueAxis.labels.fontSize = 6.5
        bc.valueAxis.labels.fontName = "Helvetica"
        bc.bars[0].fillColor = green
        bc.bars[0].strokeColor = None
        d.add(bc)
        return d

    def risk_chart():
        trend = data.summary["risk_trend"]
        if len(trend) < 2:
            return None
        d = Drawing(usable / 2 - 4, 150)
        chart_title(d, "Mean cell risk score (0-10)")
        lp = LinePlot()
        lp.x, lp.y, lp.width, lp.height = 28, 30, d.width - 38, 95
        lp.data = [[(i, t["mean_score"]) for i, t in enumerate(trend)]]
        lp.lines[0].strokeColor = colors.HexColor("#B4532A")
        lp.lines[0].strokeWidth = 1.5
        lp.xValueAxis.valueMin, lp.xValueAxis.valueMax = 0, len(trend) - 1
        step = max(1, len(trend) // 6)
        lp.xValueAxis.valueSteps = list(range(0, len(trend), step))
        lp.xValueAxis.labelTextFormat = lambda v: trend[int(v)]["date"][5:] if 0 <= int(v) < len(trend) else ""
        lp.xValueAxis.labels.fontSize = 6
        lp.xValueAxis.labels.fontName = "Helvetica"
        lo = min(t["mean_score"] for t in trend)
        hi = max(t["mean_score"] for t in trend)
        lp.yValueAxis.valueMin = max(0, int(lo) - 1)
        lp.yValueAxis.valueMax = min(10, int(hi) + 1)
        lp.yValueAxis.labels.fontSize = 6.5
        lp.yValueAxis.labels.fontName = "Helvetica"
        d.add(lp)
        return d

    def species_chart():
        sp = data.summary["species"][:8]
        if not sp:
            return None
        d = Drawing(usable, 20 + 14 * len(sp) + 20)
        chart_title(d, "Individuals counted by species")
        bc = HorizontalBarChart()
        bc.x, bc.y, bc.width, bc.height = 110, 8, usable - 130, 14 * len(sp)
        bc.data = [[s["count"] for s in reversed(sp)]]
        bc.categoryAxis.categoryNames = [s["name"] for s in reversed(sp)]
        bc.categoryAxis.labels.fontSize = 7
        bc.categoryAxis.labels.fontName = "Helvetica"
        bc.valueAxis.valueMin = 0
        bc.valueAxis.labels.fontSize = 6.5
        bc.valueAxis.labels.fontName = "Helvetica"
        bc.bars[0].fillColor = colors.HexColor("#4E7F5E")
        bc.bars[0].strokeColor = None
        d.add(bc)
        return d

    story = [Spacer(1, 2 * mm), Paragraph(escape(data.title), h1), Paragraph(f"Period: {escape(data.period)}", body)]
    story.append(Paragraph(" \u00b7 ".join(f"<b>{escape(k)}:</b> {escape(v)}" for k, v in data.filters), body))
    story.append(Paragraph(f"Prepared by {escape(generated_by)}", muted))
    story.append(Paragraph("Key metrics", h2))
    pairs = data.metrics
    grid = []
    for i in range(0, len(pairs), 2):
        row = []
        for k, v in pairs[i:i + 2]:
            row += [Paragraph(escape(k), small), Paragraph(f"<b>{escape(v)}</b>", body)]
        while len(row) < 4:
            row.append("")
        grid.append(row)
    mt = RLTable(grid, colWidths=[usable * 0.2, usable * 0.3, usable * 0.2, usable * 0.3])
    mt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#C9D1CC")),
                            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E1E6E3")),
                            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F6F8F7")),
                            ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F6F8F7")),
                            ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story.append(mt)

    charts = [c for c in (week_chart(), risk_chart()) if c is not None]
    if charts:
        story.append(Spacer(1, 4 * mm))
        row = charts + [""] * (2 - len(charts))
        ct = RLTable([row], colWidths=[usable / 2, usable / 2])
        ct.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story.append(ct)
    sc = species_chart() if data.report_type in ("patrol_summary", "wildlife_census", "donor_report") else None
    if sc is not None:
        story.append(KeepTogether([Spacer(1, 2 * mm), sc]))
    for t in data.tables:
        story.extend(make_table(t))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=margin, rightMargin=margin, topMargin=20 * mm,
                            bottomMargin=18 * mm, title=data.title, author="PATROLIQ", subject=organisation_name)
    doc.build(story, canvasmaker=NumberedCanvas)
    return buf.getvalue()


# --- orchestration ----------------------------------------------------------------------------------------

def build_file(report: Report, organisation) -> bytes:
    data = collect(organisation, report.params, report.anonymised)
    report.summary = data.summary
    report.title = data.title[:255]
    if report.format == "pdf":
        creator = report.created_by.full_name if report.created_by_id else "PATROLIQ"
        return render_pdf(data, organisation.name, creator)
    if report.format == "csv":
        return render_csv(data)
    return render_geojson(data)


def generate(organisation, user, params: dict, anonymised: bool, request=None, report_id=None) -> Report:
    report = Report(organisation=organisation, type=params["type"], format=params["format"], params=params,
                    anonymised=anonymised, created_by=user, title=TYPE_LABELS[params["type"]])
    if report_id is not None:
        report.id = report_id
    try:
        content = build_file(report, organisation)
        report.status = "ready"
        report.size_bytes = len(content)
        report.file.save(f"{report.id}", ContentFile(content), save=False)
    except Exception as exc:  # noqa: BLE001 — a failed report is recorded, not a 500
        logger.exception("report generation failed")
        report.status, report.error, report.size_bytes = "failed", f"{type(exc).__name__}: {exc}"[:2000], 0
    report.save()
    audit(request, "report.generate", target=report, actor=user, detail={
        "type": report.type, "format": report.format, "status": report.status, "anonymised": anonymised,
        "params": params, "size_bytes": report.size_bytes})
    return report


def open_file(report: Report, request=None):
    """Open the stored file, regenerating it (same params) when the storage object is missing."""
    if report.status != "ready":
        return None, False
    try:
        if report.file and report.file.storage.exists(report.file.name):
            return report.file.open("rb"), False
    except Exception:  # noqa: BLE001 — storage outage: fall through to regeneration
        logger.exception("report storage lookup failed")
    content = build_file(report, report.organisation)
    report.file.save(f"{report.id}", ContentFile(content), save=False)
    report.size_bytes = len(content)
    report.save(update_fields=["file", "size_bytes", "summary", "title", "updated_at"])
    return io.BytesIO(content), True


def content_type(report: Report) -> str:
    return CONTENT_TYPES[report.format]
