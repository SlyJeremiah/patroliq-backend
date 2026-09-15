import hashlib
from datetime import timedelta

import pytest
from django.utils import timezone
from shapely.geometry import shape

from areas.models import ApuBase, Area, Assignment, GrtsCell, Sector, Team
from field.models import Media, Observation, Patrol, PositionPing, Species, TrackPoint

from .conftest import client_for, make_area, make_org, make_user, new_uuid, utm_square

pytestmark = pytest.mark.django_db


@pytest.fixture
def field():
    org = make_org("GRTTS")
    area = make_area(org, name="Mazowe Conservancy", boundary=utm_square(30.95, -17.50, 5, 4))
    draft = make_area(org, name="Draft area", status="draft", boundary=utm_square(31.30, -17.50, 2, 2))
    base = ApuBase.objects.filter(area=area).first()
    team = Team.objects.create(organisation=org, area=area, apu_base=base, name="River Team")
    ranger = make_user(org, "ranger", employee_id="RGR-2026-041", team=team)
    buddy = make_user(org, "ranger", employee_id="RGR-2026-038", team=team)
    ranger.areas.add(area, draft)
    cells = list(GrtsCell.objects.filter(area=area).order_by("grts_order")[:5])
    assignment = Assignment.objects.create(organisation=org, team=team, area=area, date=timezone.now().date())
    assignment.cells.set(cells)
    Species.objects.create(common_name="Lion", scientific_name="Panthera leo", iucn_status="VU")
    make_user(org, "manager")
    return {"org": org, "area": area, "draft": draft, "ranger": ranger, "buddy": buddy, "team": team,
            "cells": cells, "assignment": assignment, "client": client_for(ranger)}


def test_bootstrap_contents(field):
    r = field["client"].get("/api/v1/sync/bootstrap/")
    assert r.status_code == 200, r.content
    body = r.json()
    expected_keys = {"server_time", "areas", "apu_bases", "sectors", "cells", "assignments", "risk_scores", "species",
                     "team_members", "licence"}
    assert expected_keys <= set(body)
    assert body["server_time"].endswith("Z")
    assert [a["name"] for a in body["areas"]] == ["Mazowe Conservancy"]  # draft area excluded
    area = body["areas"][0]
    assert area["boundary"]["type"] == "MultiPolygon" and area["updated_at"].endswith("Z")
    assert len(body["apu_bases"]) == 1 and body["apu_bases"][0]["location"]["type"] == "Point"
    assert len(body["cells"]) == 20 and len(body["sectors"]) == 1
    assert {"id", "area_id", "sector_id", "label", "grts_order", "geometry", "centroid", "updated_at"} <= set(body["cells"][0])
    assert len(body["assignments"]) == 1
    assert sorted(body["assignments"][0]["cell_ids"]) == sorted(str(c.pk) for c in field["cells"])
    assert body["species"][0]["common_name"] == "Lion"
    assert {m["employee_id"] for m in body["team_members"]} == {"RGR-2026-041", "RGR-2026-038"}
    assert set(body["team_members"][0]) == {"id", "full_name", "employee_id", "role"}
    assert body["licence"]["plan"] == "standard"


def test_bootstrap_since_returns_only_changes(field):
    c = field["client"]
    past = timezone.now() - timedelta(hours=1)
    for model in (Area, ApuBase, Sector, GrtsCell, Assignment, Species):
        model.objects.update(updated_at=past)
    first = c.get("/api/v1/sync/bootstrap/").json()
    later = timezone.now() + timedelta(seconds=2)
    r = c.get("/api/v1/sync/bootstrap/", {"since": later.strftime("%Y-%m-%dT%H:%M:%SZ")}).json()
    assert r["areas"] == [] and r["cells"] == [] and r["apu_bases"] == [] and r["species"] == []

    base = ApuBase.objects.get(area=field["area"])
    base.call_sign = "Changed"
    base.save()
    r = c.get("/api/v1/sync/bootstrap/", {"since": first["server_time"]}).json()
    assert [b["call_sign"] for b in r["apu_bases"]] == ["Changed"]
    assert r["cells"] == []
    # "+00:00" offset (unencoded '+' arrives as space) is accepted too.
    assert c.get("/api/v1/sync/bootstrap/?since=2026-01-01T00:00:00 00:00").status_code == 200
    assert c.get("/api/v1/sync/bootstrap/?since=yesterday").json()["error"]["code"] == "validation_error"


