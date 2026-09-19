"""
Kernel density heat map for one area (spec v1.5 §C) — ``GET areas/{id}/heatmap/``.

This module does the *data* half: choose and weight the source records, lay a regular lon/lat grid
over the area's bounding box, clip to the boundary, quantise the surface for the wire and pick the
hotspots. The estimator itself lives in :mod:`geo.density`, which is pure numpy and unit-tested on
its own.

Everything runs on every request — there is no batch job — so the result is cached for
``HEATMAP_CACHE_SECONDS`` under a key that includes the newest ``updated_at`` and the row count of
the source set. New data synced from a phone therefore invalidates the cache by itself.
"""
from __future__ import annotations

import base64
import hashlib
import math
from datetime import timedelta

import numpy as np
from django.conf import settings
from django.core.cache import cache
from django.db.models import Max, Q
from django.utils import timezone
from rest_framework import serializers
from shapely import contains_xy

import geo
from areas.models import Area, GrtsCell
from field.models import Observation, SafetyAlert

SOURCES = ("incidents", "threats", "carcasses", "hwc", "wildlife", "all")
DEFAULT_DAYS = 90
MAX_DAYS = 365
MIN_DAYS = 1
MANUAL_BANDWIDTH_RANGE = (50.0, 20000.0)

#: Grid sizing (spec v1.5 §C2).
TARGET_CELLS_ACROSS = 200
MIN_CELL_SIZE_M = 30.0
MAX_GRID = 400
MAX_HOTSPOTS = 8

#: Relative importance of each record type in the density sum (spec v1.5 §C2).
THREAT_WEIGHTS = {"low": 1.0, "medium": 2.0, "high": 3.0, "critical": 4.0}
CARCASS_WEIGHT = 3.0
HWC_BASE_WEIGHT = 3.0
HWC_MAX_WEIGHT = 10.0
WILDLIFE_MAX_WEIGHT = 50.0

_dt = serializers.DateTimeField()


def cache_seconds() -> int:
    return int(getattr(settings, "HEATMAP_CACHE_SECONDS", 600))


# --- source records ------------------------------------------------------------------------------

def _observation_categories(source: str) -> list[str]:
    return {
        "incidents": ["threat", "carcass"],
        "threats": ["threat"],
        "carcasses": ["carcass"],
        "hwc": [],
        "wildlife": ["wildlife"],
        "all": ["threat", "carcass", "wildlife"],
    }[source]


def _includes_hwc(source: str) -> bool:
    return source in ("incidents", "hwc", "all")


def observation_qs(area: Area, source: str, since, until, species_id=None):
    categories = _observation_categories(source)
    if not categories:
        return Observation.objects.none()
    qs = Observation.objects.filter(organisation_id=area.organisation_id, area=area, category__in=categories,
                                    recorded_at__gte=since, recorded_at__lt=until)
    if species_id is not None:
        qs = qs.filter(species_id=species_id)
    return qs


def hwc_qs(area: Area, source: str, since, until):
    if not _includes_hwc(source):
        return SafetyAlert.objects.none()
    return SafetyAlert.objects.filter(organisation_id=area.organisation_id, area=area, kind=SafetyAlert.HWC,
                                      started_at__gte=since, started_at__lt=until).exclude(
        Q(lat__isnull=True) | Q(lon__isnull=True))


def observation_weight(category: str, severity: str | None, count: int | None) -> float:
    if category == "threat":
        return THREAT_WEIGHTS.get(severity or "", 1.0)
    if category == "carcass":
        return CARCASS_WEIGHT
    return float(min(max(count or 1, 1), WILDLIFE_MAX_WEIGHT))  # wildlife: animal count, capped


def hwc_weight(details: dict | None) -> float:
    d = details or {}
    casualties = (d.get("people_injured") or 0) + (d.get("people_killed") or 0)
    return float(min(HWC_BASE_WEIGHT + casualties, HWC_MAX_WEIGHT))


