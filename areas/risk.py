"""
Heuristic risk engine — PRD 6.4 "AI Threat Intelligence, Phase 1: rule-based heuristics".

Per GRTS cell and date a score 0–10 is the weighted mean of factor components in [0, 1]:

  key                 weight  component
  boundary_proximity   0.25   1 - min(distance_to_boundary / 5 km, 1)   (edges are entry points)
  road_proximity       0.15   1 - min(distance_to_road / 3 km, 1)       (only if a roads layer exists)
  water_proximity      0.15   1 - min(distance_to_water / 2 km, 1)      (only if a water layer exists)
  patrol_gap           0.20   min(days since last patrol presence / 30, 1); never patrolled = 1
  incident_history     0.10   kernel density of incidents (90 days) at the cell centroid / area max
  hour_of_day          0.05   1.0 at night (18:00–05:59 local), else 0.3
  moon_phase           0.04   moon illumination fraction (bright nights favour poaching)
  season               0.03   late dry (Aug–Oct) 1.0, early dry (May–Jul) 0.7, wet (Nov–Apr) 0.4
  day_of_week          0.03   Fri–Sun 1.0, else 0.5

Missing optional layers are dropped and the remaining weights renormalised to sum to 1, so the
score stays on the same scale. Each score carries ``factors[{key, label, weight, value}]`` where
``weight`` is the normalised weight and ``value`` the 0–1 component (weight × value summed × 10 =
score); ``label`` is human readable and includes the raw measurement (fully explainable, PRD 6.4).

Levels: < 3 low · < 5.5 medium · < 7.5 high · otherwise critical.
Rules are deliberately simple and tunable; Phase 2 (XGBoost) will replace the weights.
"""
from __future__ import annotations

import math
import zoneinfo
from datetime import date, datetime, time, timedelta, timezone as dt_timezone

from django.db import transaction
from django.db.models import Max

import geo

from .models import Area, FeatureLayer, GrtsCell, RiskScore

BASE_WEIGHTS = {
    "boundary_proximity": 0.25,
    "road_proximity": 0.15,
    "water_proximity": 0.15,
    "patrol_gap": 0.20,
    "incident_history": 0.10,
    "hour_of_day": 0.05,
    "moon_phase": 0.04,
    "season": 0.03,
    "day_of_week": 0.03,
}
SYNODIC_MONTH = 29.530588853
KNOWN_NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=dt_timezone.utc)


def level_for(score: float) -> str:
    if score < 3:
        return "low"
    if score < 5.5:
        return "medium"
    if score < 7.5:
        return "high"
    return "critical"


def moon_illumination(when: datetime) -> float:
    """Fraction of the moon's disc illuminated (0 new – 1 full), simple synodic approximation."""
    days = (when - KNOWN_NEW_MOON).total_seconds() / 86400.0
    phase = (days % SYNODIC_MONTH) / SYNODIC_MONTH
    return (1 - math.cos(2 * math.pi * phase)) / 2


def season_component(month: int) -> tuple[float, str]:
    if month in (8, 9, 10):
        return 1.0, "late dry season"
    if month in (5, 6, 7):
        return 0.7, "early dry season"
    return 0.4, "wet season"


INCIDENT_DENSITY_WINDOW_DAYS = 90
INCIDENT_DENSITY_LABEL = "Incident density (KDE)"


def incident_density_by_cell(area: Area, cells, end) -> dict:
    """
    Kernel density of incidents (source ``incidents``, 90 days) sampled at each cell centroid and
    normalised 0–1 by the area's own maximum (spec v1.5 §C4).

    One kernel surface is built per area/day: the points are gathered once, the bandwidth is chosen
    once, and every cell centroid is evaluated in a single vectorised pass — so this costs the same
    whether the area has 20 cells or 2000.
    """
    import numpy as np

    from dashboard import heatmap as heatmap_service

    if not cells:
        return {}
    since = end - timedelta(days=INCIDENT_DENSITY_WINDOW_DAYS)
    lons, lats, weights = heatmap_service.collect_points(area, "incidents", since, end)
    if lons.size == 0:
        return {}
    c = geo.shape_from_geojson(area.boundary).centroid
    plane = geo.LocalPlane(c.x, c.y)
    px, py = plane.to_xy(lons, lats)
    h, _ = geo.silverman_bandwidth(px, py, weights)
    # The surface is only ever sampled at cell centroids, one per grid cell, so a bandwidth narrower
    # than the grid spacing cannot be resolved — it would just read 0 almost everywhere. Floor it at
    # the cell size (the heat map endpoint, which rasterises finely, keeps the raw bandwidth).
    h = max(h, float(area.grid_cell_size_m or 1000))
    qx, qy = plane.to_xy(
        np.asarray([cell.centroid["coordinates"][0] for cell in cells], dtype=float),
        np.asarray([cell.centroid["coordinates"][1] for cell in cells], dtype=float),
    )
    values = geo.density_at(px, py, weights, qx, qy, h, "quartic")
    peak = float(values.max())
    if peak <= 0:
        return {}
    return {cell.pk: float(v / peak) for cell, v in zip(cells, values)}


