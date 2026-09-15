"""
Boundary file importers: zipped ESRI shapefile, GeoJSON, KML/KMZ (spec §3 step 2).

Every reader produces a list of polygon *features* in WGS84. The caller then either picks one
(``feature_index``, 0-based) or dissolves all (``dissolve=True``); a file with several features
and no choice raises ``feature_selection_required`` listing the candidates.

Safety: archives are read fully in memory with an uncompressed-size cap (zip-bomb guard); KML
containing a DOCTYPE/ENTITY declaration is refused (XML entity expansion / XXE guard).
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import shapefile  # pyshp
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .core import WGS84, GeoError, area_km2, normalise_boundary, reproject

MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
POLYGON_SHAPE_TYPES = {shapefile.POLYGON, shapefile.POLYGONZ, shapefile.POLYGONM}


@dataclass
class Feature:
    geometry: BaseGeometry  # WGS84
    name: str = ""
    properties: dict = field(default_factory=dict)


@dataclass
class BoundaryImport:
    geometry: MultiPolygon
    features_found: int
    crs_detected: str
    source: str  # shapefile | geojson | kml


def crs_label(crs: CRS) -> str:
    try:
        epsg = crs.to_epsg(min_confidence=70)
    except CRSError:
        epsg = None
    return f"EPSG:{epsg}" if epsg else crs.name


def crs_from_prj(prj_text: str) -> CRS:
    text = prj_text.strip()
    for parse in (CRS.from_wkt, CRS.from_user_input):
        try:
            return parse(text)
        except (CRSError, TypeError, ValueError):
            continue
    raise GeoError("Could not interpret the .prj coordinate reference system.", code="invalid_crs")


# --- shapefile ----------------------------------------------------------------------------------

def _safe_zip(data: bytes) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise GeoError("File is not a valid .zip archive.", code="invalid_file") from exc
    total = sum(i.file_size for i in zf.infolist())
    if total > MAX_UNCOMPRESSED_BYTES:
        raise GeoError("Archive is too large when uncompressed.", code="file_too_large")
    return zf


def read_shapefile_zip(data: bytes) -> tuple[list[Feature], str]:
    zf = _safe_zip(data)
    names = [n for n in zf.namelist() if not n.endswith("/") and "__MACOSX" not in n]
    shp_names = sorted(n for n in names if n.lower().endswith(".shp"))
    if not shp_names:
        kml_names = [n for n in names if n.lower().endswith(".kml")]
        if kml_names:  # KMZ or zipped KML
            return read_kml(zf.read(kml_names[0])), "EPSG:4326"
        raise GeoError("The .zip does not contain a .shp file.", code="invalid_file")
    stem = os.path.splitext(shp_names[0])[0]
    lookup = {n.lower(): n for n in names}

    def member(ext: str, required: bool):
        name = lookup.get((stem + ext).lower())
        if name is None:
            if required:
                raise GeoError(f"Shapefile is missing its {ext} component.", code="invalid_file")
            return None
        return zf.read(name)

    shp, shx, dbf = member(".shp", True), member(".shx", True), member(".dbf", True)
    prj, cpg = member(".prj", False), member(".cpg", False)
    encoding = cpg.decode("ascii", "ignore").strip() if cpg else "utf-8"

    if prj:
        src_crs = crs_from_prj(prj.decode("utf-8", "replace"))
        label = crs_label(src_crs)
    else:
        src_crs, label = WGS84, "EPSG:4326 (assumed: no .prj)"

    try:
        reader = shapefile.Reader(
            shp=io.BytesIO(shp), shx=io.BytesIO(shx), dbf=io.BytesIO(dbf),
            encoding=encoding, encodingErrors="replace",
        )
    except Exception as exc:
        raise GeoError(f"Could not read shapefile: {exc}", code="invalid_file") from exc

    features: list[Feature] = []
    with reader:
        field_names = [f[0] for f in reader.fields[1:]]
        for sr in reader.iterShapeRecords():
            if sr.shape.shapeType not in POLYGON_SHAPE_TYPES or not sr.shape.points:
                continue
            geom = shape(sr.shape.__geo_interface__)
            geom = reproject(geom, src_crs, WGS84) if src_crs != WGS84 else geom
            props = {k: v for k, v in zip(field_names, list(sr.record)) if isinstance(v, (str, int, float))}
            name = next((str(props[k]) for k in props if k.lower() in {"name", "area_name", "label", "title"}), "")
            features.append(Feature(geometry=geom, name=name, properties=props))
    return features, label


# --- GeoJSON ------------------------------------------------------------------------------------

def _geojson_crs(obj: dict) -> CRS:
    """RFC 7946 is always WGS84; honour a legacy (2008) ``crs`` member if present."""
    crs = obj.get("crs")
    if not isinstance(crs, dict):
        return WGS84
    name = (crs.get("properties") or {}).get("name", "")
    if not name or "CRS84" in name.upper():
        return WGS84
    try:
        return CRS.from_user_input(name)
    except CRSError as exc:
        raise GeoError(f"Unsupported GeoJSON crs '{name}'.", code="invalid_crs") from exc


def read_geojson(data: bytes) -> tuple[list[Feature], str]:
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GeoError("File is not valid GeoJSON.", code="invalid_file") from exc
    if not isinstance(obj, dict):
        raise GeoError("File is not valid GeoJSON.", code="invalid_file")
    src = _geojson_crs(obj)
    t = obj.get("type")
    raw: list[tuple[dict, dict]] = []
    if t == "FeatureCollection":
        raw = [(f.get("geometry"), f.get("properties") or {}) for f in obj.get("features") or [] if isinstance(f, dict)]
    elif t == "Feature":
        raw = [(obj.get("geometry"), obj.get("properties") or {})]
    elif t == "GeometryCollection":
        raw = [(g, {}) for g in obj.get("geometries") or []]
    elif t in {"Polygon", "MultiPolygon"}:
        raw = [(obj, {})]
    else:
        raise GeoError("GeoJSON must be a FeatureCollection, Feature or (Multi)Polygon.", code="invalid_file")
    features = []
    for geom_obj, props in raw:
        if not isinstance(geom_obj, dict) or geom_obj.get("type") not in {"Polygon", "MultiPolygon"}:
            continue
        try:
            geom = shape(geom_obj)
        except Exception as exc:
            raise GeoError(f"Malformed GeoJSON geometry: {exc}") from exc
        if src != WGS84:
            geom = reproject(geom, src, WGS84)
        props = props if isinstance(props, dict) else {}
        name = str(props.get("name") or props.get("NAME") or "")
        features.append(Feature(geometry=geom, name=name, properties={k: v for k, v in props.items() if isinstance(v, (str, int, float))}))
    return features, crs_label(src)


# --- KML ----------------------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_coords(text: str | None) -> list[tuple[float, float]]:
    pts = []
    for token in (text or "").replace("\n", " ").replace("\t", " ").split():
        parts = token.split(",")
        if len(parts) >= 2:
            try:
                pts.append((float(parts[0]), float(parts[1])))
            except ValueError as exc:
                raise GeoError("Invalid KML coordinates.", code="invalid_file") from exc
    return pts


def _kml_polygon(el) -> Polygon | None:
    outer, inners = None, []
    for child in el.iter():
        tag = _local(child.tag)
        if tag in {"outerBoundaryIs", "innerBoundaryIs"}:
            coords = next((c for c in child.iter() if _local(c.tag) == "coordinates"), None)
            ring = _parse_coords(coords.text if coords is not None else None)
            if len(ring) < 3:
                continue
            if tag == "outerBoundaryIs":
                outer = ring
            else:
                inners.append(ring)
    return Polygon(outer, inners) if outer else None


def read_kml(data: bytes) -> list[Feature]:
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():
        raise GeoError("KML with DOCTYPE/ENTITY declarations is not accepted.", code="invalid_file")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise GeoError("File is not valid KML.", code="invalid_file") from exc
    features: list[Feature] = []
    placemarks = [el for el in root.iter() if _local(el.tag) == "Placemark"]
    containers = placemarks or [root]
    for pm in containers:
        polys = [p for p in (_kml_polygon(el) for el in pm.iter() if _local(el.tag) == "Polygon") if p is not None]
        if not polys:
            continue
        name_el = next((c for c in pm if _local(c.tag) == "name"), None)
        geom = polys[0] if len(polys) == 1 else MultiPolygon(polys)
        features.append(Feature(geometry=geom, name=(name_el.text or "").strip() if name_el is not None else ""))
    return features


# --- entry point --------------------------------------------------------------------------------

def detect_format(filename: str, data: bytes) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in {".zip", ".kmz"} or data[:2] == b"PK":
        return "shapefile"
    if ext in {".geojson", ".json"}:
        return "geojson"
    if ext == ".kml":
        return "kml"
    stripped = data.lstrip()[:1]
    if stripped == b"{":
        return "geojson"
    if stripped == b"<":
        return "kml"
    raise GeoError("Unsupported file type. Upload a .zip shapefile, .geojson/.json or .kml.", code="unsupported_file_type")


def read_boundary_file(filename: str, data: bytes, feature_index: int | None = None, dissolve: bool = False) -> BoundaryImport:
    fmt = detect_format(filename, data)
    if fmt == "shapefile":
        features, crs = read_shapefile_zip(data)
        source = "kml" if os.path.splitext(filename or "")[1].lower() == ".kmz" else "shapefile"
    elif fmt == "geojson":
        features, crs, source = *read_geojson(data), "geojson"
    else:
        features, crs, source = read_kml(data), "EPSG:4326", "kml"

    if not features:
        raise GeoError("No polygon features found in the file.", code="no_polygons")

    if dissolve:
        geom = unary_union([normalise_boundary(f.geometry) for f in features])
    elif feature_index is not None:
        if not 0 <= feature_index < len(features):
            raise GeoError(f"feature_index must be between 0 and {len(features) - 1}.", code="invalid_feature_index")
        geom = features[feature_index].geometry
    elif len(features) == 1:
        geom = features[0].geometry
    else:
        candidates = []
        for i, f in enumerate(features):
            try:
                km2 = round(area_km2(f.geometry), 3)
            except Exception:
                km2 = None
            candidates.append({"index": i, "name": f.name, "area_km2": km2})
        err = GeoError(
            f"The file holds {len(features)} polygon features; pass feature_index or dissolve=true.",
            code="feature_selection_required",
        )
        err.fields = {"features": candidates}
        raise err

    return BoundaryImport(geometry=normalise_boundary(geom), features_found=len(features), crs_detected=crs, source=source)