def collect_points(area: Area, source: str, since, until, species_id=None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(lon, lat, weight)`` arrays for the requested source over ``[since, until)``."""
    lons, lats, weights = [], [], []
    for lon, lat, category, severity, count in observation_qs(area, source, since, until, species_id).values_list(
            "lon", "lat", "category", "severity", "count"):
        lons.append(lon)
        lats.append(lat)
        weights.append(observation_weight(category, severity, count))
    for lon, lat, details in hwc_qs(area, source, since, until).values_list("lon", "lat", "details"):
        lons.append(lon)
        lats.append(lat)
        weights.append(hwc_weight(details))
    return (np.asarray(lons, dtype=float), np.asarray(lats, dtype=float), np.asarray(weights, dtype=float))


def source_fingerprint(area: Area, source: str, since, until, species_id=None) -> str:
    """Newest ``updated_at`` + row count of the source set — the cache's freshness token."""
    obs = observation_qs(area, source, since, until, species_id).aggregate(n=Max("updated_at"))
    obs_count = observation_qs(area, source, since, until, species_id).count()
    alerts = hwc_qs(area, source, since, until).aggregate(n=Max("updated_at"))
    alert_count = hwc_qs(area, source, since, until).count()
    newest = max([t for t in (obs["n"], alerts["n"]) if t], default=None)
    return f"{newest.isoformat() if newest else 'none'}:{obs_count + alert_count}"


# --- grid ----------------------------------------------------------------------------------------

def plan_grid(bounds: tuple[float, float, float, float]) -> tuple[geo.LocalPlane, float, np.ndarray, np.ndarray,
                                                                  np.ndarray, np.ndarray]:
    """
    Lay a regular lon/lat grid over ``bounds`` (west, south, east, north).

    Cell size is ``min(bbox side) / 200`` but never below 30 m, and is enlarged further if that
    would exceed a 400 × 400 raster. Returns the projection plane, the cell size in metres, the
    metric axes (cell centres, ascending) and the matching lon/lat axes.
    """
    west, south, east, north = bounds
    plane = geo.LocalPlane((west + east) / 2.0, (south + north) / 2.0)
    width_m = max((east - west) * plane.m_per_deg_lon, 1.0)
    height_m = max((north - south) * plane.m_per_deg_lat, 1.0)
    cell = max(min(width_m, height_m) / TARGET_CELLS_ACROSS, MIN_CELL_SIZE_M)
    cell = max(cell, width_m / MAX_GRID, height_m / MAX_GRID)
    nx = max(1, min(MAX_GRID, int(math.ceil(width_m / cell))))
    ny = max(1, min(MAX_GRID, int(math.ceil(height_m / cell))))
    x0, y0 = plane.to_xy(west, south)
    x_axis = float(x0) + (np.arange(nx) + 0.5) * cell
    y_axis = float(y0) + (np.arange(ny) + 0.5) * cell
    lon_axis, _ = plane.to_lonlat(x_axis, np.zeros_like(x_axis))
    _, lat_axis = plane.to_lonlat(np.zeros_like(y_axis), y_axis)
    return plane, cell, x_axis, y_axis, lon_axis, lat_axis


def clip_to_boundary(grid: np.ndarray, boundary, lon_axis: np.ndarray, lat_axis: np.ndarray) -> np.ndarray:
    """Zero every cell whose centre falls outside the area. Only cells with density are tested."""
    positive = grid > 0
    if not positive.any():
        return grid
    lon_mesh, lat_mesh = np.meshgrid(lon_axis, lat_axis)
    inside = np.zeros(grid.shape, dtype=bool)
    inside[positive] = contains_xy(boundary, lon_mesh[positive], lat_mesh[positive])
    return np.where(inside, grid, 0.0)


# --- main entry point ----------------------------------------------------------------------------

def empty_response(area: Area, source: str, days: int, since, until, kernel: str, bandwidth_m: float,
                   bandwidth_rule: str, cell_size_m: float, bounds, width: int, height: int) -> dict:
    """No points in the window: the grid is still described, but ``values`` is null (spec v1.5 §C3)."""
    return {
        "area_id": str(area.pk), "source": source, "days": days,
        "since": _dt.to_representation(since), "until": _dt.to_representation(until),
        "method": "kernel_density", "kernel": kernel,
        "bandwidth_m": round(bandwidth_m, 1), "bandwidth_rule": bandwidth_rule,
        "cell_size_m": round(cell_size_m, 1), "point_count": 0, "unit": "weighted events per km²",
        "bounds": bounds, "width": width, "height": height, "encoding": "uint8-base64", "values": None,
        "max_density": 0.0, "cells": [], "hotspots": [],
        "computed_at": _dt.to_representation(timezone.now()), "cached": False,
    }


def compute(area: Area, source: str = "incidents", days: int = DEFAULT_DAYS, kernel: str = "quartic",
            bandwidth_m: float | None = None, species_id=None, until=None) -> dict:
    """Compute the heat map (no caching — see :func:`heatmap`)."""
    until = until or timezone.now()
    since = until - timedelta(days=days)
    boundary = geo.shape_from_geojson(area.boundary)
    west, south, east, north = boundary.bounds
    bounds = [round(west, 7), round(south, 7), round(east, 7), round(north, 7)]
    plane, cell_size, x_axis, y_axis, lon_axis, lat_axis = plan_grid((west, south, east, north))

    lons, lats, weights = collect_points(area, source, since, until, species_id)
    px, py = plane.to_xy(lons, lats)
    if bandwidth_m is not None:
        h, rule = float(bandwidth_m), "manual"
    else:
        h, rule = geo.silverman_bandwidth(px, py, weights)
    if lons.size == 0:
        return empty_response(area, source, days, since, until, kernel, h, rule, cell_size, bounds,
                              int(lon_axis.size), int(lat_axis.size))

    grid = geo.density_grid(px, py, weights, x_axis, y_axis, h, kernel)
    grid = clip_to_boundary(grid, boundary, lon_axis, lat_axis)
    grid_km2 = geo.per_km2(grid)
    max_density = float(grid_km2.max())

    # Row-major, NORTH row first: numpy row 0 is the southernmost, so flip.
    raster = geo.scale_to_uint8(grid_km2, max_density)[::-1, :]
    values = base64.b64encode(raster.tobytes()).decode("ascii")

    cells = _cell_densities(area, plane, px, py, weights, h, kernel, max_density)
    hotspots = _hotspots(area, grid_km2, lon_axis, lat_axis, h, cell_size)
    return {
        "area_id": str(area.pk), "source": source, "days": days,
        "since": _dt.to_representation(since), "until": _dt.to_representation(until),
        "method": "kernel_density", "kernel": kernel,
        "bandwidth_m": round(h, 1), "bandwidth_rule": rule,
        "cell_size_m": round(cell_size, 1), "point_count": int(lons.size),
        "unit": "weighted events per km²",
        "bounds": bounds, "width": int(lon_axis.size), "height": int(lat_axis.size),
        "encoding": "uint8-base64", "values": values, "max_density": round(max_density, 4),
        "cells": cells, "hotspots": hotspots,
        "computed_at": _dt.to_representation(timezone.now()), "cached": False,
    }


def _cell_densities(area: Area, plane, px, py, weights, h, kernel, max_density) -> list[dict]:
    rows = list(GrtsCell.objects.filter(area=area).order_by("grts_order").values("pk", "label", "centroid"))
    if not rows:
        return []
    lon = np.asarray([r["centroid"]["coordinates"][0] for r in rows], dtype=float)
    lat = np.asarray([r["centroid"]["coordinates"][1] for r in rows], dtype=float)
    qx, qy = plane.to_xy(lon, lat)
    values = geo.per_km2(geo.density_at(px, py, weights, qx, qy, h, kernel))
    out = []
    for row, value in zip(rows, values):
        norm = float(min(value / max_density, 1.0)) if max_density > 0 else 0.0
        out.append({"cell_id": str(row["pk"]), "label": row["label"], "density": round(float(value), 4),
                    "density_norm": round(norm, 4)})
    return out


def _hotspots(area: Area, grid_km2: np.ndarray, lon_axis, lat_axis, h: float, cell_size: float) -> list[dict]:
    picks = geo.peak_indices(grid_km2, MAX_HOTSPOTS, min_separation_cells=max(h / max(cell_size, 1.0), 2.0))
    if not picks:
        return []
    from areas.services import cell_index

    index = cell_index(area.pk) if GrtsCell.objects.filter(area=area).exists() else None
    labels = dict(GrtsCell.objects.filter(area=area).values_list("pk", "label"))
    out = []
    for j, i in picks:
        lon, lat = float(lon_axis[i]), float(lat_axis[j])
        cell_id = index.find(lon, lat) if index is not None else None
        out.append({"lat": round(lat, 6), "lon": round(lon, 6), "density": round(float(grid_km2[j, i]), 4),
                    "cell_label": labels.get(cell_id)})
    return out


def heatmap(area: Area, source: str = "incidents", days: int = DEFAULT_DAYS, kernel: str = "quartic",
            bandwidth_m: float | None = None, species_id=None) -> dict:
    """Cached :func:`compute`. The cache key carries the freshness token of the source set."""
    until = timezone.now()
    # No timestamp in the key: within the TTL the same surface is reused (the window then lags by up
    # to ``HEATMAP_CACHE_SECONDS``), and any newly synced record changes the freshness token instead.
    token = source_fingerprint(area, source, until - timedelta(days=days), until, species_id)
    raw = f"{area.pk}|{area.updated_at.isoformat()}|{source}|{days}|{kernel}|{bandwidth_m}|{species_id}|{token}"
    key = "heatmap:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    hit = cache.get(key)
    if hit is not None:
        return {**hit, "cached": True}
    body = compute(area, source=source, days=days, kernel=kernel, bandwidth_m=bandwidth_m, species_id=species_id,
                   until=until)
    cache.set(key, body, cache_seconds())
    return body
