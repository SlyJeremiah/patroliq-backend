"""Reports (spec §7): formats, anonymisation, roles, tenancy, sharing, storage fallback."""
import csv
import io
import json
from datetime import timedelta

import pytest
from django.utils import timezone

from areas.models import GrtsCell, Team
from audit.models import AuditLog
from dashboard.models import Report, ReportShare
from field.models import Observation, Patrol, Species, TrackPoint

from .conftest import client_for, make_area, make_org, make_user, new_uuid, utm_square

pytestmark = pytest.mark.django_db


@pytest.fixture
def rep():
    org = make_org("REP")
    area = make_area(org, name="Mazowe")
    team = Team.objects.create(organisation=org, area=area, apu_base=area.apu_bases.first(), name="River Team")
    ranger = make_user(org, "ranger", full_name="Tendai Moyo", team=team)
    cells = list(GrtsCell.objects.filter(area=area).order_by("grts_order"))
    now = timezone.now() - timedelta(days=2)
    elephant = Species.objects.create(common_name="African Savanna Elephant", scientific_name="Loxodonta africana")
    p = Patrol.objects.create(client_uuid=new_uuid(), organisation=org, ranger=ranger, team=team, area=area,
                              started_at=now, ended_at=now + timedelta(hours=3), status="ended", distance_m=8200,
                              duration_s=3 * 3600)
    for i, c in enumerate(cells[:4]):
        lon, lat = c.centroid["coordinates"]
        TrackPoint.objects.create(organisation=org, patrol=p, recorded_at=now + timedelta(minutes=20 * i), lat=lat, lon=lon,
                                  cell=c)
    lon, lat = cells[1].centroid["coordinates"]
    Observation.objects.create(client_uuid=new_uuid(), organisation=org, area=area, observer=ranger, patrol=p,
                               category="wildlife", species=elephant, species_name=elephant.common_name, count=6, sex="mixed",
                               male_count=2, female_count=3, lat=lat, lon=lon, cell=cells[1],
                               recorded_at=now + timedelta(minutes=25))
    Observation.objects.create(client_uuid=new_uuid(), organisation=org, area=area, observer=ranger, patrol=p,
                               category="threat", subtype="snare", severity="high", alert_manager=True,
                               notes="Wire snare removed", lat=lat + 0.001, lon=lon, cell=cells[1],
                               recorded_at=now + timedelta(minutes=45))
    manager = make_user(org, "manager", full_name="Grace Mutasa")
    other = make_org("ROT")
    make_area(other, boundary=utm_square(32.05, -20.25))
    return {"org": org, "area": area, "ranger": ranger, "manager": manager, "mgr": client_for(manager),
            "researcher": client_for(make_user(org, "researcher")), "viewer": client_for(make_user(org, "viewer")),
            "other_mgr": client_for(make_user(other, "manager")), "cells": cells}


def body(rep, **kw):
    today = timezone.now().date()
    return {"type": "patrol_summary", "format": "pdf", "date_from": (today - timedelta(days=30)).isoformat(),
            "date_to": today.isoformat(), "area_id": str(rep["area"].pk), **kw}


def download(client, report):
    r = client.get(f"/api/v1/reports/{report['id']}/download/")
    assert r.status_code == 200, getattr(r, "content", b"")
    return r, b"".join(r.streaming_content)


@pytest.mark.parametrize("rtype", ["patrol_summary", "incident_report", "wildlife_census", "threat_intelligence",
                                   "grts_survey", "ranger_performance", "donor_report", "zpwma_compliance"])
def test_pdf_for_every_type(rep, rtype):
    r = rep["mgr"].post("/api/v1/reports/", body(rep, type=rtype), format="json")
    assert r.status_code == 201, r.content
    out = r.json()
    assert out["status"] == "ready", out.get("error")
    assert set(out) >= {"id", "type", "format", "status", "title", "params", "size_bytes", "created_at", "created_by_name",
                        "anonymised", "summary"}
    assert out["created_by_name"] == "Grace Mutasa" and out["anonymised"] is False
    resp, data = download(rep["mgr"], out)
    assert data.startswith(b"%PDF") and len(data) == out["size_bytes"]
    assert resp["Content-Type"] == "application/pdf" and resp["Content-Disposition"].startswith("attachment;")