def test_bootstrap_since_reports_deleted_cells(field):
    c = field["client"]
    since = c.get("/api/v1/sync/bootstrap/").json()["server_time"]
    from areas.services import generate_grid

    old = {str(pk) for pk in GrtsCell.objects.filter(area=field["area"]).values_list("pk", flat=True)}
    generate_grid(field["area"], 2000)
    body = c.get("/api/v1/sync/bootstrap/", {"since": since}).json()
    assert set(body["deleted"]["cells"]) == old
    assert len(body["deleted"]["sectors"]) == 1
    assert len(body["cells"]) == GrtsCell.objects.filter(area=field["area"]).count()
    assert not old & {c["id"] for c in body["cells"]}


def _payload(field, patrol_id, obs_id, lon, lat):
    t0 = "2026-09-15T06:00:00Z"
    return {
        "patrols": [{"client_uuid": patrol_id, "ranger_id": str(field["ranger"].pk), "team_id": str(field["team"].pk),
                     "area_id": str(field["area"].pk), "patrol_type": "foot", "started_at": t0,
                     "ended_at": "2026-09-15T08:30:00Z", "status": "ended", "notes": "Morning sweep"}],
        "track_points": [
            {"patrol_client_uuid": patrol_id, "recorded_at": f"2026-09-15T06:{m:02d}:00Z", "lat": lat + 0.001 * i,
             "lon": lon, "accuracy_m": 5.0, "speed_mps": 1.2}
            for i, m in enumerate((0, 10, 20))
        ],
        "observations": [{
            "client_uuid": obs_id, "patrol_client_uuid": patrol_id, "area_id": str(field["area"].pk),
            "category": "threat", "subtype": "snare", "severity": "high", "alert_manager": True,
            "notes": "<b>Wire</b> snare <script>alert(1)</script>removed", "lat": lat, "lon": lon, "accuracy_m": 4.5,
            "recorded_at": "2026-09-15T06:15:00Z", "cell_id": new_uuid(),
        }],
        "safety_alerts": [],
    }


def test_push_is_idempotent_and_assigns_cells(field):
    cell = field["cells"][0]
    lon, lat = shape(cell.geometry).representative_point().coords[0]
    patrol_id, obs_id = new_uuid(), new_uuid()
    payload = _payload(field, patrol_id, obs_id, lon, lat)

    for _ in range(2):
        r = field["client"].post("/api/v1/sync/push/", payload, format="json")
        assert r.status_code == 200, r.content
        body = r.json()
        assert body["accepted"] == {"patrols": [patrol_id], "observations": [obs_id], "safety_alerts": []}
        assert body["track_points_accepted"] == 3
        assert body["rejected"] == []

    assert Patrol.objects.count() == 1 and Observation.objects.count() == 1 and TrackPoint.objects.count() == 3
    obs = Observation.objects.get(pk=obs_id)
    assert obs.cell_id == cell.pk  # server-derived, client value ignored
    assert obs.notes == "Wire snare removed"
    patrol = Patrol.objects.get(pk=patrol_id)
    assert patrol.duration_s == 9000
    assert 200 < patrol.distance_m < 245  # ~0.002 degrees of latitude
    assert TrackPoint.objects.filter(cell=cell).exists()


def test_push_rejections_do_not_block_batch(field):
    good, bad = new_uuid(), new_uuid()
    base = {"area_id": str(field["area"].pk), "category": "habitat", "lat": -17.5, "lon": 30.95,
            "recorded_at": "2026-09-15T06:15:00Z"}
    r = field["client"].post("/api/v1/sync/push/", {"observations": [
        {**base, "client_uuid": good},
        {**base, "client_uuid": bad, "colour": "red"},
        {**base, "client_uuid": new_uuid(), "category": "unicorn"},
        {**base, "client_uuid": new_uuid(), "observer_id": str(field["buddy"].pk)},
    ]}, format="json").json()
    assert r["accepted"]["observations"] == [good]
    assert [x["code"] for x in r["rejected"]] == ["unexpected_fields", "validation_error", "forbidden"]
    assert r["rejected"][0]["client_uuid"] == bad


