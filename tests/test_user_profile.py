"""User personal details (spec v1.5 §B): validation, full_name derivation and exposure boundaries."""
from datetime import date, timedelta

import pytest
from django.utils import timezone

from accounts.models import PERSONAL_FIELDS, User
from audit.models import AuditLog

from .conftest import client_for, make_area, make_org, make_user, totp_now

pytestmark = pytest.mark.django_db

PROFILE = {
    "first_name": "Tendai", "surname": "Moyo", "national_id": "63-123456a78",
    "date_of_birth": "1994-03-11", "home_address": "12 Rowa Road, Mazowe",
    "next_of_kin_name": "Rudo Moyo", "next_of_kin_relationship": "Spouse",
    "next_of_kin_phone": "+263771234567", "next_of_kin_address": "12 Rowa Road, Mazowe",
    "date_joined_org": "2021-06-01", "rank": "Senior Ranger",
    "post": "Patrol ranger · APU-2 Mazowe River",
    "certificates": "Advanced Field Ranger (2024)\nFirst Aid Level 2 (2023)",
}


@pytest.fixture
def org_ctx():
    org = make_org()
    admin = make_user(org, "org_admin")
    manager = make_user(org, "manager")
    return {"org": org, "admin": admin, "manager": manager, "adm": client_for(admin), "mgr": client_for(manager)}


def create_ranger(client, **overrides):
    body = {"employee_id": "RGR-2026-777", "role": "ranger", "phone": "+263779999999", **PROFILE}
    body.update(overrides)
    return client.post("/api/v1/users/", body, format="json")


def test_create_derives_full_name_and_normalises_national_id(org_ctx):
    r = create_ranger(org_ctx["adm"])
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["full_name"] == "Tendai Moyo"  # derived: full_name was not sent
    assert body["national_id"] == "63-123456A78"  # uppercased
    assert body["date_of_birth"] == "1994-03-11" and body["date_joined_org"] == "2021-06-01"
    assert body["certificates"].startswith("Advanced Field Ranger (2024)")
    assert set(PERSONAL_FIELDS) <= set(body)
    assert "temporary_password" in body

    # An explicit full_name always wins.
    r = create_ranger(org_ctx["adm"], employee_id="RGR-2026-778", full_name="T. Moyo (Sgt)")
    assert r.status_code == 201 and r.json()["full_name"] == "T. Moyo (Sgt)"


def test_full_name_still_required_without_both_names(org_ctx):
    r = create_ranger(org_ctx["adm"], surname=None, first_name="Tendai")
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "validation_error" and "full_name" in err["fields"]


def test_personal_field_validation(org_ctx):
    today = timezone.localdate()
    cases = {
        "national_id": "63/123456*78",
        "date_of_birth": (today - timedelta(days=365 * 10)).isoformat(),   # a 10-year-old
        "date_joined_org": (today + timedelta(days=1)).isoformat(),        # in the future
    }
    for field, value in cases.items():
        r = create_ranger(org_ctx["adm"], employee_id=f"RGR-BAD-{field}", **{field: value})
        assert r.status_code == 400, field
        assert field in r.json()["error"]["fields"], field
    r = create_ranger(org_ctx["adm"], employee_id="RGR-BAD-DOB2", date_of_birth=today.isoformat())
    assert r.status_code == 400 and "date_of_birth" in r.json()["error"]["fields"]
    # 16 exactly is allowed.
    sixteen = date(today.year - 16, today.month, today.day).isoformat()
    assert create_ranger(org_ctx["adm"], employee_id="RGR-OK-16", date_of_birth=sixteen).status_code == 201
    # Over-long free text is rejected rather than silently truncated.
    r = create_ranger(org_ctx["adm"], employee_id="RGR-BAD-CERT", certificates="x" * 1001)
    assert r.status_code == 400 and "certificates" in r.json()["error"]["fields"]


