"""Manager dashboard API (spec §7): roles, tenancy, live status, tracks, risk, coverage, alerts, grid preview."""
import csv
import io
from datetime import date, datetime, timedelta, timezone as dt_timezone

import pytest
from django.utils import timezone

from areas.models import Assignment, GrtsCell, RiskScore, Team
from areas.risk import score_area
from audit.models import AuditLog
from field.models import Observation, Patrol, PositionPing, SafetyAlert, TrackPoint
from notify.models import NotificationLog

from .conftest import client_for, make_area, make_org, make_user, new_uuid, utm_square

pytestmark = pytest.mark.django_db


@pytest.fixture
def ops():
    org = make_org("GRT")
    area = make_area(org, name="Mazowe")
    base = area.apu_bases.first()
    team = Team.objects.create(organisation=org, area=area, apu_base=base, name="River Team")
    now = timezone.now()
    r_active = make_user(org, "ranger", full_name="Active Ranger", team=team, apu_base=base, last_sync_at=now)
    r_paused = make_user(org, "ranger", full_name="Paused Ranger", team=team, last_sync_at=now - timedelta(hours=1))
    r_offline = make_user(org, "ranger", full_name="Offline Ranger", team=team, last_sync_at=now - timedelta(days=3))
    r_sos = make_user(org, "ranger", full_name="Sos Ranger", team=team, last_sync_at=now - timedelta(hours=2))
    for r in (r_active, r_paused, r_offline, r_sos):
        r.areas.add(area)
    cells = list(GrtsCell.objects.filter(area=area).order_by("grts_order"))
    c0 = cells[0].centroid["coordinates"]
    p_active = Patrol.objects.create(client_uuid=new_uuid(), organisation=org, ranger=r_active, team=team, area=area,
                                     started_at=now - timedelta(hours=1), status="active", distance_m=1234.4)
    TrackPoint.objects.bulk_create([
        TrackPoint(organisation=org, patrol=p_active, recorded_at=now - timedelta(minutes=50 - i * 10), lat=c0[1] + i * 1e-4,
                   lon=c0[0], cell=cells[0]) for i in range(5)])
    PositionPing.objects.create(organisation=org, ranger=r_active, patrol_client_uuid=p_active.pk,
                                recorded_at=now - timedelta(minutes=3), lat=c0[1], lon=c0[0], accuracy_m=5, battery_pct=77)
    PositionPing.objects.create(organisation=org, ranger=r_active, recorded_at=now - timedelta(hours=30), lat=c0[1],
                                lon=c0[0], battery_pct=99)
    Patrol.objects.create(client_uuid=new_uuid(), organisation=org, ranger=r_paused, team=team, area=area,
                          started_at=now - timedelta(hours=2), status="paused")
    Patrol.objects.create(client_uuid=new_uuid(), organisation=org, ranger=r_offline, team=team, area=area,
                          started_at=now - timedelta(hours=5), status="active")
    sos = SafetyAlert.objects.create(client_uuid=new_uuid(), organisation=org, ranger=r_sos, kind="panic", status="active",
                                     lat=c0[1], lon=c0[0], started_at=now - timedelta(minutes=5))
    snare = Observation.objects.create(client_uuid=new_uuid(), organisation=org, area=area, observer=r_active,
                                       patrol=p_active, category="threat", subtype="poacher_camp", severity="critical",
                                       alert_manager=True, lat=c0[1], lon=c0[0], cell=cells[0],
                                       recorded_at=now - timedelta(minutes=20))
    manager = make_user(org, "manager", full_name="Grace Manager")
    other = make_org("OTH")
    other_area = make_area(other, name="Other", boundary=utm_square(32.05, -20.25))
    return {"org": org, "area": area, "team": team, "cells": cells, "manager": manager, "mgr": client_for(manager),
            "r_active": r_active, "r_paused": r_paused, "r_offline": r_offline, "r_sos": r_sos, "p_active": p_active,
            "sos": sos, "snare": snare, "other": other, "other_area": other_area,
            "other_mgr": client_for(make_user(other, "manager"))}