def score_area(area: Area, day: date, hour: int = 20) -> int:
    """Compute and upsert RiskScore rows for every cell of ``area`` on ``day``. Returns count."""
    from field.models import Observation, TrackPoint

    cells = list(GrtsCell.objects.filter(area=area))
    if not cells or not area.boundary:
        return 0
    boundary = geo.shape_from_geojson(area.boundary)
    layers = {fl.kind: geo.shape_from_geojson(fl.geometry) for fl in FeatureLayer.objects.filter(area=area)}
    weights = {k: w for k, w in BASE_WEIGHTS.items()
               if not (k == "road_proximity" and "roads" not in layers) and not (k == "water_proximity" and "water" not in layers)}
    total_w = sum(weights.values())
    weights = {k: w / total_w for k, w in weights.items()}

    tz = zoneinfo.ZoneInfo(area.timezone or "UTC")
    moment = datetime.combine(day, time(hour=hour % 24), tzinfo=tz)
    end_of_day = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)

    last_obs = dict(Observation.objects.filter(area=area, cell__isnull=False, recorded_at__lt=end_of_day)
                    .values("cell_id").annotate(last=Max("recorded_at")).values_list("cell_id", "last"))
    last_track = dict(TrackPoint.objects.filter(organisation_id=area.organisation_id, cell__area=area,
                                                recorded_at__lt=end_of_day)
                      .values("cell_id").annotate(last=Max("recorded_at")).values_list("cell_id", "last"))
    incident_density = incident_density_by_cell(area, cells, end_of_day)

    # Temporal factors are identical for every cell on this date/hour.
    is_night = hour % 24 >= 18 or hour % 24 < 6
    moon = moon_illumination(moment.astimezone(dt_timezone.utc))
    season_val, season_label = season_component(day.month)
    weekend = day.weekday() >= 4

    c = boundary.centroid
    metric = geo.MetricContext(c.x, c.y)
    boundary_line = metric.boundary_line(boundary)
    metric_layers = {k: metric.project(g) for k, g in layers.items()}
    rows = []
    for cell in cells:
        lon, lat = cell.centroid["coordinates"]
        comps: dict[str, tuple[float, str]] = {}
        d_b = metric.distance_m(lon, lat, boundary_line)
        comps["boundary_proximity"] = (1 - min(d_b / 5000.0, 1.0), f"Distance to boundary: {d_b:,.0f} m")
        if "roads" in metric_layers:
            d = metric.distance_m(lon, lat, metric_layers["roads"])
            comps["road_proximity"] = (1 - min(d / 3000.0, 1.0), f"Distance to road: {d:,.0f} m")
        if "water" in metric_layers:
            d = metric.distance_m(lon, lat, metric_layers["water"])
            comps["water_proximity"] = (1 - min(d / 2000.0, 1.0), f"Distance to water: {d:,.0f} m")
        last_seen = max([t for t in (last_obs.get(cell.pk), last_track.get(cell.pk)) if t], default=None)
        if last_seen is None:
            comps["patrol_gap"] = (1.0, "Never patrolled")
        else:
            gap = max(0.0, (end_of_day - last_seen).total_seconds() / 86400.0)
            comps["patrol_gap"] = (min(gap / 30.0, 1.0), f"Last patrolled {gap:.0f} day(s) ago")
        comps["incident_history"] = (incident_density.get(cell.pk, 0.0), INCIDENT_DENSITY_LABEL)
        comps["hour_of_day"] = (1.0 if is_night else 0.3, f"{'Night' if is_night else 'Day'} ({hour % 24:02d}:00 local)")
        comps["moon_phase"] = (moon, f"Moon {moon * 100:.0f}% illuminated")
        comps["season"] = (season_val, season_label.capitalize())
        comps["day_of_week"] = (1.0 if weekend else 0.5, day.strftime("%A"))

        score = round(10 * sum(weights[k] * comps[k][0] for k in weights), 2)
        factors = [
            {"key": k, "label": comps[k][1], "weight": round(weights[k], 3), "value": round(comps[k][0], 3)}
            for k in sorted(weights, key=lambda key: -weights[key] * comps[key][0])
        ]
        rows.append((cell, score, factors))

    # One upsert statement per batch instead of a SELECT + UPDATE/INSERT per cell (fast over a remote DB).
    from django.utils import timezone

    now = timezone.now()
    with transaction.atomic():
        RiskScore.objects.bulk_create(
            [RiskScore(organisation_id=area.organisation_id, area=area, cell=cell, date=day, score=score,
                       level=level_for(score), factors=factors, created_at=now, updated_at=now)
             for cell, score, factors in rows],
            update_conflicts=True, unique_fields=["cell", "date"],
            update_fields=["score", "level", "factors", "updated_at", "area"], batch_size=500,
        )
    return len(rows)
