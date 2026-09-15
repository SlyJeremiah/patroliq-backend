"""Area set-up operations (boundary, grid, activation). Spatial work is delegated to :mod:`geo`."""
from __future__ import annotations

from django.db import transaction

import geo
from core.exceptions import ApiError
from core.models import Tombstone

from .models import ApuBase, Area, GrtsCell, Sector


def geo_api_error(exc: geo.GeoError) -> ApiError:
    return ApiError(400, exc.code, exc.message, getattr(exc, "fields", None))


def area_shape(area: Area):
    if not area.boundary:
        raise ApiError(400, "boundary_required", "The area has no boundary yet.")
    return geo.shape_from_geojson(area.boundary)


def set_boundary(area: Area, multipolygon, source: str) -> Area:
    area.boundary = geo.geojson_from_shape(multipolygon)
    area.boundary_source = source
    area.area_km2 = round(geo.area_km2(multipolygon), 4)
    area.save(update_fields=["boundary", "boundary_source", "area_km2", "updated_at"])
    return area


def ensure_point_in_area(area: Area, point: dict) -> None:
    lon, lat = point["coordinates"][0], point["coordinates"][1]
    if not geo.point_in_geometry(lon, lat, area_shape(area)):
        raise ApiError(400, "outside_boundary", "The point lies outside the area boundary.",
                       fields={"location": ["Point must lie inside the area boundary."]})


def grid_in_use(area: Area) -> bool:
    from field.models import Observation, TrackPoint

    return (
        Observation.objects.filter(area=area, cell__isnull=False).exists()
        or TrackPoint.objects.filter(organisation_id=area.organisation_id, cell__area=area).exists()
    )


def _build_cells(area: Area, cell_size_m: int, seed: str | None):
    boundary = area_shape(area)
    bases = list(ApuBase.objects.filter(area=area).order_by("code"))
    try:
        cells = geo.build_grid(
            boundary, cell_size_m, [(b.location["coordinates"][0], b.location["coordinates"][1]) for b in bases],
            seed=seed or str(area.pk),
        )
    except geo.GeoError as exc:
        raise geo_api_error(exc)
    return bases, cells


def preview_grid(area: Area, cell_size_m: int | None = None, seed: str | None = None) -> dict:
    """``dry_run``: the grid that ``generate_grid`` would create, as GeoJSON, without saving anything."""
    cell_size_m = int(cell_size_m or area.grid_cell_size_m or 1000)
    bases, cells = _build_cells(area, cell_size_m, seed)
    features = [{
        "type": "Feature",
        "id": None,
        "geometry": c.geometry,
        "properties": {"id": None, "label": c.label, "grts_order": c.grts_order, "sector_id": None,
                       "apu_base_id": str(bases[c.base_index].pk) if c.base_index is not None else None},
    } for c in cells]
    return {"cells_created": len(cells), "sectors_created": len(bases) or 1, "cell_size_m": cell_size_m,
            "dry_run": True, "cells": {"type": "FeatureCollection", "features": features}}


def cells_feature_collection(cells) -> dict:
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "id": str(c.pk),
        "geometry": c.geometry,
        "properties": {"id": str(c.pk), "label": c.label, "grts_order": c.grts_order,
                       "sector_id": str(c.sector_id) if c.sector_id else None, "area_id": str(c.area_id),
                       "centroid": c.centroid},
    } for c in cells]}


@transaction.atomic
def generate_grid(area: Area, cell_size_m: int | None = None, force: bool = False, seed: str | None = None) -> dict:
    """Replace the area's grid. Refused (409 ``grid_in_use``) when field data references cells unless ``force``."""
    area_shape(area)
    cell_size_m = int(cell_size_m or area.grid_cell_size_m or 1000)
    if GrtsCell.objects.filter(area=area).exists() and not force and grid_in_use(area):
        raise ApiError(409, "grid_in_use", "Patrol data references the current grid; pass force=true to replace it.")

    bases, cells = _build_cells(area, cell_size_m, seed)

    old_cell_ids = list(GrtsCell.objects.filter(area=area).values_list("pk", flat=True))
    old_sector_ids = list(Sector.objects.filter(area=area).values_list("pk", flat=True))
    GrtsCell.objects.filter(area=area).delete()  # observations/track points: cell -> NULL; risk scores cascade
    Sector.objects.filter(area=area).delete()
    Tombstone.record(area.organisation_id, "cells", old_cell_ids, area.pk)
    Tombstone.record(area.organisation_id, "sectors", old_sector_ids, area.pk)

    if bases:
        sectors = [Sector.objects.create(organisation_id=area.organisation_id, area=area, apu_base=b,
                                         name=f"{b.code} {b.name} sector") for b in bases]
    else:
        sectors = [Sector.objects.create(organisation_id=area.organisation_id, area=area, name="Sector 1")]

    GrtsCell.objects.bulk_create([
        GrtsCell(
            organisation_id=area.organisation_id, area=area,
            sector=sectors[c.base_index if c.base_index is not None else 0],
            label=c.label, grts_order=c.grts_order, geometry=c.geometry, centroid=c.centroid,
            min_lon=c.bbox[0], min_lat=c.bbox[1], max_lon=c.bbox[2], max_lat=c.bbox[3],
        )
        for c in cells
    ])
    area.grid_cell_size_m = cell_size_m
    area.save(update_fields=["grid_cell_size_m", "updated_at"])

    if old_cell_ids:
        reassign_cells(area)
    return {"cells_created": len(cells), "sectors_created": len(sectors)}


def cell_index(area_id) -> geo.CellIndex:
    return geo.CellIndex(GrtsCell.objects.filter(area_id=area_id).values_list("pk", "geometry"))


def reassign_cells(area: Area) -> None:
    """After a forced regrid, re-derive ``cell`` for the area's observations and track points."""
    from field.models import Observation, TrackPoint

    index = cell_index(area.pk)
    for obs in Observation.objects.filter(area=area, cell__isnull=True).only("pk", "lat", "lon"):
        cid = index.find(obs.lon, obs.lat)
        if cid:
            Observation.objects.filter(pk=obs.pk).update(cell_id=cid)
    for tp in TrackPoint.objects.filter(patrol__area=area, cell__isnull=True).only("pk", "lat", "lon"):
        cid = index.find(tp.lon, tp.lat)
        if cid:
            TrackPoint.objects.filter(pk=tp.pk).update(cell_id=cid)


def activation_problems(area: Area) -> dict:
    from accounts.licensing import module_enabled

    problems = {}
    if not area.boundary:
        problems["boundary"] = ["A boundary is required."]
    if not ApuBase.objects.filter(area=area).exists():
        problems["apu_bases"] = ["At least one APU base is required."]
    if module_enabled(area.organisation, "grts") and not GrtsCell.objects.filter(area=area).exists():
        problems["grid"] = ["A GRTS grid is required."]
    return problems