def test_online_status_off_patrol(ops, settings):
    from accounts.models import AuthToken

    org, team, area = ops["org"], ops["team"], ops["area"]
    now = timezone.now()
    synced = make_user(org, "ranger", full_name="Synced Ranger", team=team, last_sync_at=now - timedelta(minutes=10))
    caller = make_user(org, "ranger", full_name="Caller Ranger", team=team, last_sync_at=now - timedelta(days=2))
    AuthToken.objects.create(user=caller, device_id="phone", last_used_at=now - timedelta(minutes=5))
    stale = make_user(org, "ranger", full_name="Stale Ranger", team=team, last_sync_at=now - timedelta(minutes=50))
    ended = Patrol.objects.create(client_uuid=new_uuid(), organisation=org, ranger=stale, team=team, area=area,
                                  started_at=now - timedelta(hours=2), ended_at=now - timedelta(hours=1), status="ended")
    PositionPing.objects.create(organisation=org, ranger=synced, patrol_client_uuid=ended.pk,
                                recorded_at=now - timedelta(hours=1), lat=-17.5, lon=30.95)

    rangers = {r["full_name"]: r["status"] for r in ops["mgr"].get("/api/v1/rangers/").json()}
    assert rangers["Synced Ranger"] == "online" and rangers["Caller Ranger"] == "online"
    assert rangers["Stale Ranger"] == "offline" and rangers["Offline Ranger"] == "offline"
    assert rangers["Active Ranger"] == "active"  # an open patrol with fresh pings still wins
    body = ops["mgr"].get("/api/v1/dashboard/summary/").json()
    assert (body["rangers_online"], body["rangers_offline"]) == (2, 2)
    assert body["rangers_total"] == sum(body[k] for k in ("rangers_active", "rangers_paused", "rangers_online",
                                                            "rangers_offline", "rangers_sos"))

    settings.RANGER_ONLINE_MINUTES = 3
    rangers = {r["full_name"]: r["status"] for r in ops["mgr"].get("/api/v1/rangers/").json()}
    assert rangers["Synced Ranger"] == "offline" and rangers["Caller Ranger"] == "offline"


def test_ranger_and_researcher_forbidden(ops):
    ranger = client_for(ops["r_active"])
    researcher = client_for(make_user(ops["org"], "researcher"))
    a = ops["area"].pk
    paths = ["/api/v1/dashboard/summary/", "/api/v1/rangers/", f"/api/v1/rangers/{ops['r_active'].pk}/",
             f"/api/v1/areas/{a}/coverage/", f"/api/v1/areas/{a}/coverage/export/", f"/api/v1/areas/{a}/risk/",
             f"/api/v1/areas/{a}/risk/trend/", f"/api/v1/positions/history/?ranger_id={ops['r_active'].pk}",
             f"/api/v1/patrols/{ops['p_active'].pk}/track/", f"/api/v1/alerts/{ops['sos'].pk}/"]
    for path in paths:
        assert ranger.get(path).status_code == 403, path
        assert researcher.get(path).status_code == 403, path
    r = ranger.post(f"/api/v1/alerts/{ops['sos'].pk}/dispatch/", {"responder_ids": [str(ops["r_paused"].pk)]}, format="json")
    assert r.status_code == 403
    assert ranger.post(f"/api/v1/rangers/{ops['r_paused'].pk}/message/", {"text": "hi"}, format="json").status_code == 403
    assert ranger.get("/api/v1/reports/").status_code == 403


