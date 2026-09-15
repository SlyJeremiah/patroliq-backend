import io
import json
import zipfile

import pytest
import shapefile
from pyproj import CRS, Transformer
from pyproj.enums import WktVersion
from shapely.geometry import shape

import geo
from areas.models import Area

from .conftest import client_for, make_org, make_user, utm_square

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup():
    org = make_org()
    admin = make_user(org, "org_admin")
    area = Area.objects.create(organisation=org, name="Import target", area_type="safari_area")
    return org, client_for(admin), area


def shapefile_zip(polygons_xy, prj_wkt=None, names=None) -> bytes:
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON)
    w.field("NAME", "C", size=40)
    for i, ring in enumerate(polygons_xy):
        w.poly([ring])
        w.record((names or [f"Block {i}"] * len(polygons_xy))[i])
    w.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("boundary/area.shp", shp.getvalue())
        z.writestr("boundary/area.shx", shx.getvalue())
        z.writestr("boundary/area.dbf", dbf.getvalue())
        if prj_wkt:
            z.writestr("boundary/area.prj", prj_wkt)
    return buf.getvalue()


def upload(client, area, name, data, **extra):
    f = io.BytesIO(data)
    f.name = name
    return client.post(f"/api/v1/areas/{area.pk}/boundary/import/", {"file": f, **extra}, format="multipart")


def test_shapefile_zip_in_utm_is_reprojected(setup):
    _, c, area = setup
    utm = CRS.from_epsg(32736)  # UTM 36S — Mazowe
    to_utm = Transformer.from_crs(4326, utm, always_xy=True)
    x0, y0 = [round(v, -3) for v in to_utm.transform(30.95, -17.50)]
    ring = [(x0, y0), (x0, y0 + 10_000), (x0 + 12_000, y0 + 10_000), (x0 + 12_000, y0), (x0, y0)]  # clockwise (ESRI)
    data = shapefile_zip([ring], prj_wkt=utm.to_wkt(WktVersion.WKT1_ESRI))

    r = upload(c, area, "mazowe.zip", data)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["features_found"] == 1
    assert body["crs_detected"] == "EPSG:32736"
    a = body["area"]
    assert a["boundary_source"] == "shapefile"
    assert a["boundary"]["type"] == "MultiPolygon"
    assert a["area_km2"] == pytest.approx(120.0, rel=0.01)  # 12 km x 10 km
    minx, miny, maxx, maxy = shape(a["boundary"]).bounds
    assert 30.9 < minx < maxx < 31.1 and -17.6 < miny < maxy < -17.4  # now lon/lat


def test_shapefile_without_prj_assumes_wgs84(setup):
    _, c, area = setup
    ring = [(30.90, -17.45), (31.00, -17.45), (31.00, -17.55), (30.90, -17.55), (30.90, -17.45)]
    r = upload(c, area, "wgs.zip", shapefile_zip([ring]))
    assert r.status_code == 200, r.content
    assert r.json()["crs_detected"].startswith("EPSG:4326")


def test_multiple_features_require_choice_or_dissolve(setup):
    _, c, area = setup
    west = [(30.90, -17.45), (30.95, -17.45), (30.95, -17.50), (30.90, -17.50), (30.90, -17.45)]
    east = [(30.95, -17.45), (31.00, -17.45), (31.00, -17.50), (30.95, -17.50), (30.95, -17.45)]
    data = shapefile_zip([west, east], names=["West", "East"])

    r = upload(c, area, "two.zip", data)
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "feature_selection_required"
    assert [f["name"] for f in err["fields"]["features"]] == ["West", "East"]

    one = upload(c, area, "two.zip", data, feature_index="1").json()
    both = upload(c, area, "two.zip", data, dissolve="true").json()
    assert both["features_found"] == 2
    assert both["area"]["area_km2"] == pytest.approx(2 * one["area"]["area_km2"], rel=0.01)
    assert len(both["area"]["boundary"]["coordinates"]) == 1  # adjacent blocks dissolve into one polygon
    assert upload(c, area, "two.zip", data, feature_index="5").json()["error"]["code"] == "invalid_feature_index"


def test_geojson_feature_collection(setup):
    _, c, area = setup
    poly = utm_square(30.95, -17.50, 3, 2)
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "Core"}, "geometry": json.loads(json.dumps(poly.__geo_interface__))}]}
    r = upload(c, area, "area.geojson", json.dumps(fc).encode())
    assert r.status_code == 200, r.content
    assert r.json()["area"]["boundary_source"] == "geojson"
    assert r.json()["area"]["area_km2"] == pytest.approx(6.0, rel=0.01)


def test_kml_polygon(setup):
    _, c, area = setup
    kml = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>Mazowe</name>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
30.90,-17.45,0 31.00,-17.45,0 31.00,-17.55,0 30.90,-17.55,0 30.90,-17.45,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></Document></kml>"""
    r = upload(c, area, "area.kml", kml)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["area"]["boundary_source"] == "kml"
    expected = geo.area_km2(shape({"type": "Polygon", "coordinates": [[(30.9, -17.45), (31.0, -17.45), (31.0, -17.55), (30.9, -17.55), (30.9, -17.45)]]}))
    assert body["area"]["area_km2"] == pytest.approx(expected, rel=0.001)
    assert 100 < expected < 130


def test_kml_with_entities_is_refused(setup):
    _, c, area = setup
    evil = b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY x "boom">]><kml><Placemark/></kml>'
    r = upload(c, area, "evil.kml", evil)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_file"


def test_invalid_self_intersecting_boundary_is_repaired(setup):
    _, c, area = setup
    bowtie = {"type": "Polygon", "coordinates": [[[30.90, -17.45], [31.00, -17.55], [31.00, -17.45], [30.90, -17.55], [30.90, -17.45]]]}
    r = c.put(f"/api/v1/areas/{area.pk}/boundary/", {"boundary": bowtie}, format="json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["boundary_source"] == "drawn"
    assert body["boundary"]["type"] == "MultiPolygon"
    assert shape(body["boundary"]).is_valid and body["area_km2"] > 0


def test_drawn_boundary_rejects_non_polygons(setup):
    _, c, area = setup
    r = c.put(f"/api/v1/areas/{area.pk}/boundary/", {"boundary": {"type": "Point", "coordinates": [30, -17]}}, format="json")
    assert r.status_code == 400


def test_apu_base_outside_boundary_rejected(setup):
    _, c, area = setup
    c.put(f"/api/v1/areas/{area.pk}/boundary/", {"boundary": utm_square(30.95, -17.50, 5, 5).__geo_interface__}, format="json")
    inside = c.post("/api/v1/apu-bases/", {"area_id": str(area.pk), "name": "HQ Camp", "code": "APU-1",
                                           "call_sign": "One", "location": {"type": "Point", "coordinates": [30.97, -17.48]}},
                    format="json")
    assert inside.status_code == 201, inside.content
    outside = c.post("/api/v1/apu-bases/", {"area_id": str(area.pk), "name": "Far", "code": "APU-9",
                                            "location": {"type": "Point", "coordinates": [31.50, -17.00]}}, format="json")
    assert outside.status_code == 400
    assert outside.json()["error"]["code"] == "outside_boundary"
    moved = c.patch(f"/api/v1/apu-bases/{inside.json()['id']}/",
                    {"location": {"type": "Point", "coordinates": [31.50, -17.00]}}, format="json")
    assert moved.status_code == 400 and moved.json()["error"]["code"] == "outside_boundary"


def test_unsupported_file(setup):
    _, c, area = setup
    r = upload(c, area, "notes.txt", b"hello")
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_file_type"
