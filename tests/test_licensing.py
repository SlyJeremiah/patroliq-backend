from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.licensing import effective_status
from accounts.models import Licence
from field.models import SafetyAlert

from .conftest import client_for, make_area, make_org, make_user, new_uuid

pytestmark = pytest.mark.django_db


def test_ranger_seat_limit_returns_402():
    org = make_org(max_rangers=2)
    admin = make_user(org, "org_admin")
    make_user(org, "ranger")
    c = client_for(admin)
    r = c.post("/api/v1/users/", {"employee_id": "RGR-9", "full_name": "Second", "role": "ranger"}, format="json")
    assert r.status_code == 201, r.content
    assert r.json()["temporary_password"]
    r = c.post("/api/v1/users/", {"employee_id": "RGR-10", "full_name": "Third", "role": "ranger"}, format="json")
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "licence_seat_limit"
    # Inactive users do not consume seats.
    r = c.post("/api/v1/users/", {"employee_id": "RGR-11", "full_name": "Reserve", "role": "ranger", "is_active": False},
               format="json")
    assert r.status_code == 201


def test_manager_seat_limit_and_totp_secret_issued():
    org = make_org(max_managers=2)
    admin = make_user(org, "org_admin")
    c = client_for(admin)
    r = c.post("/api/v1/users/", {"email": "m1@example.org", "full_name": "M1", "role": "manager"}, format="json")
    assert r.status_code == 201
    assert r.json()["totp_secret"] and r.json()["totp_uri"].startswith("otpauth://")
    r = c.post("/api/v1/users/", {"email": "m2@example.org", "full_name": "M2", "role": "viewer"}, format="json")
    assert r.status_code == 402 and r.json()["error"]["code"] == "licence_seat_limit"


def test_area_limit_returns_402():
    org = make_org(max_areas=1)
    admin = make_user(org, "org_admin")
    c = client_for(admin)
    assert c.post("/api/v1/areas/", {"name": "One", "area_type": "conservancy"}, format="json").status_code == 201
    r = c.post("/api/v1/areas/", {"name": "Two", "area_type": "conservancy"}, format="json")
    assert r.status_code == 402 and r.json()["error"]["code"] == "licence_area_limit"


def test_status_transitions():
    org = make_org()
    lic = org.licence
    assert effective_status(org) == "active"
    lic.expires_at = timezone.now() - timedelta(days=1)
    lic.grace_days = 14
    lic.save()
    assert effective_status(org) == "grace"
    lic.expires_at = timezone.now() - timedelta(days=15)
    lic.save()
    assert effective_status(org) == "suspended"


def test_grace_still_syncs():
    org = make_org(expires_at=timezone.now() - timedelta(days=2), grace_days=14)
    ranger = make_user(org, "ranger")
    c = client_for(ranger)
    r = c.get("/api/v1/sync/bootstrap/")
    assert r.status_code == 200
    assert r.json()["licence"]["status"] == "grace"


def test_suspended_licence_refuses_login_but_accepts_sos():
    org = make_org(expires_at=timezone.now() - timedelta(days=60), grace_days=14)
    area = make_area(org)
    ranger = make_user(org, "ranger", employee_id="RGR-1")
    make_user(org, "manager")
    r = client_for().post("/api/v1/auth/login/", {"organisation_code": org.code, "employee_id": "RGR-1",
                                                  "password": "patrol123", "device_id": "d"}, format="json")
    assert r.status_code == 403 and r.json()["error"]["code"] == "licence_suspended"

    c = client_for(ranger)  # token issued while the licence was still valid
    r = c.get("/api/v1/sync/bootstrap/")
    assert r.status_code == 403 and r.json()["error"]["code"] == "licence_suspended"

    r = c.post("/api/v1/safety/alerts/", {"client_uuid": new_uuid(), "kind": "panic", "lat": -17.5, "lon": 30.95,
                                          "battery_pct": 40, "started_at": "2026-09-15T08:00:00Z"}, format="json")
    assert r.status_code == 201, r.content
    assert r.json()["status"] == "active"

    alert_id = new_uuid()
    r = c.post("/api/v1/sync/push/", {
        "observations": [{"client_uuid": new_uuid(), "area_id": str(area.pk), "category": "other", "lat": -17.5,
                          "lon": 30.95, "recorded_at": "2026-09-15T08:00:00Z"}],
        "safety_alerts": [{"client_uuid": alert_id, "kind": "dead_mans_switch", "lat": -17.5, "lon": 30.95}],
    }, format="json")
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"]["safety_alerts"] == [alert_id]
    assert body["accepted"]["observations"] == []
    assert body["rejected"][0]["code"] == "licence_suspended"
    assert SafetyAlert.objects.filter(organisation=org).count() == 2


def test_manually_suspended_org():
    org = make_org()
    ranger = make_user(org, "ranger")
    org.status = "suspended"
    org.save()
    r = client_for(ranger).get("/api/v1/areas/")
    assert r.status_code == 403 and r.json()["error"]["code"] == "licence_suspended"
    # me/ still works so the app can show why.
    assert client_for(ranger).get("/api/v1/me/").json()["organisation"]["status"] == "suspended"


def test_module_disabled_returns_403():
    org = make_org(modules=["ai_risk"])
    area = make_area(org, grid=False, status="draft")
    admin = make_user(org, "org_admin")
    r = client_for(admin).post(f"/api/v1/areas/{area.pk}/grid/generate/", {"cell_size_m": 1000}, format="json")
    assert r.status_code == 403 and r.json()["error"]["code"] == "module_disabled"


def test_platform_admin_provisions_org_and_licence():
    platform = make_user(None, "platform_admin", email="ops@zrgis.example")
    c = client_for(platform)
    r = c.post("/api/v1/platform/organisations/", {
        "name": "Hwange Trust", "code": "hwt", "country": "Zimbabwe",
        "licence": {"plan": "pilot", "max_rangers": 5, "max_managers": 2, "max_areas": 1, "modules": ["grts"],
                    "starts_at": "2026-09-01T00:00:00Z", "expires_at": "2027-09-01T00:00:00Z", "grace_days": 7},
        "admin": {"full_name": "First Admin", "email": "first@hwt.example"},
    }, format="json")
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["organisation"]["code"] == "HWT" and body["temporary_password"] and body["totp_secret"]
    org_id = body["organisation"]["id"]
    r = c.put(f"/api/v1/platform/organisations/{org_id}/licence/", {"max_rangers": 25, "status": "suspended"}, format="json")
    assert r.status_code == 200 and r.json()["max_rangers"] == 25 and r.json()["status"] == "suspended"
    assert Licence.objects.get(organisation_id=org_id).max_rangers == 25
    r = c.patch(f"/api/v1/platform/organisations/{org_id}/", {"name": "Hwange Trust II"}, format="json")
    assert r.json()["name"] == "Hwange Trust II"
    usage = c.get(f"/api/v1/platform/organisations/{org_id}/usage/").json()
    assert usage["seats"]["managers_used"] == 1 and usage["seats"]["max_rangers"] == 25