def test_other_org_cannot_read(ops):
    c = ops["other_mgr"]
    a = ops["area"].pk
    assert c.get("/api/v1/rangers/").json() == []
    for path in [f"/api/v1/rangers/{ops['r_active'].pk}/", f"/api/v1/areas/{a}/coverage/", f"/api/v1/areas/{a}/risk/",
                 f"/api/v1/areas/{a}/risk/trend/", f"/api/v1/areas/{a}/coverage/export/",
                 f"/api/v1/patrols/{ops['p_active'].pk}/track/", f"/api/v1/alerts/{ops['sos'].pk}/",
                 f"/api/v1/positions/history/?ranger_id={ops['r_active'].pk}", f"/api/v1/dashboard/summary/?area_id={a}"]:
        r = c.get(path)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found", path
    assert c.post(f"/api/v1/alerts/{ops['sos'].pk}/dispatch/", {"responder_ids": [str(ops["r_paused"].pk)]},
                  format="json").status_code == 404
    assert c.post(f"/api/v1/rangers/{ops['r_active'].pk}/message/", {"text": "x"}, format="json").status_code == 404
    summary = c.get("/api/v1/dashboard/summary/").json()
    assert summary["rangers_total"] == 0 and summary["open_alerts"] == 0


def test_summary_and_ranger_status(ops):
    body = ops["mgr"].get("/api/v1/dashboard/summary/").json()
    assert body["rangers_total"] == 4
    assert (body["rangers_active"], body["rangers_paused"], body["rangers_offline"], body["rangers_sos"]) == (1, 1, 1, 1)
    assert body["open_alerts"] == 2 and body["critical_alerts"] == 2 and body["sos_active"] == 1
    assert body["observations_today"] >= 0 and body["server_time"].endswith("Z")
    # synced in 24 h: active, paused, sos; field activity in 7 days: all but the 3-day-old sync... which is also < 7 d.
    assert body["sync_rate_24h"] == 0.75
    assert set(body) >= {"area_id", "grts_coverage_month", "patrols_today", "last_ranger_sync_at"}

    rangers = {r["full_name"]: r for r in ops["mgr"].get("/api/v1/rangers/", {"area_id": str(ops["area"].pk)}).json()}
    assert {n: r["status"] for n, r in rangers.items()} == {
        "Active Ranger": "active", "Paused Ranger": "paused", "Offline Ranger": "offline", "Sos Ranger": "sos"}
    active = rangers["Active Ranger"]
    assert active["last_position"]["battery_pct"] == 77
    assert active["current_patrol"]["client_uuid"] == str(ops["p_active"].pk)
    assert active["current_patrol"]["distance_m"] == 1234
    assert active["team_name"] == "River Team" and active["apu_base_code"] == "APU-1"
    assert active["today"]["observations"] == 1 and ops["cells"][0].label in active["today"]["cells_visited"]
    assert rangers["Offline Ranger"]["last_position"] is None

    detail = ops["mgr"].get(f"/api/v1/rangers/{ops['r_active'].pk}/").json()
    assert detail["status"] == "active" and len(detail["recent_observations"]) == 1
    assert detail["alerts"][0]["id"] == str(ops["snare"].pk)


def test_ranger_message(ops):
    r = ops["mgr"].post(f"/api/v1/rangers/{ops['r_active'].pk}/message/", {"text": "Return to APU-1 <b>now</b>"}, format="json")
    assert r.status_code == 202 and r.json() == {"channel": "sms", "queued": True}
    log = NotificationLog.objects.get(recipient_id=ops["r_active"].pk)
    assert log.channel == "sms" and log.body == "Return to APU-1 now"
    ops["r_paused"].phone = ""
    ops["r_paused"].save()
    r = ops["mgr"].post(f"/api/v1/rangers/{ops['r_paused'].pk}/message/", {"text": "Check in"}, format="json")
    assert r.json()["channel"] == "push"
    assert NotificationLog.objects.get(recipient_id=ops["r_paused"].pk).to == f"user-{ops['r_paused'].pk}"
    assert ops["mgr"].post(f"/api/v1/rangers/{ops['r_active'].pk}/message/", {"text": "x" * 321},
                           format="json").status_code == 400
    assert ops["mgr"].post(f"/api/v1/rangers/{ops['r_active'].pk}/message/", {"text": "x", "urgent": True},
                           format="json").json()["error"]["code"] == "unexpected_fields"
    assert AuditLog.objects.filter(action="ranger.message").count() == 2


