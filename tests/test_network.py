"""CORS for the web dashboard and the optional web IP allowlist (spec §7 "Cross-origin + network")."""
import pytest

from .conftest import client_for, make_org, make_user, totp_now

pytestmark = pytest.mark.django_db

DASH = "https://patroliq-dashboard.vercel.app"


@pytest.fixture
def cors(settings):
    settings.CORS_ALLOWED_ORIGINS = [DASH]
    settings.CORS_ALLOWED_ORIGIN_REGEXES = [r"^https://patroliq-dashboard-[a-z0-9-]+\.vercel\.app$"]


def test_cors_allowed_origin(cors):
    manager = make_user(make_org(), "manager")
    c = client_for(manager)
    r = c.get("/api/v1/me/", HTTP_ORIGIN=DASH)
    assert r.status_code == 200
    assert r["Access-Control-Allow-Origin"] == DASH
    assert set(r["Access-Control-Expose-Headers"].replace(" ", "").split(",")) == {"retry-after", "content-disposition"}
    assert "Access-Control-Allow-Credentials" not in r

    pre = c.options("/api/v1/reports/", HTTP_ORIGIN=DASH, HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
                    HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization, content-type")
    assert pre.status_code == 200 and pre["Access-Control-Allow-Origin"] == DASH
    assert {"authorization", "content-type"} <= set(pre["Access-Control-Allow-Headers"].replace(" ", "").split(","))

    preview = "https://patroliq-dashboard-git-feature-x.vercel.app"
    assert c.get("/api/v1/me/", HTTP_ORIGIN=preview)["Access-Control-Allow-Origin"] == preview


def test_cors_disallowed_origin(cors):
    c = client_for(make_user(make_org(), "manager"))
    r = c.get("/api/v1/me/", HTTP_ORIGIN="https://evil.example")
    assert r.status_code == 200 and "Access-Control-Allow-Origin" not in r
    r = c.get("/api/v1/me/", HTTP_ORIGIN="https://patroliq-dashboard.vercel.app.evil.example")
    assert "Access-Control-Allow-Origin" not in r


@pytest.fixture
def allowlist(settings):
    settings.WEB_IP_ALLOWLIST = ["196.44.0.0/16", "2001:db8::/32"]
    settings.TRUSTED_PROXY_COUNT = 1
    settings.CORS_ALLOWED_ORIGINS = [DASH]


def via(ip, spoofed="203.0.113.9"):
    # Render's proxy appends the real client address as the right-most X-Forwarded-For hop.
    return {"HTTP_X_FORWARDED_FOR": f"{spoofed}, {ip}", "REMOTE_ADDR": "10.0.0.1"}


def test_ip_allowlist_restricts_web_roles(allowlist):
    org = make_org()
    manager, researcher, ranger = make_user(org, "manager"), make_user(org, "researcher"), make_user(org, "ranger")
    for user in (manager, researcher):
        c = client_for(user)
        r = c.get("/api/v1/me/", HTTP_ORIGIN=DASH, **via("8.8.8.8", spoofed="196.44.1.1"))
        assert r.status_code == 403 and r.json()["error"]["code"] == "ip_not_allowed"
        assert r["Access-Control-Allow-Origin"] == DASH  # the dashboard can read the error
        assert c.get("/api/v1/me/", **via("196.44.10.20")).status_code == 200
    assert client_for(manager).get("/api/v1/me/", **via("2001:db8::5")).status_code == 200
    platform = make_user(None, "platform_admin", email="ops@zrgis.example")
    assert client_for(platform).get("/api/v1/platform/organisations/", **via("8.8.8.8")).status_code == 403


def test_ip_allowlist_never_blocks_rangers_sync_safety_or_health(allowlist):
    org = make_org()
    ranger = make_user(org, "ranger")
    c = client_for(ranger)
    outside = via("8.8.8.8")
    assert c.get("/api/v1/me/", **outside).status_code == 200
    assert c.get("/api/v1/sync/bootstrap/", **outside).status_code == 200
    assert c.post("/api/v1/positions/", {"pings": []}, format="json", **outside).status_code == 202
    assert client_for().get("/healthz/", **outside).status_code == 200
    # Safety endpoint for a manager token from outside is still accepted (never cut off from SOS).
    manager = make_user(org, "manager")
    r = client_for(manager).post("/api/v1/safety/alerts/", {"client_uuid": "7d1f0f0e-9d49-4d1e-8b0e-111111111111"},
                                 format="json", **outside)
    assert r.status_code == 201
    # Ranger sign-in form is not restricted; web sign-in is.
    r = client_for().post("/api/v1/auth/login/", {"organisation_code": org.code, "employee_id": ranger.employee_id,
                                                  "password": "patrol123"}, format="json", **outside)
    assert r.status_code == 200
    r = client_for().post("/api/v1/auth/login/", {"email": manager.email, "password": "patrol123",
                                                  "totp": totp_now(manager)}, format="json", **outside)
    assert r.status_code == 403 and r.json()["error"]["code"] == "ip_not_allowed"
    r = client_for().post("/api/v1/auth/login/", {"email": manager.email, "password": "patrol123",
                                                  "totp": totp_now(manager)}, format="json", **via("196.44.3.3"))
    assert r.status_code == 200


def test_healthz_bypasses_host_validation_and_ssl_redirect(settings):
    settings.ALLOWED_HOSTS = ["patroliq-api.onrender.com"]
    settings.SECURE_SSL_REDIRECT = True
    c = client_for()
    r = c.get("/healthz/", HTTP_HOST="10.201.4.7:10000")
    assert r.status_code == 200 and r.json() == {"status": "ok", "database": True}
    assert c.get("/api/v1/me/", HTTP_HOST="10.201.4.7:10000").status_code == 400  # everything else still validated


def test_r2_storage_settings(monkeypatch):
    import importlib

    import patroliq.settings as base

    for k, v in {"R2_BUCKET": "patroliq-media", "R2_ACCOUNT_ID": "abc123", "R2_ACCESS_KEY_ID": "key",
                 "R2_SECRET_ACCESS_KEY": "secret"}.items():
        monkeypatch.setenv(k, v)
    try:
        mod = importlib.reload(base)
        assert mod.USE_R2 and mod.STORAGES["default"]["BACKEND"] == "storages.backends.s3.S3Storage"
        opts = mod.STORAGES["default"]["OPTIONS"]
        assert opts["endpoint_url"] == "https://abc123.r2.cloudflarestorage.com" and opts["default_acl"] is None
        from storages.backends.s3 import S3Storage

        assert S3Storage(**opts).bucket_name == "patroliq-media"
    finally:
        for k in ("R2_BUCKET", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
            monkeypatch.delenv(k)
        importlib.reload(base)


def test_no_allowlist_means_no_restriction(settings):
    settings.WEB_IP_ALLOWLIST = []
    c = client_for(make_user(make_org(), "manager"))
    assert c.get("/api/v1/me/", **via("8.8.8.8")).status_code == 200
