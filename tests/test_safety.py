from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import AuthToken
from audit.models import AuditLog
from field.models import AlertEvent, SafetyAlert
from notify.models import NotificationLog

from .conftest import client_for, make_org, make_user, new_uuid

pytestmark = pytest.mark.django_db


@pytest.fixture
def ctx():
    org = make_org()
    other = make_org()
    ranger = make_user(org, "ranger", full_name="Tendai Moyo")
    manager = make_user(org, "manager", phone="+263771111111")
    admin = make_user(org, "org_admin", phone="+263772222222")
    make_user(other, "manager")  # must NOT be notified
    return {"org": org, "ranger": ranger, "manager": manager, "admin": admin, "client": client_for(ranger)}


def sos(ctx, **extra):
    body = {"client_uuid": new_uuid(), "ranger_id": str(ctx["ranger"].pk), "kind": "panic", "status": "active",
            "lat": -17.4712, "lon": 30.9153, "accuracy_m": 6.0, "battery_pct": 54, "signal_level": 2,
            "started_at": "2026-09-15T09:12:00Z", **extra}
    return body, ctx["client"].post("/api/v1/safety/alerts/", body, format="json")


def test_sos_notifies_managers_and_audits(ctx):
    body, r = sos(ctx)
    assert r.status_code == 201, r.content
    out = r.json()
    assert out["client_uuid"] == body["client_uuid"] and out["status"] == "active" and out["kind"] == "panic"

    sms = NotificationLog.objects.filter(channel="sms")
    assert {n.recipient_id for n in sms} == {ctx["manager"].pk, ctx["admin"].pk}
    assert all(n.organisation_id == ctx["org"].pk for n in NotificationLog.objects.all())
    assert NotificationLog.objects.filter(channel="push", to=f"org-{ctx['org'].pk}-managers").count() == 1
    msg = sms.first()
    assert "PANIC" in msg.title and "Tendai Moyo" in msg.body and "-17.47120" in msg.body and "54%" in msg.body

    entry = AuditLog.objects.get(action="safety_alert.create")
    assert entry.target_id == body["client_uuid"] and entry.actor_id == ctx["ranger"].pk
    assert entry.organisation_id == ctx["org"].pk


def test_sos_replay_is_idempotent(ctx):
    body, _ = sos(ctx)
    r = ctx["client"].post("/api/v1/safety/alerts/", {**body, "lat": -17.48, "battery_pct": 50}, format="json")
    assert r.status_code == 200
    assert SafetyAlert.objects.count() == 1 and SafetyAlert.objects.get().battery_pct == 50
    assert NotificationLog.objects.filter(channel="sms").count() == 2


def test_sos_is_lenient_and_ignores_idle_expiry(ctx):
    AuthToken.objects.filter(pk=ctx["client"].token.pk).update(last_used_at=timezone.now() - timedelta(days=60))
    r = ctx["client"].post("/api/v1/safety/alerts/", {"client_uuid": new_uuid(), "future_field": "x"}, format="json")
    assert r.status_code == 201, r.content
    assert r.json()["kind"] == "panic" and r.json()["lat"] is None
    assert ctx["client"].get("/api/v1/me/").json()["error"]["code"] == "token_expired"


def test_cancel_requires_pin_and_notifies(ctx):
    body, _ = sos(ctx)
    url = f"/api/v1/safety/alerts/{body['client_uuid']}/cancel/"
    r = ctx["client"].post(url, {"pin_verified": False}, format="json")
    assert r.status_code == 400 and r.json()["error"]["code"] == "pin_not_verified"

    other_ranger = client_for(make_user(ctx["org"], "ranger"))
    assert other_ranger.post(url, {"pin_verified": True}, format="json").status_code == 404

    r = ctx["client"].post(url, {"pin_verified": True, "note": "False alarm <i>sorry</i>"}, format="json")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled" and r.json()["resolution_note"] == "False alarm sorry"
    assert r.json()["resolved_at"]
    assert AuditLog.objects.filter(action="safety_alert.cancel", target_id=body["client_uuid"]).exists()
    assert NotificationLog.objects.filter(title="PATROLIQ SOS CANCELLED", channel="sms").count() == 2


def test_manager_alert_feed_and_acknowledge(ctx):
    body, _ = sos(ctx, kind="dead_mans_switch")
    mgr = client_for(ctx["manager"])
    feed = mgr.get("/api/v1/alerts/").json()
    assert feed[0]["id"] == body["client_uuid"] and feed[0]["type"] == "safety"
    r = mgr.post(f"/api/v1/alerts/{body['client_uuid']}/acknowledge/", {}, format="json")
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"
    assert mgr.get("/api/v1/alerts/", {"status": "active"}).json() == []
    assert ctx["client"].get("/api/v1/alerts/").status_code == 403


# --- human-wildlife conflict (spec v1.5 §A) ---------------------------------------------------