def test_summary_csv_geojson_and_audit(rep):
    r = rep["mgr"].post("/api/v1/reports/", body(rep, format="csv", type="incident_report"), format="json").json()
    s = r["summary"]
    assert (s["patrols"], s["observations"], s["high_severity_incidents"]) == (1, 2, 1)
    assert s["distance_km"] == 8.2 and s["grts_coverage_pct"] == round(4 / 20, 4)
    assert s["species"] == [{"name": "African Savanna Elephant", "count": 6}]
    assert {"week", "observations"} == set(s["by_week"][0]) and isinstance(s["risk_trend"], list)
    _, data = download(rep["mgr"], r)
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows[0][:3] == ["Recorded", "Category", "Type"] and "Lat" in rows[0] and "Reporter" in rows[0]
    assert len(rows) == 2 and "Tendai Moyo" in rows[1]

    r = rep["mgr"].post("/api/v1/reports/", body(rep, format="geojson"), format="json").json()
    _, data = download(rep["mgr"], r)
    fc = json.loads(data)
    assert fc["type"] == "FeatureCollection"
    kinds = {f["properties"]["kind"]: f["geometry"]["type"] for f in fc["features"]}
    assert kinds == {"track": "LineString", "observation": "Point"}
    assert all(f["type"] == "Feature" for f in fc["features"])

    actions = list(AuditLog.objects.filter(organisation_id=rep["org"].pk, action__startswith="report.")
                   .values_list("action", flat=True))
    assert actions.count("report.generate") == 2 and actions.count("report.download") == 2

    listing = rep["mgr"].get("/api/v1/reports/").json()
    assert len(listing) == 2 and rep["mgr"].get(f"/api/v1/reports/{r['id']}/").json()["id"] == r["id"]


def test_researcher_gets_anonymised_csv_and_no_pdf(rep):
    res = rep["researcher"]
    r = res.post("/api/v1/reports/", body(rep), format="json")
    assert r.status_code == 403 and r.json()["error"]["code"] == "format_not_allowed"
    assert rep["viewer"].post("/api/v1/reports/", body(rep), format="json").status_code == 403

    r = res.post("/api/v1/reports/", body(rep, format="csv", type="incident_report"), format="json")
    assert r.status_code == 201 and r.json()["anonymised"] is True
    _, data = download(res, r.json())
    text = data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert "Lat" not in rows[0] and "Lon" not in rows[0] and "Reporter" not in rows[0] and "Cell" in rows[0]
    assert "Tendai" not in text and str(rep["ranger"].pk) not in text
    assert rows[1][rows[0].index("Cell")] == rep["cells"][1].label

    r = res.post("/api/v1/reports/", body(rep, format="csv", type="ranger_performance"), format="json").json()
    _, data = download(res, r)
    assert "Tendai" not in data.decode("utf-8-sig") and "Ranger 01" in data.decode("utf-8-sig")

    r = res.post("/api/v1/reports/", body(rep, format="geojson"), format="json").json()
    _, data = download(res, r)
    fc = json.loads(data)
    assert fc["features"] and all(f["properties"]["kind"] == "observation" for f in fc["features"])  # no tracks
    assert all(f["geometry"]["type"] == "Polygon" for f in fc["features"])  # cell polygons, not points
    assert "Tendai" not in data.decode() and "observer_id" not in data.decode()

    assert res.post("/api/v1/reports/", body(rep, format="csv", ranger_id=str(rep["ranger"].pk)),
                    format="json").status_code == 400

    # Manager reports (not anonymised) are invisible to researchers.
    mgr_report = rep["mgr"].post("/api/v1/reports/", body(rep, format="csv"), format="json").json()
    assert res.get(f"/api/v1/reports/{mgr_report['id']}/").status_code == 404
    assert res.get(f"/api/v1/reports/{mgr_report['id']}/download/").status_code == 404
    assert mgr_report["id"] not in {x["id"] for x in res.get("/api/v1/reports/").json()}


