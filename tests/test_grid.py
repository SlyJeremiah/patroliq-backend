import pytest
from shapely.geometry import Point, shape

import geo
from areas.models import GrtsCell, Sector
from field.models import Observation

from .conftest import client_for, make_area, make_org, make_user, new_uuid, utm_square

pytestmark = pytest.mark.django_db


def test_grid_counts_labels_and_sectors():
    org = make_org()
    boundary = utm_square(30.95, -17.50, 5, 4)
    minx, miny, maxx, maxy = boundary.bounds
    west_base = (minx + 0.005, (miny + maxy) / 2)
    east_base = (maxx - 0.005, (miny + maxy) / 2)
    area = make_area(org, boundary=boundary, bases=[west_base, east_base], grid=False, status="draft")
    admin = make_user(org, "org_admin")
    c = client_for(admin)

    r = c.post(f"/api/v1/areas/{area.pk}/grid/generate/", {"cell_size_m": 1000}, format="json")
    assert r.status_code == 200, r.content
    assert r.json() == {"cells_created": 20, "sectors_created": 2}

    cells = c.get(f"/api/v1/areas/{area.pk}/cells/").json()
    assert len(cells) == 20
    assert [x["grts_order"] for x in cells] == list(range(1, 21))
    assert [x["label"] for x in cells] == [f"GRTS-{i:03d}" for i in range(1, 21)]
    for cell in cells:
        poly = shape(cell["geometry"])
        assert poly.geom_type == "Polygon"
        assert geo.area_km2(poly) == pytest.approx(1.0, rel=0.01)
        assert boundary.buffer(1e-5).covers(poly)  # ~1 m tolerance: UTM vs lon/lat edge curvature

    sectors = {s["id"]: s for s in c.get(f"/api/v1/areas/{area.pk}/sectors/").json()}
    assert len(sectors) == 2
    bases = {b.pk: b.location["coordinates"] for b in area.apu_bases.all()}
    for cell in cells:
        cx, cy = cell["centroid"]["coordinates"]
        nearest = min(bases, key=lambda pk: Point(bases[pk]).distance(Point(cx, cy)))
        assert sectors[cell["sector_id"]]["apu_base_id"] == str(nearest)


def test_grts_order_is_spatially_balanced():
    org = make_org()
    area = make_area(org, boundary=utm_square(30.95, -17.50, 4, 4), grid=True)
    cells = list(GrtsCell.objects.filter(area=area).order_by("grts_order"))
    assert len(cells) == 16
    c = shape(area.boundary).centroid
    quadrants = {(cell.centroid["coordinates"][0] > c.x, cell.centroid["coordinates"][1] > c.y) for cell in cells[:4]}
    assert len(quadrants) == 4  # first four samples hit all four quadrants


def test_grts_ordering_is_deterministic_and_unique():
    idx = [(c, r) for c in range(7) for r in range(5)]
    a = geo.grts_reverse_hierarchical_order(idx, 7, 5, "seed-1")
    b = geo.grts_reverse_hierarchical_order(idx, 7, 5, "seed-1")
    other = geo.grts_reverse_hierarchical_order(idx, 7, 5, "seed-2")
    assert a == b and len(set(a)) == len(idx)
    assert a != other


def test_slivers_dropped_and_single_sector_without_bases():
    org = make_org()
    # 3.05 km wide: the extra 50 m strip (5% of a cell) must not become cells.
    square = utm_square(30.95, -17.50, 3, 3)
    minx, miny, maxx, maxy = square.bounds
    area = make_area(org, boundary=square, bases=[], grid=False, status="draft")
    from areas.services import generate_grid

    assert generate_grid(area, 1000) == {"cells_created": 9, "sectors_created": 1}
    assert Sector.objects.get(area=area).apu_base is None

    lon_per_m = (maxx - minx) / 3000
    strip = geo.normalise_boundary(square.union(
        shape({"type": "Polygon", "coordinates": [[(maxx, miny), (maxx + 50 * lon_per_m, miny),
                                                   (maxx + 50 * lon_per_m, maxy), (maxx, maxy), (maxx, miny)]]})))
    area.boundary = geo.geojson_from_shape(strip)
    area.save()
    assert generate_grid(area, 1000, force=True)["cells_created"] == 9


def test_regenerate_refused_when_observations_reference_cells_unless_force():
    org = make_org()
    area = make_area(org)
    ranger = make_user(org, "ranger")
    cell = GrtsCell.objects.filter(area=area).first()
    lon, lat = cell.centroid["coordinates"]
    obs = Observation.objects.create(client_uuid=new_uuid(), organisation=org, area=area, observer=ranger,
                                     category="other", lat=lat, lon=lon, cell=cell, recorded_at="2026-09-01T10:00:00Z")
    c = client_for(make_user(org, "org_admin"))
    r = c.post(f"/api/v1/areas/{area.pk}/grid/generate/", {"cell_size_m": 500}, format="json")
    assert r.status_code == 409 and r.json()["error"]["code"] == "grid_in_use"
    r = c.post(f"/api/v1/areas/{area.pk}/grid/generate/", {"cell_size_m": 500, "force": True}, format="json")
    assert r.status_code == 200 and r.json()["cells_created"] == 80
    obs.refresh_from_db()
    assert obs.cell is not None and obs.cell.area_id == area.pk and shape(obs.cell.geometry).covers(Point(lon, lat))


def test_activation_requires_boundary_base_and_grid():
    org = make_org()
    admin = client_for(make_user(org, "org_admin"))
    area_id = admin.post("/api/v1/areas/", {"name": "Draft", "area_type": "concession"}, format="json").json()["id"]
    r = admin.post(f"/api/v1/areas/{area_id}/activate/", {}, format="json")
    assert r.status_code == 400
    assert set(r.json()["error"]["fields"]) == {"boundary", "apu_bases", "grid"}
    area = make_area(org)
    r = admin.post(f"/api/v1/areas/{area.pk}/activate/", {}, format="json")
    assert r.status_code == 200 and r.json()["status"] == "active"
