from datetime import date

import pytest
from django.core.management import call_command

from areas.models import FeatureLayer, RiskScore
from areas.risk import BASE_WEIGHTS, level_for, moon_illumination, score_area

from .conftest import client_for, make_area, make_org, make_user

pytestmark = pytest.mark.django_db


def test_risk_scores_without_optional_layers_renormalise():
    org = make_org()
    area = make_area(org)
    assert score_area(area, date(2026, 9, 15)) == 24
    scores = list(RiskScore.objects.filter(area=area))
    assert len(scores) == 24
    for s in scores:
        assert 0 <= s.score <= 10 and s.level == level_for(s.score)
        keys = {f["key"] for f in s.factors}
        assert "road_proximity" not in keys and "water_proximity" not in keys
        assert sum(f["weight"] for f in s.factors) == pytest.approx(1.0, abs=0.01)
        assert s.score == pytest.approx(10 * sum(f["weight"] * f["value"] for f in s.factors), abs=0.05)
        assert {"key", "label", "weight", "value"} == set(s.factors[0])
    # Cells at the edge score higher on boundary proximity than central cells.
    by_boundary = sorted(scores, key=lambda s: next(f["value"] for f in s.factors if f["key"] == "boundary_proximity"))
    assert by_boundary[0].score < by_boundary[-1].score


def test_risk_with_layers_and_command():
    org = make_org()
    area = make_area(org)
    c = area.boundary["coordinates"][0][0][0]
    FeatureLayer.objects.create(organisation=org, area=area, kind="roads",
                                geometry={"type": "LineString", "coordinates": [c, [c[0] + 0.05, c[1]]]})
    call_command("score_risk", "--date", "2026-09-15")
    s = RiskScore.objects.filter(area=area, date=date(2026, 9, 15)).first()
    keys = {f["key"] for f in s.factors}
    assert "road_proximity" in keys and "water_proximity" not in keys
    assert sum(f["weight"] for f in s.factors) == pytest.approx(1.0, abs=0.01)
    assert set(BASE_WEIGHTS) - keys == {"water_proximity"}

    ranger = make_user(org, "ranger")
    ranger.areas.add(area)
    body = client_for(ranger).get("/api/v1/sync/bootstrap/").json()
    # bootstrap window is relative to "today"; scores for the fixed date may be outside it.
    assert isinstance(body["risk_scores"], list)


def test_moon_illumination_known_dates():
    from datetime import datetime, timezone

    assert moon_illumination(datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)) == pytest.approx(0, abs=0.01)
    assert moon_illumination(datetime(2000, 1, 21, 5, 0, tzinfo=timezone.utc)) > 0.95  # full moon 2000-01-21


def test_unexpected_fields_on_endpoints():
    org = make_org()
    admin = client_for(make_user(org, "org_admin"))
    r = admin.post("/api/v1/areas/", {"name": "X", "area_type": "other", "is_public": True}, format="json")
    assert r.status_code == 400
    assert r.json() == {"error": {"code": "unexpected_fields", "message": "Unexpected fields in request.",
                                  "fields": {"is_public": ["Unexpected field."]}}}
    r = client_for().post("/api/v1/auth/login/", {"email": "a@b.org", "password": "x", "remember_me": True}, format="json")
    assert r.status_code == 400 and r.json()["error"]["code"] == "unexpected_fields"
    # Read-only fields echoed back by a client are tolerated.
    r = admin.post("/api/v1/areas/", {"name": "Y", "area_type": "other", "id": "ignored", "area_km2": 5}, format="json")
    assert r.status_code == 201 and r.json()["area_km2"] == 0


def test_validation_error_envelope():
    org = make_org()
    admin = client_for(make_user(org, "org_admin"))
    r = admin.post("/api/v1/areas/", {"area_type": "volcano"}, format="json")
    err = r.json()["error"]
    assert r.status_code == 400 and err["code"] == "validation_error"
    assert set(err["fields"]) == {"name", "area_type"}
    assert admin.get("/api/v1/nope/").json()["error"]["code"] == "not_found"