def test_validation_tenancy_and_ranger(rep):
    mgr = rep["mgr"]
    today = timezone.now().date()
    r = mgr.post("/api/v1/reports/", body(rep, date_from=(today - timedelta(days=400)).isoformat()), format="json")
    assert r.status_code == 400 and r.json()["error"]["code"] == "date_range_too_large"
    r = mgr.post("/api/v1/reports/", body(rep, date_from=today.isoformat(), date_to=(today - timedelta(days=1)).isoformat()),
                 format="json")
    assert r.status_code == 400
    assert mgr.post("/api/v1/reports/", body(rep, type="poem"), format="json").status_code == 400
    assert mgr.post("/api/v1/reports/", body(rep, colour="red"), format="json").json()["error"]["code"] == "unexpected_fields"
    other_area = rep["other_mgr"].get("/api/v1/areas/").json()[0]["id"]
    r = mgr.post("/api/v1/reports/", body(rep, area_id=other_area), format="json")
    assert r.status_code == 400 and "area_id" in r.json()["error"]["fields"]

    report = mgr.post("/api/v1/reports/", body(rep, format="csv"), format="json").json()
    other = rep["other_mgr"]
    assert other.get(f"/api/v1/reports/{report['id']}/").status_code == 404
    assert other.get(f"/api/v1/reports/{report['id']}/download/").status_code == 404
    assert other.post(f"/api/v1/reports/{report['id']}/share/", {}, format="json").status_code == 404
    assert other.get("/api/v1/reports/").json() == []
    assert client_for(rep["ranger"]).get(f"/api/v1/reports/{report['id']}/").status_code == 403

    lic = rep["org"].licence
    lic.modules = ["grts"]
    lic.save()
    assert mgr.get("/api/v1/reports/").json()["error"]["code"] == "module_disabled"


def test_share_token_expiry_and_same_org(rep):
    mgr = rep["mgr"]
    report = mgr.post("/api/v1/reports/", body(rep, format="csv"), format="json").json()
    r = mgr.post(f"/api/v1/reports/{report['id']}/share/", {}, format="json")
    assert r.status_code == 201
    share = r.json()
    assert set(share) == {"url", "token", "expires_at"} and share["token"] in share["url"]
    row = ReportShare.objects.get(token=share["token"])
    assert abs((row.expires_at - timezone.now()) - timedelta(hours=48)) < timedelta(minutes=1)
    assert AuditLog.objects.filter(action="report.share", target_id=report["id"]).exists()

    colleague = client_for(make_user(rep["org"], "org_admin"))
    got = colleague.get(f"/api/v1/reports/shared/{share['token']}/")
    assert got.status_code == 200 and got.json()["id"] == report["id"] and got.json()["shared_by_name"] == "Grace Mutasa"
    assert rep["other_mgr"].get(f"/api/v1/reports/shared/{share['token']}/").status_code == 404
    assert client_for().get(f"/api/v1/reports/shared/{share['token']}/").status_code == 401
    r = rep["researcher"].get(f"/api/v1/reports/shared/{share['token']}/")
    assert r.status_code == 403 and r.json()["error"]["code"] == "anonymised_only"
    assert colleague.get("/api/v1/reports/shared/not-a-token/").status_code == 404

    ReportShare.objects.filter(pk=row.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    r = colleague.get(f"/api/v1/reports/shared/{share['token']}/")
    assert r.status_code == 410 and r.json()["error"]["code"] == "share_expired"


def test_download_regenerates_missing_file(rep):
    report = rep["mgr"].post("/api/v1/reports/", body(rep, format="csv"), format="json").json()
    obj = Report.objects.get(pk=report["id"])
    obj.file.storage.delete(obj.file.name)
    assert obj.file.name.startswith(f"reports/{rep['org'].pk}/")
    _, data = download(rep["mgr"], report)
    assert data.decode("utf-8-sig").startswith("Date,Ranger")
    assert AuditLog.objects.filter(action="report.download", detail__regenerated=True).exists()
