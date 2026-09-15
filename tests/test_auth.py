from datetime import timedelta

import pyotp
import pytest
from django.utils import timezone

from accounts.models import AuthToken, LoginLockout
from audit.models import AuditLog

from .conftest import client_for, make_org, make_user, totp_now

pytestmark = pytest.mark.django_db

LOGIN = "/api/v1/auth/login/"


def ranger_login(code, emp, password, **extra):
    return client_for().post(LOGIN, {"organisation_code": code, "employee_id": emp, "password": password,
                                     "device_id": "device-1", **extra}, format="json")


def test_ranger_login_success_payload():
    org = make_org("GRTTS")
    ranger = make_user(org, "ranger", employee_id="RGR-2026-041")
    r = ranger_login("grtts", "rgr-2026-041", "patrol123")  # code and employee id are case-insensitive
    assert r.status_code == 200, r.content
    body = r.json()
    assert set(body) == {"token", "user", "organisation", "licence"}
    assert body["user"]["id"] == str(ranger.pk)
    assert body["user"]["employee_id"] == "RGR-2026-041"
    assert body["organisation"]["code"] == "GRTTS"
    assert body["licence"]["status"] == "active"
    assert AuthToken.objects.get(key=body["token"]).device_id == "device-1"
    assert AuditLog.objects.filter(action="auth.login", actor_id=ranger.pk).exists()

    me = client_for()
    me.credentials(HTTP_AUTHORIZATION=f"Token {body['token']}")
    assert me.get("/api/v1/me/").json()["user"]["full_name"] == ranger.full_name


def test_wrong_password_and_unknown_user_look_identical():
    org = make_org()
    ranger = make_user(org, "ranger")
    bad = ranger_login(org.code, ranger.employee_id, "nope")
    unknown = ranger_login(org.code, "NOBODY", "nope")
    assert bad.status_code == unknown.status_code == 401
    assert bad.json() == unknown.json() == {"error": {"code": "invalid_credentials", "message": "Invalid credentials."}}


def test_lockout_after_five_failures_blocks_correct_password():
    org = make_org()
    ranger = make_user(org, "ranger")
    codes = [ranger_login(org.code, ranger.employee_id, "wrong").status_code for _ in range(5)]
    assert codes == [401, 401, 401, 401, 429]
    r = ranger_login(org.code, ranger.employee_id, "patrol123")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "locked_out"
    assert int(r["Retry-After"]) > 800

    # After 15 minutes the lock expires.
    LoginLockout.objects.update(locked_until=timezone.now() - timedelta(seconds=1))
    assert ranger_login(org.code, ranger.employee_id, "patrol123").status_code == 200
    assert not LoginLockout.objects.exists()


def test_totp_required_for_manager():
    org = make_org()
    manager = make_user(org, "manager", email="grace@example.org", password="manager123")
    c = client_for()
    r = c.post(LOGIN, {"email": "grace@example.org", "password": "manager123"}, format="json")
    assert r.status_code == 401 and r.json()["error"]["code"] == "totp_required"

    r = c.post(LOGIN, {"email": "grace@example.org", "password": "manager123", "totp": "000000"}, format="json")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_totp"

    r = c.post(LOGIN, {"email": "GRACE@example.org", "password": "manager123", "totp": totp_now(manager)}, format="json")
    assert r.status_code == 200, r.content
    assert r.json()["user"]["role"] == "manager"


def test_totp_code_cannot_be_replayed():
    org = make_org()
    admin = make_user(org, "org_admin", email="admin@example.org", password="admin1234")
    code = totp_now(admin)
    c = client_for()
    body = {"email": "admin@example.org", "password": "admin1234", "totp": code}
    assert c.post(LOGIN, body, format="json").status_code == 200
    r = c.post(LOGIN, body, format="json")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_totp"


def test_manager_on_ranger_path_still_needs_totp():
    org = make_org()
    make_user(org, "manager", employee_id="MGR-1")
    r = ranger_login(org.code, "MGR-1", "patrol123")
    assert r.json()["error"]["code"] == "totp_required"


def test_logout_deletes_token():
    org = make_org()
    c = client_for(make_user(org, "ranger"))
    assert c.get("/api/v1/me/").status_code == 200
    assert c.post("/api/v1/auth/logout/").status_code == 204
    assert not AuthToken.objects.filter(pk=c.token.pk).exists()
    r = c.get("/api/v1/me/")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_token"


def test_idle_token_expires_for_web_roles(settings):
    org = make_org()
    c = client_for(make_user(org, "manager"))
    AuthToken.objects.filter(pk=c.token.pk).update(last_used_at=timezone.now() - timedelta(hours=settings.WEB_TOKEN_IDLE_HOURS + 1))
    r = c.get("/api/v1/me/")
    assert r.status_code == 401 and r.json()["error"]["code"] == "token_expired"


def test_unauthenticated_request_uses_error_envelope():
    r = client_for().get("/api/v1/areas/")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "not_authenticated"