def hwc(ctx, client_uuid=None, **extra):
    body = {"client_uuid": client_uuid or new_uuid(), "kind": "human_wildlife_conflict",
            "lat": -17.4712, "lon": 30.9153, "accuracy_m": 8.0, "battery_pct": 61,
            "started_at": "2026-09-15T09:12:00Z", **extra}
    return body, ctx["client"].post("/api/v1/safety/alerts/", body, format="json")


def test_hwc_raise_notifies_managers_and_keeps_ranger_off_sos(ctx):
    body, r = hwc(ctx)
    assert r.status_code == 201, r.content
    out = r.json()
    assert out["kind"] == "human_wildlife_conflict" and out["status"] == "active"
    assert out["details"] is None and out["details_updated_at"] is None and out["area_id"] is None

    sms = NotificationLog.objects.filter(channel="sms")
    assert {n.recipient_id for n in sms} == {ctx["manager"].pk, ctx["admin"].pk}
    msg = sms.first()
    assert msg.title == "PATROLIQ HUMAN-WILDLIFE CONFLICT" and "Tendai Moyo" in msg.body
    assert msg.data == {"type": "safety", "kind": "human_wildlife_conflict", "client_uuid": body["client_uuid"]}

    mgr = client_for(ctx["manager"])
    item = mgr.get("/api/v1/alerts/").json()[0]
    assert item["severity"] == "high" and item["title"] == "Human–wildlife conflict"
    summary = mgr.get("/api/v1/dashboard/summary/").json()
    assert summary["hwc_active"] == 1 and summary["sos_active"] == 0 and summary["open_alerts"] == 1
    assert summary["critical_alerts"] == 0 and summary["rangers_sos"] == 0


def test_hwc_details_merge_notify_once_and_severity(ctx):
    cu = new_uuid()
    _, r = hwc(ctx, client_uuid=cu)
    assert r.status_code == 201
    NotificationLog.objects.all().delete()

    first = {"client_uuid": cu, "kind": "human_wildlife_conflict", "lat": -17.4713, "lon": 30.9154,
             "details": {"conflict_type": "crop_raiding", "species_name": "African Elephant",
                         "animal_count": 7, "location_description": "Chiweshe ward 12",
                         "reporter_name": "M. Chibanda", "logged_at": "2026-09-15T10:00:00Z"}}
    r = ctx["client"].post("/api/v1/safety/alerts/", first, format="json")
    assert r.status_code == 200, r.content
    out = r.json()
    assert out["details"]["conflict_type"] == "crop_raiding" and out["details"]["animal_count"] == 7
    assert out["details_updated_at"] and "details_warnings" not in out
    assert out["lat"] == -17.4713  # position refreshed alongside the details

    note = NotificationLog.objects.filter(channel="sms").first()
    assert note.title == "PATROLIQ HWC DETAILS" and "crop raiding" in note.body
    assert "African Elephant" in note.body and "Chiweshe ward 12" in note.body
    assert AlertEvent.objects.filter(alert_id=cu, action="note", note="Details logged").count() == 1

    NotificationLog.objects.all().delete()
    r = ctx["client"].post("/api/v1/safety/alerts/", {
        "client_uuid": cu, "kind": "human_wildlife_conflict",
        "details": {"people_injured": 2, "action_taken": "PWMA rangers dispatched"}}, format="json")
    assert r.status_code == 200
    merged = r.json()["details"]
    assert merged["species_name"] == "African Elephant" and merged["people_injured"] == 2
    assert merged["action_taken"] == "PWMA rangers dispatched"
    assert NotificationLog.objects.count() == 0  # later updates do not notify
    assert AlertEvent.objects.filter(alert_id=cu, action="note", note="Details updated").count() == 1

    mgr = client_for(ctx["manager"])
    item = mgr.get("/api/v1/alerts/").json()[0]
    assert item["severity"] == "critical"  # people_injured > 0
    assert item["title"] == "Human–wildlife conflict · African Elephant · crop raiding"
    detail = mgr.get(f"/api/v1/alerts/{cu}/").json()
    assert detail["details"]["animal_count"] == 7 and detail["details_updated_at"]
    assert detail["timeline"][0]["note"] == "Human–wildlife conflict reported"
    assert [e["note"] for e in detail["timeline"] if e["action"] == "note"] == ["Details logged", "Details updated"]
    assert mgr.get("/api/v1/dashboard/summary/").json()["critical_alerts"] == 1


def test_hwc_species_id_resolves_to_a_name(ctx):
    from field.models import Species

    elephant = Species.objects.get(scientific_name="Loxodonta africana")
    _, r = hwc(ctx, details={"species_id": str(elephant.pk), "conflict_type": "crop_raiding"})
    assert r.status_code == 201
    assert r.json()["details"]["species_name"] == elephant.common_name


