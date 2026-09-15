"""Geometry primitives: conversion, validation, CRS, areas and distances."""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Iterable

from pyproj import CRS, Transformer
from shapely import make_valid
from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union

WGS84 = CRS.from_epsg(4326)
EARTH_RADIUS_M = 6_371_008.8


class GeoError(ValueError):
    """Invalid or unsupported geometry/input. ``code`` maps to the API error code."""

    def __init__(self, message: str, code: str = "invalid_geometry"):
        super().__init__(message)
        self.code = code
        self.message = message


# --- conversion ---------------------------------------------------------------------------------

def shape_from_geojson(obj: dict) -> BaseGeometry:
    """GeoJSON geometry / Feature / FeatureCollection (dissolved) -> shapely geometry."""
    if not isinstance(obj, dict) or "type" not in obj:
        raise GeoError("Not a GeoJSON object.")
    t = obj["type"]
    try:
        if t == "Feature":
            if not obj.get("geometry"):
                raise GeoError("Feature has no geometry.")
            return shape(obj["geometry"])
        if t == "FeatureCollection":
            geoms = [shape(f["geometry"]) for f in obj.get("features", []) if f.get("geometry")]
            if not geoms:
                raise GeoError("FeatureCollection has no geometries.")
            return unary_union(geoms)
        return shape(obj)
    except GeoError:
        raise
    except Exception as exc:  # shapely raises a variety of errors on malformed coordinates
        raise GeoError(f"Malformed GeoJSON geometry: {exc}") from exc


def round_geojson(obj, ndigits: int = 7):
    """Round coordinates (7 dp ≈ 1 cm) to keep sync payloads small."""
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, (list, tuple)):
        return [round_geojson(v, ndigits) for v in obj]
    if isinstance(obj, dict):
        return {k: round_geojson(v, ndigits) for k, v in obj.items()}
    return obj


def geojson_from_shape(geom: BaseGeometry, ndigits: int = 7) -> dict:
    return round_geojson(mapping(geom), ndigits)


def _polygons_of(geom: BaseGeometry) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if isinstance(geom, GeometryCollection):
        out: list[Polygon] = []
        for g in geom.geoms:
            out.extend(_polygons_of(g))
        return out
    return []


def normalise_boundary(geom: BaseGeometry) -> MultiPolygon:
    """
    Validate/repair and return a MultiPolygon (RFC 7946 orientation: exterior CCW).

    Repair uses ``make_valid`` (falls back to ``buffer(0)``); lines/points produced by repair are
    discarded. Coordinates must be WGS84 lon/lat.
    """
    if geom is None or geom.is_empty:
        raise GeoError("Boundary geometry is empty.")
    if geom.has_z:
        geom = shp_transform(lambda x, y, z=None: (x, y), geom)
    if not geom.is_valid:
        repaired = make_valid(geom)
        if not _polygons_of(repaired):
            repaired = geom.buffer(0)
        geom = repaired
    polys = [p for p in _polygons_of(geom) if p.area > 0]
    if not polys:
        raise GeoError("Boundary must contain at least one polygon with non-zero area.")
    minx, miny, maxx, maxy = unary_union(polys).bounds
    if minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
        raise GeoError("Boundary coordinates are not WGS84 longitude/latitude.", code="invalid_crs")
    merged = unary_union(polys)
    polys = _polygons_of(merged)
    return MultiPolygon([orient(p, sign=1.0) for p in polys])


# --- CRS ----------------------------------------------------------------------------------------

def utm_crs_for(lon: float, lat: float) -> CRS:
    zone = int((lon + 180) // 6) + 1
    zone = min(max(zone, 1), 60)
    return CRS.from_epsg((32600 if lat >= 0 else 32700) + zone)


@lru_cache(maxsize=64)
def _cached_transformer(src_wkt: str, dst_wkt: str) -> Transformer:
    return Transformer.from_crs(CRS.from_wkt(src_wkt), CRS.from_wkt(dst_wkt), always_xy=True)


def transformer(src: CRS, dst: CRS) -> Transformer:
    return _cached_transformer(src.to_wkt(), dst.to_wkt())


def reproject(geom: BaseGeometry, src: CRS, dst: CRS) -> BaseGeometry:
    if src == dst:
        return geom
    t = transformer(src, dst)
    return shp_transform(t.transform, geom)


def to_local_metric(geom: BaseGeometry, crs: CRS | None = None) -> tuple[BaseGeometry, CRS]:
    """Reproject WGS84 geometry into the UTM zone of its centroid (or ``crs``)."""
    if crs is None:
        c = geom.centroid
        crs = utm_crs_for(c.x, c.y)
    return reproject(geom, WGS84, crs), crs


# --- measurements -------------------------------------------------------------------------------

def area_km2(geom: BaseGeometry) -> float:
    """Area in km² using a Lambert Azimuthal Equal-Area projection centred on the geometry."""
    if geom.is_empty:
        return 0.0
    c = geom.centroid
    laea = CRS.from_proj4(f"+proj=laea +lat_0={c.y} +lon_0={c.x} +x_0=0 +y_0=0 +ellps=WGS84 +units=m +no_defs")
    return reproject(geom, WGS84, laea).area / 1_000_000.0


def point_in_geometry(lon: float, lat: float, geom: BaseGeometry) -> bool:
    """True if the point lies inside or on the edge of ``geom`` (WGS84)."""
    return geom.covers(Point(lon, lat))


def distance_to_boundary_m(lon: float, lat: float, geom: BaseGeometry, crs: CRS | None = None) -> float:
    """Distance from a point to the boundary *line* of a polygon (metres, local UTM)."""
    metric, crs = to_local_metric(geom, crs)
    pt = reproject(Point(lon, lat), WGS84, crs)
    return pt.distance(metric.boundary)


def distance_to_geometry_m(lon: float, lat: float, geom: BaseGeometry, crs: CRS | None = None) -> float:
    """Distance from a point to any geometry (roads, rivers …), metres; 0 when inside."""
    metric, crs = to_local_metric(geom, crs)
    pt = reproject(Point(lon, lat), WGS84, crs)
    return pt.distance(metric)


class MetricContext:
    """
    Pre-projected geometries for repeated point-distance queries (risk engine): project the
    boundary line and feature layers once, then measure many points against them.
    """

    def __init__(self, lon: float, lat: float):
        self.crs = utm_crs_for(lon, lat)
        self._to_metric = transformer(WGS84, self.crs)

    def project(self, geom: BaseGeometry) -> BaseGeometry:
        return shp_transform(self._to_metric.transform, geom)

    def boundary_line(self, polygonal: BaseGeometry) -> BaseGeometry:
        return self.project(polygonal).boundary

    def distance_m(self, lon: float, lat: float, metric_geom: BaseGeometry) -> float:
        x, y = self._to_metric.transform(lon, lat)
        return Point(x, y).distance(metric_geom)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def path_length_m(points: Iterable[tuple[float, float]]) -> float:
    """Sum of great-circle segment lengths for an ordered sequence of (lat, lon)."""
    total, prev = 0.0, None
    for lat, lon in points:
        if prev is not None:
            total += haversine_m(prev[0], prev[1], lat, lon)
        prev = (lat, lon)
    return total
