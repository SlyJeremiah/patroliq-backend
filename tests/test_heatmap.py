"""Kernel density heat map (spec v1.5 §C): the maths in :mod:`geo.density` and ``areas/{id}/heatmap/``."""
import base64
import math
import uuid
from datetime import timedelta

import numpy as np
import pytest
from django.utils import timezone

import geo
from areas.models import GrtsCell
from areas.risk import INCIDENT_DENSITY_LABEL, score_area
from dashboard import heatmap as heatmap_service
from field.models import Observation, SafetyAlert

from .conftest import client_for, make_area, make_org, make_user

pytestmark = pytest.mark.django_db


# --- the maths (no database) ----------------------------------------------------------------------

@pytest.mark.parametrize("kernel", ["quartic", "gaussian"])
def test_single_point_surface_is_symmetric_and_peaks_at_the_point(kernel):
    h, cell = 500.0, 25.0
    axis = np.arange(-2000.0, 2000.0 + cell, cell)
    grid = geo.density_grid(np.array([0.0]), np.array([0.0]), np.array([1.0]), axis, axis, h, kernel)
    centre = (axis.size // 2, axis.size // 2)
    assert np.unravel_index(int(np.argmax(grid)), grid.shape) == centre
    assert np.allclose(grid, grid[::-1, :]) and np.allclose(grid, grid[:, ::-1]) and np.allclose(grid, grid.T)
    # strictly decreasing away from the peak along a row
    row = grid[centre[0], centre[1]:]
    assert np.all(np.diff(row) <= 1e-12)
    # the quartic has compact support: nothing beyond one bandwidth
    if kernel == "quartic":
        assert grid[centre[0], centre[1] + int(h / cell) + 1] == 0.0


@pytest.mark.parametrize("kernel,tolerance", [("quartic", 0.01), ("gaussian", 0.02)])
def test_surface_integrates_to_the_total_weight(kernel, tolerance):
    """∫ density dA ≈ Σ wᵢ — the kernels are normalised, so the surface conserves mass."""
    rng = np.random.default_rng(7)
    px, py = rng.normal(0, 800, 40), rng.normal(0, 800, 40)
    w = rng.integers(1, 5, 40).astype(float)
    h, cell = 700.0, 50.0
    axis = np.arange(-8000.0, 8000.0 + cell, cell)
    grid = geo.density_grid(px, py, w, axis, axis, h, kernel)
    assert geo.integrate(grid, cell) == pytest.approx(float(w.sum()), rel=tolerance)


def test_density_at_matches_the_grid():
    h = 600.0
    px, py, w = np.array([0.0, 900.0]), np.array([0.0, 200.0]), np.array([2.0, 1.0])
    axis = np.arange(-1000.0, 1000.0, 50.0)
    grid = geo.density_grid(px, py, w, axis, axis, h, "quartic")
    qx, qy = np.meshgrid(axis, axis)
    point_wise = geo.density_at(px, py, w, qx.ravel(), qy.ravel(), h, "quartic").reshape(grid.shape)
    assert np.allclose(grid, point_wise)


def test_silverman_bandwidth_follows_the_spread():
    rng = np.random.default_rng(11)
    bandwidths = []
    for sigma in (500.0, 1500.0, 4000.0):
        x, y = rng.normal(0, sigma, 300), rng.normal(0, sigma, 300)
        h, rule = geo.silverman_bandwidth(x, y, np.ones(300))
        assert rule == "silverman"
        bandwidths.append(h)
    assert bandwidths[0] < bandwidths[1] < bandwidths[2]
    # h = 0.9 · min(SD, sqrt(1/ln2)·Dm) · n^(-0.2), clamped to [150, 5000] m
    assert all(geo.MIN_BANDWIDTH_M <= h <= geo.MAX_BANDWIDTH_M for h in bandwidths)
    assert geo.silverman_bandwidth(np.array([1.0]), np.array([2.0]), np.array([1.0])) == (geo.DEFAULT_BANDWIDTH_M,
                                                                                          "default")
    # more points at the same spread -> a narrower bandwidth (the n^(-0.2) term)
    x, y = rng.normal(0, 1500.0, 3000), rng.normal(0, 1500.0, 3000)
    assert geo.silverman_bandwidth(x, y, np.ones(3000))[0] < bandwidths[1]


def test_silverman_matches_the_published_formula():
    """Spot-check against a hand-computed value for a small, exactly known configuration."""
    x = np.array([-1000.0, 0.0, 1000.0, 0.0])
    y = np.array([0.0, -1000.0, 0.0, 1000.0])
    w = np.ones(4)
    sd = math.sqrt(((x ** 2 + y ** 2).sum()) / 4)            # mean centre is the origin
    dm = 1000.0                                               # every point is 1000 m away
    expected = 0.9 * min(sd, math.sqrt(1 / math.log(2)) * dm) * 4 ** -0.2
    assert geo.silverman_bandwidth(x, y, w) == (pytest.approx(expected), "silverman")


def test_uint8_scaling_never_loses_a_faint_hotspot():
    values = np.array([[0.0, 1e-6], [0.5, 1.0]])
    out = geo.scale_to_uint8(values, 1.0)
    assert out.tolist() == [[0, 1], [128, 255]]
    assert geo.scale_to_uint8(values, 0.0).max() == 0


def test_local_plane_round_trip():
    plane = geo.LocalPlane(30.95, -17.5)
    x, y = plane.to_xy(31.05, -17.4)
    lon, lat = plane.to_lonlat(x, y)
    assert (float(lon), float(lat)) == (pytest.approx(31.05), pytest.approx(-17.4))
    # 0.1° of latitude is ~11.1 km
    assert float(y) == pytest.approx(11119.5, rel=0.01)


# --- the endpoint ----------------------------------------------------------------------------------

@pytest.fixture
def heat():
    org = make_org()
    area = make_area(org, name="Mazowe")
    ranger = make_user(org, "ranger")
    manager = make_user(org, "manager")
    boundary = geo.shape_from_geojson(area.boundary)
    centre = boundary.centroid
    now = timezone.now()
    # Two snare reports in each of four GRTS cells: a realistic clustered pattern that still leaves
    # most of the area cold, so "the density is local" is actually testable.
    hot = list(GrtsCell.objects.filter(area=area).order_by("grts_order")[:4])
    for i in range(8):
        lon, lat = hot[i % 4].centroid["coordinates"]
        offset = (i // 4) * 0.0004
        Observation.objects.create(client_uuid=uuid.uuid4(), organisation=org, area=area, observer=ranger,
                                   category="threat", subtype="snare", severity="high",
                                   lat=lat + offset, lon=lon + offset, recorded_at=now - timedelta(days=i))
    return {"org": org, "area": area, "ranger": ranger, "manager": manager, "mgr": client_for(manager),
            "centre": centre, "hot": hot, "now": now}


def url(area, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"/api/v1/areas/{area.pk}/heatmap/" + (f"?{query}" if query else "")


def test_heatmap_response_shape(heat):
    r = heat["mgr"].get(url(heat["area"]))
    assert r.status_code == 200, r.content
    body = r.json()
    assert set(body) == {"area_id", "source", "days", "since", "until", "method", "kernel", "bandwidth_m",
                         "bandwidth_rule", "cell_size_m", "point_count", "unit", "bounds", "width", "height",
                         "encoding", "values", "max_density", "cells", "hotspots", "computed_at", "cached"}
    assert body["area_id"] == str(heat["area"].pk)
    assert (body["source"], body["days"], body["method"], body["kernel"]) == ("incidents", 90, "kernel_density",
                                                                              "quartic")
    assert body["encoding"] == "uint8-base64" and body["unit"] == "weighted events per km²"
    assert body["bandwidth_rule"] == "silverman" and geo.MIN_BANDWIDTH_M <= body["bandwidth_m"] <= geo.MAX_BANDWIDTH_M
    assert body["cell_size_m"] >= 30 and 0 < body["width"] <= 400 and 0 < body["height"] <= 400
    assert body["point_count"] == 8 and body["max_density"] > 0
    assert body["since"].endswith("Z") and body["until"].endswith("Z") and body["cached"] is False

    raster = base64.b64decode(body["values"])
    assert len(raster) == body["width"] * body["height"] and max(raster) == 255
    west, south, east, north = body["bounds"]
    assert west < east and south < north

    labels = {c["label"] for c in body["cells"]}
    assert labels == set(GrtsCell.objects.filter(area=heat["area"]).values_list("label", flat=True))
    assert all(0.0 <= c["density_norm"] <= 1.0 for c in body["cells"])
    assert max(c["density_norm"] for c in body["cells"]) > 0
    assert body["hotspots"] and body["hotspots"][0]["density"] == body["max_density"]
    hottest = body["hotspots"][0]
    assert west <= hottest["lon"] <= east and south <= hottest["lat"] <= north
    assert hottest["cell_label"] in {c.label for c in heat["hot"]}  # the hottest place is a snare cell
    assert all(h["density"] > 0 and h["cell_label"] in labels for h in body["hotspots"])
    assert len({(h["lat"], h["lon"]) for h in body["hotspots"]}) == len(body["hotspots"])
    assert max(c["density_norm"] for c in body["cells"] if c["label"] in {h.label for h in heat["hot"]}) > 0.5


def test_heatmap_is_cached_until_new_data_arrives(heat):
    first = heat["mgr"].get(url(heat["area"])).json()
    second = heat["mgr"].get(url(heat["area"])).json()
    assert second["cached"] is True and second["values"] == first["values"]

    Observation.objects.create(client_uuid=uuid.uuid4(), organisation=heat["org"], area=heat["area"],
                               observer=heat["ranger"], category="carcass", severity="critical",
                               lat=heat["centre"].y - 0.004, lon=heat["centre"].x - 0.004,
                               recorded_at=heat["now"] - timedelta(hours=1))
    third = heat["mgr"].get(url(heat["area"])).json()
    assert third["cached"] is False and third["point_count"] == 9


def test_heatmap_sources_weights_and_empty_case(heat):
    area, org = heat["area"], heat["org"]
    SafetyAlert.objects.create(client_uuid=uuid.uuid4(), organisation=org, ranger=heat["ranger"],
                               kind=SafetyAlert.HWC, status="active", area=area,
                               lat=heat["centre"].y, lon=heat["centre"].x, started_at=heat["now"],
                               details={"conflict_type": "human_injury", "people_injured": 2, "people_killed": 1})
    assert heat["mgr"].get(url(area, source="threats")).json()["point_count"] == 8
    assert heat["mgr"].get(url(area, source="hwc")).json()["point_count"] == 1
    assert heat["mgr"].get(url(area, source="incidents")).json()["point_count"] == 9
    assert heat["mgr"].get(url(area, source="all")).json()["point_count"] == 9

    empty = heat["mgr"].get(url(area, source="wildlife")).json()
    assert empty["point_count"] == 0 and empty["values"] is None and empty["max_density"] == 0
    assert empty["cells"] == [] and empty["hotspots"] == []
    assert empty["bandwidth_rule"] == "default" and empty["bandwidth_m"] == geo.DEFAULT_BANDWIDTH_M

    # 3 base + 2 injured + 1 killed, capped at 10
    assert heatmap_service.hwc_weight({"people_injured": 2, "people_killed": 1}) == 6
    assert heatmap_service.hwc_weight({"people_injured": 50}) == 10
    assert heatmap_service.observation_weight("threat", "critical", None) == 4
    assert heatmap_service.observation_weight("carcass", None, None) == 3
    assert heatmap_service.observation_weight("wildlife", None, 900) == 50


def test_heatmap_parameters(heat):
    area = heat["area"]
    body = heat["mgr"].get(url(area, days=30, kernel="gaussian", bandwidth_m=1200)).json()
    assert (body["days"], body["kernel"]) == (30, "gaussian")
    assert body["bandwidth_m"] == 1200.0 and body["bandwidth_rule"] == "manual"
    assert heat["mgr"].get(url(area, days=8)).json()["point_count"] == 8

    for bad in ("source=volcano", "kernel=epanechnikov", "days=0", "days=999", "bandwidth_m=5",
                "bandwidth_m=999999", "days=soon"):
        r = heat["mgr"].get(f"/api/v1/areas/{area.pk}/heatmap/?{bad}")
        assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error", bad
    assert heat["mgr"].get(url(area, species_id=uuid.uuid4())).status_code == 404


def test_heatmap_role_module_and_tenancy(heat):
    area = heat["area"]
    assert client_for(make_user(heat["org"], "ranger")).get(url(area)).status_code == 403
    assert client_for(make_user(heat["org"], "researcher")).get(url(area)).status_code == 403

    other = make_org()
    assert client_for(make_user(other, "manager")).get(url(area)).status_code == 404

    licence = heat["org"].licence
    licence.modules = [m for m in licence.modules if m != "ai_risk"]
    licence.save()
    r = heat["mgr"].get(url(area))
    assert r.status_code == 403 and r.json()["error"]["code"] == "module_disabled"


def test_risk_engine_uses_kde_incident_density(heat):
    area, today = heat["area"], timezone.localdate()
    assert score_area(area, today) == GrtsCell.objects.filter(area=area).count()
    from areas.models import RiskScore

    scores = list(RiskScore.objects.filter(area=area, date=today).select_related("cell"))
    factors = [next(f for f in s.factors if f["key"] == "incident_history") for s in scores]
    assert {f["label"] for f in factors} == {INCIDENT_DENSITY_LABEL}
    values = [f["value"] for f in factors]
    assert max(values) == pytest.approx(1.0) and min(values) == 0.0
    assert sum(1 for v in values if v > 0) < len(values)  # the density is local, not area-wide