def test_track_and_position_history(ops):
    feat = ops["mgr"].get(f"/api/v1/patrols/{ops['p_active'].pk}/track/").json()
    assert feat["type"] == "Feature" and feat["geometry"]["type"] == "LineString"
    assert len(feat["geometry"]["coordinates"]) == 5 and len(feat["properties"]["times"]) == 5
    assert set(feat["properties"]) >= {"ranger_id", "started_at", "ended_at", "distance_m", "status"}
    assert feat["properties"]["status"] == "active" and feat["properties"]["ended_at"] is None

    rid = ops["r_active"].pk
    hist = ops["mgr"].get("/api/v1/positions/history/", {"ranger_id": str(rid)}).json()
    assert len(hist) == 1 and set(hist[0]) == {"recorded_at", "lat", "lon", "battery_pct"}
    since = (timezone.now() - timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = (timezone.now() - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hist = ops["mgr"].get("/api/v1/positions/history/", {"ranger_id": str(rid), "since": since, "until": until}).json()
    assert [h["battery_pct"] for h in hist] == [99]
    r = ops["mgr"].get("/api/v1/positions/history/", {"ranger_id": str(rid), "since": since})
    assert r.status_code == 200
    far = (timezone.now() - timedelta(hours=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = ops["mgr"].get("/api/v1/positions/history/", {"ranger_id": str(rid), "since": far, "until": until})
    assert r.status_code == 400 and r.json()["error"]["code"] == "window_too_large"
    assert ops["mgr"].get("/api/v1/positions/history/").json()["error"]["code"] == "validation_error"


def test_risk_map_and_trend_shape(ops):
    area = ops["area"]
    today = timezone.now().astimezone(__import__("zoneinfo").ZoneInfo(area.timezone)).date()
    score_area(area, today)
    score_area(area, today - timedelta(days=2))
    body = ops["mgr"].get(f"/api/v1/areas/{area.pk}/risk/").json()
    assert body["date"] == today.isoformat() and body["engine"] == "heuristic"
    assert body["model_confidence"] in {"low", "moderate", "high"}
    assert len(body["cells"]) == 24
    cell = body["cells"][0]
    assert set(cell) >= {"cell_id", "label", "sector_id", "score", "level", "factors", "centroid"}
    assert cell["centroid"]["type"] == "Point" and cell["factors"][0]["key"]
    # A date without scores falls back to the most recent scored date.
    assert ops["mgr"].get(f"/api/v1/areas/{area.pk}/risk/", {"date": (today - timedelta(days=1)).isoformat()}).json()["date"] \
        == (today - timedelta(days=2)).isoformat()

    trend = ops["mgr"].get(f"/api/v1/areas/{area.pk}/risk/trend/", {"days": 5}).json()
    assert [t["date"] for t in trend] == [(today - timedelta(days=4 - i)).isoformat() for i in range(5)]
    assert all(set(t) == {"date", "mean_score", "max_score", "high_cells", "critical_cells"} for t in trend)
    last = trend[-1]
    assert isinstance(last["mean_score"], float) and last["max_score"] >= last["mean_score"]
    assert last["high_cells"] == RiskScore.objects.filter(area=area, date=today, level="high").count()
    assert trend[-2]["mean_score"] is None and trend[-2]["high_cells"] == 0
    assert len(ops["mgr"].get(f"/api/v1/areas/{area.pk}/risk/trend/").json()) == 30
    assert ops["mgr"].get(f"/api/v1/areas/{area.pk}/risk/trend/", {"days": 0}).status_code == 400

    lic = ops["org"].licence
    lic.modules = ["grts"]
    lic.save()
    assert ops["mgr"].get(f"/api/v1/areas/{area.pk}/risk/").json()["error"]["code"] == "module_disabled"


def _visit(org, area, ranger, cell, when):
    lon, lat = cell.centroid["coordinates"]
    p = Patrol.objects.create(client_uuid=new_uuid(), organisation=org, ranger=ranger, area=area, started_at=when,
                              ended_at=when + timedelta(hours=2), status="ended")
    TrackPoint.objects.create(organisation=org, patrol=p, recorded_at=when + timedelta(minutes=10), lat=lat, lon=lon, cell=cell)
    TrackPoint.objects.create(organisation=org, patrol=p, recorded_at=when + timedelta(minutes=40), lat=lat, lon=lon, cell=cell)


def test_coverage_status_computation_and_export():
    org = make_org()
    area = make_area(org)
    ranger = make_user(org, "ranger")
    manager = client_for(make_user(org, "manager"))
    team = Team.objects.create(organisation=org, area=area, name="T")
    Assignment.objects.create(organisation=org, team=team, area=area, date=date(2026, 8, 1), visit_target=2)
    cells = list(GrtsCell.objects.filter(area=area).order_by("grts_order"))
    utc = dt_timezone.utc
    _visit(org, area, ranger, cells[0], datetime(2026, 8, 3, 6, tzinfo=utc))
    _visit(org, area, ranger, cells[0], datetime(2026, 8, 10, 6, tzinfo=utc))
    _visit(org, area, ranger, cells[0], datetime(2026, 8, 10, 12, tzinfo=utc))  # same day: not a new visit
    _visit(org, area, ranger, cells[1], datetime(2026, 8, 5, 6, tzinfo=utc))
    _visit(org, area, ranger, cells[2], datetime(2026, 7, 20, 6, tzinfo=utc))
    lon, lat = cells[3].centroid["coordinates"]
    Observation.objects.create(client_uuid=new_uuid(), organisation=org, area=area, observer=ranger, category="wildlife",
                               lat=lat, lon=lon, cell=cells[3], recorded_at=datetime(2026, 8, 20, 9, tzinfo=utc))
    _visit(org, area, ranger, cells[4], datetime(2026, 9, 2, 6, tzinfo=utc))  # after the month: still "never" for August

    body = manager.get(f"/api/v1/areas/{area.pk}/coverage/", {"month": "2026-08"}).json()
    assert body["month"] == "2026-08" and body["visit_target"] == 2
    status = {c["label"]: c for c in body["cells"]}
    assert status[cells[0].label]["status"] == "complete" and status[cells[0].label]["visits"] == 2
    assert status[cells[1].label]["status"] == "partial"
    assert status[cells[2].label]["status"] == "pending" and status[cells[2].label]["last_visit_at"].startswith("2026-07-20")
    assert status[cells[3].label]["status"] == "partial" and status[cells[3].label]["observations"] == 1
    assert status[cells[4].label]["status"] == "never" and status[cells[4].label]["last_visit_at"] is None
    assert body["coverage_pct"] == round(1 / 24, 4) and body["never_surveyed"] == 20
    assert body["mean_visits"] == round(4 / 24, 2)
    assert body["season_coverage_pct"] == round(4 / 24, 4)  # dry season May–Aug: cells 0–3
    sector = body["sectors"][0]
    assert (sector["cells"], sector["complete"], sector["partial"], sector["pending"], sector["never"]) == (24, 1, 2, 1, 20)
    assert body["cells"][0]["status"] in {"complete", "partial", "pending", "never"}
    assert manager.get(f"/api/v1/areas/{area.pk}/coverage/", {"month": "2026-08", "visit_target": 1}).json()["coverage_pct"] \
        == round(3 / 24, 4)
    assert manager.get(f"/api/v1/areas/{area.pk}/coverage/", {"month": "Aug"}).status_code == 400

    r = manager.get(f"/api/v1/areas/{area.pk}/coverage/export/", {"month": "2026-08"})
    assert r.status_code == 200 and r["Content-Type"].startswith("text/csv")
    assert r["Content-Disposition"].startswith("attachment;") and "2026-08" in r["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8"))))
    assert len(rows) == 24 and {row["cell_label"]: row["status"] for row in rows}[cells[0].label] == "complete"


def test_dispatch_notifies_responders_and_builds_timeline(ops):
    responder_sms = ops["r_paused"]
    responder_push = ops["r_offline"]
    responder_push.phone = ""
    responder_push.save()
    url = f"/api/v1/alerts/{ops['sos'].pk}/dispatch/"
    r = ops["mgr"].post(url, {"note": "Go to last fix", "responder_ids": [str(responder_sms.pk), str(responder_push.pk)]},
                        format="json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["status"] == "acknowledged" and body["dispatched_at"]
    assert {x["id"] for x in body["responders"]} == {str(responder_sms.pk), str(responder_push.pk)}
    assert [t["action"] for t in body["timeline"]] == ["raised", "acknowledged", "dispatched"]
    assert body["timeline"][2]["actor_name"] == "Grace Manager" and body["timeline"][2]["note"] == "Go to last fix"
    logs = NotificationLog.objects.filter(title="PATROLIQ DISPATCH")
    assert {(n.recipient_id, n.channel) for n in logs} == {(responder_sms.pk, "sms"), (responder_push.pk, "push")}
    assert "Sos Ranger" in logs.first().body
    assert AuditLog.objects.filter(action="alert.dispatch", target_id=str(ops["sos"].pk)).exists()

    feed = {a["id"]: a for a in ops["mgr"].get("/api/v1/alerts/").json()}
    assert feed[str(ops["sos"].pk)]["dispatched_at"] == body["dispatched_at"]
    detail = ops["mgr"].get(f"/api/v1/alerts/{ops['sos'].pk}/").json()
    assert detail["timeline"] == body["timeline"] and detail["signal_level"] is None

    # Threat alerts can be dispatched too; foreign/unknown responders are rejected.
    r = ops["mgr"].post(f"/api/v1/alerts/{ops['snare'].pk}/dispatch/", {"responder_ids": [new_uuid()]}, format="json")
    assert r.status_code == 400 and "responder_ids" in r.json()["error"]["fields"]
    r = ops["mgr"].post(f"/api/v1/alerts/{ops['snare'].pk}/dispatch/", {"responder_ids": [str(responder_sms.pk)]}, format="json")
    assert r.status_code == 200 and r.json()["type"] == "threat" and r.json()["cell_label"] == ops["cells"][0].label

    ops["mgr"].post(f"/api/v1/alerts/{ops['sos'].pk}/resolve/", {"note": "Safe"}, format="json")
    detail = ops["mgr"].get(f"/api/v1/alerts/{ops['sos'].pk}/").json()
    assert detail["timeline"][-1]["action"] == "resolved" and detail["timeline"][-1]["note"] == "Safe"
    r = ops["mgr"].post(url, {"responder_ids": [str(responder_sms.pk)]}, format="json")
    assert r.status_code == 409 and r.json()["error"]["code"] == "alert_closed"
    assert ops["mgr"].get(f"/api/v1/alerts/{new_uuid()}/").status_code == 404


def test_grid_dry_run_does_not_save():
    org = make_org()
    area = make_area(org, grid=False, status="draft")
    admin = client_for(make_user(org, "org_admin"))
    r = admin.post(f"/api/v1/areas/{area.pk}/grid/generate/", {"cell_size_m": 1000, "dry_run": True}, format="json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["cells_created"] == 24 and body["sectors_created"] == 1
    assert body["cells"]["type"] == "FeatureCollection" and len(body["cells"]["features"]) == 24
    feat = body["cells"]["features"][0]
    assert feat["geometry"]["type"] == "Polygon" and feat["properties"]["label"] == "GRTS-001"
    assert GrtsCell.objects.filter(area=area).count() == 0 and not area.sectors.exists()
    # With an in-use grid a preview is still allowed and leaves the grid untouched.
    admin.post(f"/api/v1/areas/{area.pk}/grid/generate/", {"cell_size_m": 1000}, format="json")
    cell = GrtsCell.objects.filter(area=area).first()
    lon, lat = cell.centroid["coordinates"]
    Observation.objects.create(client_uuid=new_uuid(), organisation=org, area=area, observer=make_user(org, "ranger"),
                               category="other", lat=lat, lon=lon, cell=cell, recorded_at=timezone.now())
    r = admin.post(f"/api/v1/areas/{area.pk}/grid/generate/", {"cell_size_m": 500, "dry_run": True}, format="json")
    assert r.status_code == 200 and r.json()["cells_created"] == 100
    assert GrtsCell.objects.filter(area=area).count() == 24


def test_area_setup_counters_and_cells_geojson():
    org = make_org()
    area = make_area(org)
    draft = make_area(org, grid=False, bases=[], status="draft", boundary=utm_square(31.3, -17.5, 2, 2))
    Team.objects.create(organisation=org, area=area, name="T")
    c = client_for(make_user(org, "manager"))
    items = {a["id"]: a for a in c.get("/api/v1/areas/").json()}
    a = items[str(area.pk)]
    assert (a["apu_base_count"], a["cell_count"], a["team_count"], a["sector_count"]) == (1, 24, 1, 1)
    assert a["setup"] == {"boundary": True, "bases": True, "grid": True, "teams": True}
    assert items[str(draft.pk)]["setup"] == {"boundary": True, "bases": False, "grid": False, "teams": False}
    assert c.get(f"/api/v1/areas/{area.pk}/").json()["cell_count"] == 24
    fc = c.get(f"/api/v1/areas/{area.pk}/cells/").json()
    assert fc["type"] == "FeatureCollection" and len(fc["features"]) == 24
    assert set(fc["features"][0]["properties"]) >= {"id", "label", "grts_order", "sector_id"}


def test_seeded_demo_summary():
    from django.core.management import call_command

    call_command("seed_demo", "--allow-production", stdout=io.StringIO())
    from accounts.models import User
    from dashboard.models import Report

    grace = User.objects.get(email="grace.mutasa@grtts.co.zw")
    c = client_for(grace)
    body = c.get("/api/v1/dashboard/summary/").json()
    assert body["rangers_total"] == 9
    assert (body["rangers_active"], body["rangers_paused"], body["rangers_offline"], body["rangers_sos"]) == (1, 1, 7, 0)
    assert body["open_alerts"] == 2 and body["critical_alerts"] == 1 and body["sos_active"] == 0
    assert 0 < body["sync_rate_24h"] < 1 and 0 < body["grts_coverage_month"] <= 1
    if timezone.now().astimezone(__import__("zoneinfo").ZoneInfo("Africa/Harare")).hour >= 5:  # live patrols started today
        assert body["observations_today"] >= 1 and body["patrols_today"] >= 1
    rangers = {r["full_name"]: r["status"] for r in c.get("/api/v1/rangers/").json()}
    assert rangers["Tendai Moyo"] == "active" and rangers["Sipho Ndlovu"] == "paused"
    assert rangers["Precious Mpofu"] == "offline" and "Blessing Dube" not in rangers  # SVT ranger
    assert [r["status"] for r in c.get("/api/v1/reports/").json()] == ["ready", "ready"]
    kinds = {(a["kind"], a["status"]) for a in c.get("/api/v1/alerts/").json()}
    assert {("poacher_camp", "active"), ("elephant_carcass", "acknowledged"), ("dead_mans_switch", "resolved")} <= kinds
    area_id = c.get("/api/v1/areas/").json()[0]["id"]
    trend = c.get(f"/api/v1/areas/{area_id}/risk/trend/").json()
    assert all(t["mean_score"] is not None for t in trend)

    counts = (Patrol.objects.count(), TrackPoint.objects.count(), Observation.objects.count(), PositionPing.objects.count(),
              SafetyAlert.objects.count(), Report.objects.count(), RiskScore.objects.count())
    call_command("seed_demo", "--allow-production", stdout=io.StringIO())  # idempotent
    assert counts == (Patrol.objects.count(), TrackPoint.objects.count(), Observation.objects.count(),
                      PositionPing.objects.count(), SafetyAlert.objects.count(), Report.objects.count(),
                      RiskScore.objects.count())


def test_summary_separates_hwc_from_sos(ops):
    """An HWC alert is open and urgent but is neither an SOS nor (by default) critical (spec v1.5 §A5)."""
    org, area = ops["org"], ops["area"]
    hwc = SafetyAlert.objects.create(client_uuid=new_uuid(), organisation=org, ranger=ops["r_offline"],
                                     kind=SafetyAlert.HWC, status="active", area=area,
                                     lat=-17.48, lon=30.95, started_at=timezone.now() - timedelta(minutes=2),
                                     details={"conflict_type": "crop_raiding", "species_name": "African Elephant"})
    body = ops["mgr"].get("/api/v1/dashboard/summary/").json()
    assert body["open_alerts"] == 3 and body["sos_active"] == 1 and body["hwc_active"] == 1
    assert body["critical_alerts"] == 2  # the panic alert and the poacher-camp threat only
    # The HWC alert does not move its ranger into the sos bucket.
    assert (body["rangers_sos"], body["rangers_offline"]) == (1, 1)

    feed = {a["id"]: a for a in ops["mgr"].get("/api/v1/alerts/").json()}
    item = feed[str(hwc.pk)]
    assert item["severity"] == "high" and item["area_id"] == str(area.pk)
    assert item["title"] == "Human–wildlife conflict · African Elephant · crop raiding"
    assert feed[str(ops["sos"].pk)]["title"] == "Panic button" and feed[str(ops["sos"].pk)]["severity"] == "critical"
    assert feed[str(ops["sos"].pk)]["area_id"] is None

    # Human casualties raise it to critical.
    hwc.details = {**hwc.details, "people_injured": 1}
    hwc.save(update_fields=["details"])
    body = ops["mgr"].get("/api/v1/dashboard/summary/").json()
    assert body["critical_alerts"] == 3 and body["hwc_active"] == 1

    # Resolving closes it.
    assert ops["mgr"].post(f"/api/v1/alerts/{hwc.pk}/resolve/", {"note": "done"}, format="json").status_code == 200
    assert ops["mgr"].get("/api/v1/dashboard/summary/").json()["hwc_active"] == 0


def test_alerts_area_filter_keeps_arealess_safety_alerts(ops):
    org, area = ops["org"], ops["area"]
    hwc = SafetyAlert.objects.create(client_uuid=new_uuid(), organisation=org, ranger=ops["r_paused"],
                                     kind=SafetyAlert.HWC, status="active", area=area,
                                     lat=-17.48, lon=30.95, started_at=timezone.now())
    ids = {a["id"] for a in ops["mgr"].get("/api/v1/alerts/", {"area_id": str(area.pk)}).json()}
    assert {str(hwc.pk), str(ops["sos"].pk), str(ops["snare"].pk)} == ids

    elsewhere = make_area(org, name="Far side", boundary=utm_square(31.55, -18.10))
    ids = {a["id"] for a in ops["mgr"].get("/api/v1/alerts/", {"area_id": str(elsewhere.pk)}).json()}
    assert ids == {str(ops["sos"].pk)}  # the SOS has no area, so it is never filtered out
