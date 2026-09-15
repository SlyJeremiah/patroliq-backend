from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import AuthToken
from audit.models import AuditLog
from field.models import SafetyAlert
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