def test_update_rederives_full_name_and_audits_field_names_only(org_ctx):
    ranger = User.objects.get(pk=create_ranger(org_ctx["adm"]).json()["id"])
    r = org_ctx["adm"].patch(f"/api/v1/users/{ranger.pk}/",
                             {"first_name": "Tendai", "surname": "Chikwanha", "national_id": "63-999999z01"},
                             format="json")
    assert r.status_code == 200, r.content
    assert r.json()["full_name"] == "Tendai Chikwanha" and r.json()["national_id"] == "63-999999Z01"

    entry = AuditLog.objects.filter(action="user.update").order_by("-created_at").first()
    assert entry.detail == {"fields": ["first_name", "national_id", "surname"]}
    assert "Chikwanha" not in str(entry.detail) and "63-999999Z01" not in str(entry.detail)

    # Changing only one of the two names leaves full_name alone.
    r = org_ctx["adm"].patch(f"/api/v1/users/{ranger.pk}/", {"surname": "Moyo"}, format="json")
    assert r.status_code == 200 and r.json()["full_name"] == "Tendai Chikwanha"


def test_only_org_admin_may_write_personal_details(org_ctx):
    ranger_id = create_ranger(org_ctx["adm"]).json()["id"]
    r = org_ctx["mgr"].patch(f"/api/v1/users/{ranger_id}/", {"national_id": "63-000000A00"}, format="json")
    assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied"
    assert User.objects.get(pk=ranger_id).national_id == "63-123456A78"

    ranger = User.objects.get(pk=ranger_id)
    ranger.set_password("patrol123")
    ranger.save()
    assert client_for(ranger).patch(f"/api/v1/users/{ranger_id}/", {"rank": "General"},
                                    format="json").status_code == 403
    # Managers may still read.
    assert org_ctx["mgr"].get(f"/api/v1/users/{ranger_id}/").json()["rank"] == "Senior Ranger"


def test_personal_details_only_in_users_and_ranger_profile(org_ctx, settings):
    area = make_area(org_ctx["org"])
    ranger = User.objects.get(pk=create_ranger(org_ctx["adm"]).json()["id"])
    ranger.set_password("patrol123")
    ranger.areas.add(area)
    ranger.save()
    ranger_client = client_for(ranger)
    personal = set(PERSONAL_FIELDS)

    # users/ — the one place the details live.
    listed = org_ctx["adm"].get("/api/v1/users/").json()
    assert personal <= set(next(u for u in listed if u["id"] == str(ranger.pk)))

    # rangers/{id}/ exposes them under `profile`, and only there.
    detail = org_ctx["mgr"].get(f"/api/v1/rangers/{ranger.pk}/").json()
    assert set(detail["profile"]) == personal
    assert detail["profile"]["rank"] == "Senior Ranger" and detail["profile"]["date_of_birth"] == "1994-03-11"
    assert not personal & set(detail)

    # Everything ranger-facing and every other manager payload stays clean.
    listed_rangers = org_ctx["mgr"].get("/api/v1/rangers/").json()
    assert all(not personal & set(row) for row in listed_rangers)
    assert all("profile" not in row for row in listed_rangers)

    me = ranger_client.get("/api/v1/me/").json()
    assert not personal & set(me["user"])

    settings.WEB_TOTP_REQUIRED = True
    login = client_for().post("/api/v1/auth/login/", {"email": org_ctx["admin"].email, "password": "patrol123",
                                                      "totp": totp_now(org_ctx["admin"])}, format="json").json()
    assert not personal & set(login["user"])

    bootstrap = ranger_client.get("/api/v1/sync/bootstrap/").json()
    assert all(set(m) == {"id", "full_name", "employee_id", "role"} for m in bootstrap["team_members"])


def test_personal_details_are_tenant_scoped(org_ctx):
    ranger_id = create_ranger(org_ctx["adm"]).json()["id"]
    other_org = make_org()
    outsider = client_for(make_user(other_org, "org_admin"))
    assert outsider.get(f"/api/v1/users/{ranger_id}/").status_code == 404
    assert outsider.get(f"/api/v1/rangers/{ranger_id}/").status_code == 404
    assert {u["organisation_id"] for u in outsider.get("/api/v1/users/").json()} == {str(other_org.pk)}
    assert outsider.patch(f"/api/v1/users/{ranger_id}/", {"rank": "Nobody"}, format="json").status_code == 404