def test_ended_patrol_does_not_revert(field):
    pid = new_uuid()
    body = {"client_uuid": pid, "area_id": str(field["area"].pk), "started_at": "2026-09-15T06:00:00Z",
            "status": "ended", "ended_at": "2026-09-15T07:00:00Z", "distance_m": 1234.5}
    c = field["client"]
    c.post("/api/v1/sync/push/", {"patrols": [body]}, format="json")
    c.post("/api/v1/sync/push/", {"patrols": [{**body, "status": "active", "ended_at": None}]}, format="json")
    p = Patrol.objects.get(pk=pid)
    assert p.status == "ended" and p.distance_m == 1234.5
    listed = client_for(make_user(field["org"], "manager")).get("/api/v1/patrols/").json()
    assert listed[0]["distance_m"] == 1234  # integers on output


def test_push_top_level_unexpected_fields(field):
    r = field["client"].post("/api/v1/sync/push/", {"observations": [], "extra": 1}, format="json")
    assert r.status_code == 400 and r.json()["error"]["code"] == "unexpected_fields"


def test_media_upload_sha256_and_idempotent(field):
    obs_id = new_uuid()
    field["client"].post("/api/v1/sync/push/", {"observations": [{
        "client_uuid": obs_id, "area_id": str(field["area"].pk), "category": "wildlife", "lat": -17.5, "lon": 30.95,
        "recorded_at": "2026-09-15T06:15:00Z"}]}, format="json")
    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 2048
    from django.core.files.uploadedfile import SimpleUploadedFile

    def send():
        f = SimpleUploadedFile("lion.jpg", jpeg, content_type="application/octet-stream")
        return field["client"].post("/api/v1/media/", {"file": f, "observation_client_uuid": obs_id, "kind": "photo"},
                                    format="multipart")

    r = send()
    assert r.status_code == 201, r.content
    m = r.json()
    assert m["sha256"] == hashlib.sha256(jpeg).hexdigest()
    assert m["content_type"] == "image/jpeg" and m["size_bytes"] == len(jpeg) and m["kind"] == "photo"
    assert m["url"].endswith(f"/api/v1/media/{m['id']}/file/")
    again = send()
    assert again.status_code == 200 and again.json()["id"] == m["id"]
    assert Media.objects.count() == 1
    download = field["client"].get(f"/api/v1/media/{m['id']}/file/")
    assert download.status_code == 200 and b"".join(download.streaming_content) == jpeg

    f = SimpleUploadedFile("a.jpg", jpeg, content_type="image/jpeg")
    r = field["client"].post("/api/v1/media/", {"file": f, "observation_client_uuid": new_uuid(), "kind": "photo"},
                             format="multipart")
    assert r.status_code == 404 and r.json()["error"]["code"] == "observation_not_found"
    f = SimpleUploadedFile("a.jpg", jpeg, content_type="image/jpeg")
    r = field["client"].post("/api/v1/media/", {"file": f, "observation_client_uuid": obs_id, "kind": "audio"},
                             format="multipart")
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_media_type"


def test_positions_and_latest(field):
    r = field["client"].post("/api/v1/positions/", {"pings": [
        {"recorded_at": "2026-09-15T06:00:00Z", "lat": -17.5, "lon": 30.95, "accuracy_m": 5, "battery_pct": 80},
        {"recorded_at": "2026-09-15T06:05:00Z", "lat": -17.501, "lon": 30.951, "accuracy_m": 5, "battery_pct": 79},
        {"recorded_at": "bad", "lat": -17.5, "lon": 30.95},
    ]}, format="json")
    assert r.status_code == 202
    assert r.json()["accepted"] == 2 and r.json()["rejected"][0]["index"] == 2
    assert PositionPing.objects.count() == 2
    latest = client_for(make_user(field["org"], "manager")).get("/api/v1/positions/latest/").json()
    assert len(latest) == 1 and latest[0]["battery_pct"] == 79


def test_gzip_request_body(field):
    import gzip
    import json

    payload = {"observations": [{"client_uuid": new_uuid(), "area_id": str(field["area"].pk), "category": "other",
                                 "lat": -17.5, "lon": 30.95, "recorded_at": "2026-09-15T06:15:00Z"}]}
    r = field["client"].generic("POST", "/api/v1/sync/push/", gzip.compress(json.dumps(payload).encode()),
                                content_type="application/json", HTTP_CONTENT_ENCODING="gzip")
    assert r.status_code == 200, r.content
    assert len(r.json()["accepted"]["observations"]) == 1


def test_researcher_cannot_sync(field):
    r = client_for(make_user(field["org"], "researcher")).get("/api/v1/sync/bootstrap/")
    assert r.status_code == 403
