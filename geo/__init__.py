"""
PATROLIQ spatial toolkit — the ONLY place spatial logic lives.

Why not GeoDjango/PostGIS (yet)?
    The dev/CI machines have no GDAL, and SQLite must work out of the box. Geometries are therefore
    stored as GeoJSON (RFC 7946, WGS84 lon/lat) in ``JSONField`` columns and all computation is done
    in Python with shapely (geometry), pyproj (CRS/reprojection) and pyshp (shapefile reading).
    Conservation areas have at most a few thousand GRTS cells, so in-process computation is fast
    enough (bbox pre-filter + prepared geometries).

Public API (import from ``geo``):
    shape_from_geojson / geojson_from_shape    — conversion
    normalise_boundary                          — validate/repair to a MultiPolygon
    area_km2                                    — equal-area (local LAEA) area
    utm_crs_for, reproject, transformer         — CRS handling
    point_in_geometry, distance_to_boundary_m, distance_to_geometry_m
    haversine_m, path_length_m
    build_grid, grts_reverse_hierarchical_order — GRTS grid generation (see geo.grid)
    CellIndex                                   — fast point -> cell lookup
    read_boundary_file                          — shapefile zip / GeoJSON / KML import (geo.importers)

Migration path to PostGIS / GeoDjango (when GDAL is available in production):
    1. ``pip install`` GDAL, switch ENGINE to ``django.contrib.gis.db.backends.postgis`` and add
       ``django.contrib.gis`` to INSTALLED_APPS.
    2. Add ``geometry`` columns next to the JSON ones (``MultiPolygonField(srid=4326)`` on Area,
       ``PolygonField`` on GrtsCell, ``PointField`` on ApuBase/Observation/TrackPoint/PositionPing)
       and backfill with a data migration: ``GEOSGeometry(json.dumps(row.boundary))``.
    3. Re-implement the functions below with ORM/SQL equivalents, keeping signatures:
         point_in_geometry     -> ``ST_Covers`` / ``cells.filter(geometry__covers=pt)``
         area_km2              -> ``ST_Area(geom::geography) / 1e6``
         distance_*_m          -> ``ST_Distance(geography)``
         build_grid clipping   -> ``ST_SquareGrid`` + ``ST_Intersection`` (ordering stays in Python)
         reproject             -> ``ST_Transform``
    4. Keep the GeoJSON columns (or serialise from geometry) so the API contract does not change,
       then drop them once the Android app no longer depends on the JSON representation.
    Because callers only use this package, no view/serializer code has to change.
"""
from .core import (  # noqa: F401
    GeoError,
    MetricContext,
    area_km2,
    distance_to_boundary_m,
    distance_to_geometry_m,
    geojson_from_shape,
    haversine_m,
    normalise_boundary,
    path_length_m,
    point_in_geometry,
    reproject,
    round_geojson,
    shape_from_geojson,
    transformer,
    utm_crs_for,
)
from .grid import CellIndex, build_grid, grts_reverse_hierarchical_order  # noqa: F401
from .importers import BoundaryImport, read_boundary_file  # noqa: F401