def test_hwc_is_never_rejected_by_bad_details(ctx):
    _, r = hwc(ctx, details={
        "conflict_type": "alien_abduction", "animal_count": "many", "people_injured": -4,
        "species_id": "not-a-uuid", "logged_at": "yesterday", "notes": "<script>bad()</script>Maize destroyed",
        "moon_phase": 0.4, "reporter_phone": "+263771234567"})
    assert r.status_code == 201, r.content
    out = r.json()
    assert out["details"] == {"notes": "Maize destroyed", "reporter_phone": "+263771234567"}
    assert set(out["details_warnings"]) == {
        "moon_phase: unknown field",
        "conflict_type: must be one of crop_raiding, livestock_attack, human_injury, human_death, "
        "property_damage, animal_in_settlement, animal_injured, other",
        "species_id: expected a UUID", "animal_count: expected an integer",
        "people_injured: must be between 0 and 1000000", "logged_at: expected an ISO-8601 datetime"}

    r = ctx["client"].post("/api/v1/safety/alerts/", {"client_uuid": new_uuid(),
                                                      "kind": "human_wildlife_conflict",
                                                      "details": "not an object"}, format="json")
    assert r.status_code == 201 and r.json()["details_warnings"] == ["details: expected an object"]
    assert SafetyAlert.objects.count() == 2


def test_hwc_details_ignored_after_cancel_and_for_panic(ctx):
    cu = new_uuid()
    hwc(ctx, client_uuid=cu)
    ctx["client"].post(f"/api/v1/safety/alerts/{cu}/cancel/", {"pin_verified": True}, format="json")
    r = ctx["client"].post("/api/v1/safety/alerts/", {"client_uuid": cu, "kind": "human_wildlife_conflict",
                                                      "details": {"conflict_type": "other"}}, format="json")
    assert r.status_code == 200
    assert r.json()["details"] is None
    assert r.json()["details_warnings"] == ["details: ignored because the alert is cancelled"]

    panic = new_uuid()
    sos(ctx, client_uuid=panic)
    r = ctx["client"].post("/api/v1/safety/alerts/", {"client_uuid": panic, "kind": "panic",
                                                      "details": {"conflict_type": "other"}}, format="json")
    assert r.status_code == 200 and r.json()["details"] is None
    assert r.json()["details_warnings"] == ["details: ignored for this alert kind"]


def test_hwc_area_scoping_and_tenancy(ctx):
    from .conftest import make_area

    area = make_area(ctx["org"], name="Mazowe")
    other_area = make_area(make_org(), name="Elsewhere")
    cu = new_uuid()
    _, r = hwc(ctx, client_uuid=cu, area_id=str(area.pk))
    assert r.status_code == 201 and r.json()["area_id"] == str(area.pk)

    # An area belonging to another organisation is silently dropped, never a rejection.
    stranger = new_uuid()
    _, r = hwc(ctx, client_uuid=stranger, area_id=str(other_area.pk))
    assert r.status_code == 201 and r.json()["area_id"] is None

    mgr = client_for(ctx["manager"])
    assert {a["id"] for a in mgr.get("/api/v1/alerts/", {"area_id": str(area.pk)}).json()} == {cu, stranger}
    assert [a["id"] for a in mgr.get("/api/v1/alerts/", {"area_id": str(other_area.pk)}).json()] == [stranger]

    outsider = client_for(make_user(make_org(), "manager"))
    assert outsider.get(f"/api/v1/alerts/{cu}/").status_code == 404
    assert outsider.get("/api/v1/alerts/").json() == []


def test_hwc_acknowledge_dispatch_resolve(ctx):
    cu = new_uuid()
    hwc(ctx, client_uuid=cu, details={"conflict_type": "livestock_attack", "species_name": "Lion"})
    mgr = client_for(ctx["manager"])
    r = mgr.post(f"/api/v1/alerts/{cu}/acknowledge/", {"note": "Calling the PAC team"}, format="json")
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"
    assert mgr.get("/api/v1/dashboard/summary/").json()["hwc_active"] == 1  # acknowledged is still open

    r = mgr.post(f"/api/v1/alerts/{cu}/dispatch/", {"responder_ids": [str(ctx["admin"].pk)], "note": "Go"},
                 format="json")
    assert r.status_code == 200 and [x["id"] for x in r.json()["responders"]] == [str(ctx["admin"].pk)]

    r = mgr.post(f"/api/v1/alerts/{cu}/resolve/", {"note": "Herd driven back"}, format="json")
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    assert mgr.get("/api/v1/dashboard/summary/").json()["hwc_active"] == 0

    # Details may still be logged against a resolved alert.
    r = ctx["client"].post("/api/v1/safety/alerts/", {"client_uuid": cu, "kind": "human_wildlife_conflict",
                                                      "details": {"livestock_lost": 3}}, format="json")
    assert r.status_code == 200 and r.json()["details"]["livestock_lost"] == 3


def test_hwc_through_sync_push(ctx):
    cu = new_uuid()
    r = ctx["client"].post("/api/v1/sync/push/", {"safety_alerts": [
        {"client_uuid": cu, "kind": "human_wildlife_conflict", "lat": -17.47, "lon": 30.91,
         "details": {"conflict_type": "property_damage", "people_affected": 4}}]}, format="json")
    assert r.status_code == 200, r.content
    assert r.json()["accepted"]["safety_alerts"] == [cu]
    alert = SafetyAlert.objects.get(pk=cu)
    assert alert.kind == "human_wildlife_conflict" and alert.details["people_affected"] == 4
