"""PATROLIQ v1.6 notifications: email channel, sync summaries, SMS fixes, notification health API."""
from __future__ import annotations

from unittest import mock

import pytest
from django.core import mail
from django.core.mail.backends.base import BaseEmailBackend
from shapely.geometry import shape

from areas.models import GrtsCell
from audit.models import AuditLog
from notify import backends
from notify.jobs import dispatch
from notify.models import NotificationLog
from notify.phones import mask_email, mask_phone, normalise_phone

from .conftest import client_for, make_area, make_org, make_user, new_uuid

pytestmark = pytest.mark.django_db

SECRET_TOKEN = "tok_do_not_leak_0123456789"
SECRET_KEY = "key_secret_do_not_leak_987"


class BrokenEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        raise ConnectionRefusedError("SMTP server unreachable")


@pytest.fixture
def smtp(settings):
    """Email 'configured' (EMAIL_HOST set); messages land in mail.outbox (locmem backend)."""
    settings.EMAIL_HOST = "smtp.example.org"
    settings.DEFAULT_FROM_EMAIL = "PATROLIQ Alerts <alerts@example.org>"
    settings.DASHBOARD_URL = "https://dash.example.org"
    return settings


@pytest.fixture
def ctx():
    org = make_org()
    other = make_org()
    area = make_area(org, name="Mazowe Conservancy")
    ranger = make_user(org, "ranger", full_name="Tendai Moyo", employee_id="RGR-2026-041",
                       national_id="63-123456-A-42", home_address="12 Secret Road", next_of_kin_name="Kin Person")
    ranger.areas.add(area)
    manager = make_user(org, "manager", full_name="Grace Manager", email="grace@grtts.co.zw", phone="0771111111")
    admin = make_user(org, "org_admin", full_name="Admin Person", email="admin@grtts.co.zw", phone="+263772222222")
    make_user(org, "researcher", email="researcher@grtts.co.zw")  # never a recipient
    foreign = make_user(other, "manager", email="other@elsewhere.org")  # other tenant: never a recipient
    cell = GrtsCell.objects.filter(area=area).order_by("grts_order").first()
    lon, lat = shape(cell.geometry).representative_point().coords[0]
    return {"org": org, "other": other, "area": area, "ranger": ranger, "manager": manager, "admin": admin,
            "foreign": foreign, "cell": cell, "lon": lon, "lat": lat, "client": client_for(ranger)}


def sync_emails():
    return [m for m in mail.outbox if "PATROLIQ sync" in m.subject]


def alert_emails(title):
    return [m for m in mail.outbox if m.subject == title]


def push(ctx, **payload):
    body = {"patrols": [], "track_points": [], "observations": [], "safety_alerts": [], **payload}
    r = ctx["client"].post("/api/v1/sync/push/", body, format="json")
    assert r.status_code == 200, r.content
    return r.json()


def patrol_item(ctx, patrol_id, status="active", **extra):
    return {"client_uuid": patrol_id, "area_id": str(ctx["area"].pk), "patrol_type": "foot",
            "started_at": "2026-09-15T06:00:00Z", "status": status, **extra}


def observation_item(ctx, obs_id=None, **extra):
    return {"client_uuid": obs_id or new_uuid(), "area_id": str(ctx["area"].pk), "category": "wildlife",
            "subtype": "sighting", "species_name": "African Elephant", "count": 7, "sex": "mixed",
            "lat": ctx["lat"], "lon": ctx["lon"], "recorded_at": "2026-09-15T06:15:00Z", **extra}


# --- phone numbers ----------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("0771234567", "+263771234567"),
    ("+263 77 123 4567", "+263771234567"),
    ("263771234567", "+263771234567"),
    ("00263771234567", "+263771234567"),
    ("+263-77-123-4567", "+263771234567"),
    ("(077) 123 4567", "+263771234567"),
    ("+263 0771234567", "+263771234567"),
    ("771234567", "+263771234567"),
    ("+27 82 123 4567", "+27821234567"),
    ("+1 (202) 555-0123", "+12025550123"),
    ("", ""),
    (None, ""),
    ("garbage", None),
    ("077123456x", None),
    ("12", None),
    ("+263 77 123", None),
    ("+26377123456789", None),
    ("+0771234567", None),
    ("+1234567890123456", None),
])
def test_normalise_phone(raw, expected):
    assert normalise_phone(raw) == expected


def test_default_country_code_is_configurable(settings):
    settings.SMS_DEFAULT_COUNTRY_CODE = "27"
    assert normalise_phone("0821234567") == "+27821234567"
    assert normalise_phone("0821234567", country_code="263") == "+263821234567"


def test_masking():
    assert mask_phone("+263771234567") == "+26377•••••67"
    assert mask_phone("0771234567") == "+26377•••••67"
    assert mask_phone("") is None
    assert mask_email("grace@grtts.co.zw") == "g•••@grtts.co.zw"
    assert mask_email(None) is None


def test_users_api_normalises_and_rejects_phone(ctx):
    admin = client_for(ctx["admin"])
    url = f"/api/v1/users/{ctx['ranger'].pk}/"
    r = admin.patch(url, {"phone": "077 123 4567"}, format="json")
    assert r.status_code == 200, r.content
    assert r.json()["phone"] == "+263771234567"
    r = admin.patch(url, {"phone": "garbage"}, format="json")
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "validation_error" and "phone" in err["fields"]
    r = admin.patch(url, {"phone": ""}, format="json")
    assert r.status_code == 200 and r.json()["phone"] == ""
    r = admin.post("/api/v1/users/", {"employee_id": "RGR-9", "full_name": "New Ranger", "role": "ranger",
                                      "phone": "263 77 999 8888"}, format="json")
    assert r.status_code == 201, r.content
    assert r.json()["phone"] == "+263779998888"


# --- alert emails -------------------------------------------------------------------------------------

def sos(ctx, **extra):
    body = {"client_uuid": new_uuid(), "kind": "panic", "lat": ctx["lat"], "lon": ctx["lon"], "accuracy_m": 6.0,
            "battery_pct": 54, "signal_level": 2, "started_at": "2026-09-15T09:12:00Z",
            "area_id": str(ctx["area"].pk), **extra}
    r = ctx["client"].post("/api/v1/safety/alerts/", body, format="json")
    return body, r


def test_sos_emails_managers_with_full_body(ctx, smtp):
    body, r = sos(ctx)
    assert r.status_code == 201, r.content
    emails = alert_emails("PATROLIQ PANIC ALERT")
    assert sorted(m.to[0] for m in emails) == ["admin@grtts.co.zw", "grace@grtts.co.zw"]  # one message each
    msg = emails[0]
    assert msg.from_email == "PATROLIQ Alerts <alerts@example.org>"
    text = msg.body
    for expected in ("Tendai Moyo", "RGR-2026-041", "Panic button", "2026-09-15 11:12 CAT", "Mazowe Conservancy",
                     ctx["cell"].label, "54%", "https://www.google.com/maps?q=", "https://dash.example.org"):
        assert expected in text, expected
    html = msg.alternatives[0][0]
    assert "#102C26" in html and "<img" not in html and "Tendai Moyo" in html
    for secret in ("63-123456-A-42", "Secret Road", "Kin Person"):
        assert secret not in text and secret not in html

    logs = NotificationLog.objects.filter(channel="email", title="PATROLIQ PANIC ALERT")
    assert {log.recipient_id for log in logs} == {ctx["manager"].pk, ctx["admin"].pk}
    assert all(log.success and log.backend == "smtp" for log in logs)
    # SMS went to the normalised number of the manager saved as 0771111111.
    assert NotificationLog.objects.filter(channel="sms", recipient_id=ctx["manager"].pk).get().to == "+263771111111"


def test_hwc_alert_email_and_details_email(ctx, smtp):
    cu = new_uuid()
    body, r = sos(ctx, client_uuid=cu, kind="human_wildlife_conflict")
    assert r.status_code == 201
    assert len(alert_emails("PATROLIQ HUMAN-WILDLIFE CONFLICT")) == 2
    r = ctx["client"].post("/api/v1/safety/alerts/", {
        "client_uuid": cu, "kind": "human_wildlife_conflict",
        "details": {"conflict_type": "crop_raiding", "species_name": "African Elephant", "animal_count": 7,
                    "reporter_phone": "+263770000000"}}, format="json")
    assert r.status_code == 200
    details = alert_emails("PATROLIQ HWC DETAILS")
    assert len(details) == 2
    assert "Crop raiding" in details[0].body and "African Elephant" in details[0].body


def test_email_not_configured_is_logged_as_skipped(ctx):
    _, r = sos(ctx)
    assert r.status_code == 201
    logs = NotificationLog.objects.filter(channel="email")
    assert logs.count() >= 2
    assert all(not log.success and log.error.startswith("skipped:") for log in logs)


def test_email_alerts_toggle(ctx, smtp):
    smtp.EMAIL_ALERTS = False
    smtp.EMAIL_SYNC_SUMMARIES = False
    _, r = sos(ctx)
    assert r.status_code == 201
    assert mail.outbox == [] and not NotificationLog.objects.filter(channel="email").exists()
    assert NotificationLog.objects.filter(channel="sms").count() == 2


def test_no_manager_phone_writes_single_row_and_still_emails(ctx, smtp):
    for u in (ctx["manager"], ctx["admin"]):
        u.phone = ""
        u.save()
    _, r = sos(ctx)
    assert r.status_code == 201
    sms = NotificationLog.objects.filter(channel="sms")
    assert sms.count() == 1
    row = sms.get()
    assert row.error == "no manager has a phone number" and not row.success and row.recipient_id is None
    assert len(alert_emails("PATROLIQ PANIC ALERT")) == 2


def test_invalid_stored_phone_logged_at_send_time(ctx):
    type(ctx["manager"]).objects.filter(pk=ctx["manager"].pk).update(phone="call me maybe")  # legacy row
    _, r = sos(ctx)
    assert r.status_code == 201
    bad = NotificationLog.objects.get(channel="sms", recipient_id=ctx["manager"].pk)
    assert not bad.success and bad.error == "invalid phone number"
    good = NotificationLog.objects.get(channel="sms", recipient_id=ctx["admin"].pk)
    assert good.success and good.to == "+263772222222"


# --- sync summaries -------------------------------------------------------------------------------------

def test_sync_summary_for_new_observation(ctx, smtp):
    push(ctx, observations=[observation_item(ctx)])
    emails = sync_emails()
    assert len(emails) == 2
    msg = emails[0]
    assert msg.subject == "PATROLIQ sync · Tendai Moyo · 1 new record"
    for expected in ("OBSERVATIONS", "African Elephant", "Count: 7", "Sex: mixed", ctx["cell"].label,
                     "2026-09-15 08:15 CAT", "Device: test", "Mazowe Conservancy"):
        assert expected in msg.body, expected
    assert "63-123456-A-42" not in msg.body
    assert NotificationLog.objects.filter(channel="email", title=msg.subject, success=True).count() == 2


def test_sync_summary_for_patrol_start_and_end_only_once(ctx, smtp):
    pid = new_uuid()
    push(ctx, patrols=[patrol_item(ctx, pid, notes="Morning sweep")])
    started = sync_emails()
    assert len(started) == 2 and "Foot patrol started" in started[0].body

    mail.outbox.clear()
    points = [{"patrol_client_uuid": pid, "recorded_at": f"2026-09-15T06:{m:02d}:00Z", "lat": ctx["lat"],
               "lon": ctx["lon"]} for m in (5, 10)]
    push(ctx, patrols=[patrol_item(ctx, pid, status="active")], track_points=points)
    push(ctx, track_points=points)
    assert sync_emails() == []  # routine syncs of an ongoing patrol are not reportable

    push(ctx, patrols=[patrol_item(ctx, pid, status="ended", ended_at="2026-09-15T08:30:00Z",
                                   notes="Morning sweep, all quiet", debrief_audio="media/abc.m4a")])
    ended = sync_emails()
    assert len(ended) == 2
    body = ended[0].body
    assert "Foot patrol ended" in body and "2 h 30 min" in body and "all quiet" in body
    assert "Debrief audio: yes" in body and "10:30 CAT" in body

    mail.outbox.clear()
    push(ctx, patrols=[patrol_item(ctx, pid, status="ended", ended_at="2026-09-15T08:30:00Z")])
    assert sync_emails() == []  # replay of the ended patrol


def test_no_summary_for_replayed_push(ctx, smtp):
    obs = observation_item(ctx)
    push(ctx, observations=[obs])
    mail.outbox.clear()
    NotificationLog.objects.all().delete()
    push(ctx, observations=[obs])
    assert sync_emails() == [] and not NotificationLog.objects.exists()


def test_summary_flags_urgent_batches(ctx, smtp):
    push(ctx, observations=[observation_item(ctx, category="threat", subtype="snare", severity="critical",
                                             species_name=None, count=None, sex=None)],
         safety_alerts=[{"client_uuid": new_uuid(), "kind": "panic", "lat": ctx["lat"], "lon": ctx["lon"]}])
    emails = sync_emails()
    assert len(emails) == 2
    assert emails[0].subject == "⚠ PATROLIQ sync · Tendai Moyo · 2 new records"
    assert "SAFETY ALERTS" in emails[0].body and "Panic button" in emails[0].body
    # the immediate alert emails still go out alongside the summary
    assert len(alert_emails("PATROLIQ PANIC ALERT")) == 2 and len(alert_emails("PATROLIQ THREAT ALERT")) == 2


def test_safety_alert_endpoint_sends_summary_once(ctx, smtp):
    body, _ = sos(ctx)
    assert len(sync_emails()) == 2
    mail.outbox.clear()
    ctx["client"].post("/api/v1/safety/alerts/", {**body, "battery_pct": 40}, format="json")  # replay
    assert sync_emails() == []


def test_sync_summaries_toggle(ctx, smtp):
    smtp.EMAIL_SYNC_SUMMARIES = False
    push(ctx, observations=[observation_item(ctx)], patrols=[patrol_item(ctx, new_uuid())])
    assert sync_emails() == []


def test_email_failure_does_not_break_push(ctx, smtp):
    smtp.EMAIL_BACKEND = "tests.test_notifications.BrokenEmailBackend"
    out = push(ctx, observations=[observation_item(ctx, category="threat", subtype="snare", severity="high",
                                                   species_name=None, count=None, sex=None)])
    assert len(out["accepted"]["observations"]) == 1 and out["rejected"] == []
    failed = NotificationLog.objects.filter(channel="email", success=False)
    assert failed.count() == 4  # threat alert + sync summary, two managers each
    assert all("SMTP server unreachable" in f.error for f in failed)
    assert NotificationLog.objects.filter(channel="sms", success=True).count() == 2


def test_summary_rendering_error_does_not_break_push(ctx, smtp):
    with mock.patch("field.notifications.build_sync_summary", side_effect=RuntimeError("boom")):
        out = push(ctx, observations=[observation_item(ctx)])
    assert len(out["accepted"]["observations"]) == 1


def test_dispatch_waits_for_commit_when_async(settings, django_capture_on_commit_callbacks):
    settings.NOTIFY_ASYNC = True
    calls = []
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        dispatch(calls.append, 1)
    assert calls == [] and len(callbacks) == 1


# --- Twilio -------------------------------------------------------------------------------------------

def _response(status, payload=None):
    resp = mock.Mock(status_code=status)
    resp.json.side_effect = (lambda: payload) if payload is not None else ValueError("no json")
    return resp


@pytest.fixture
def twilio_env(monkeypatch):
    for key in ("TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET", "TWILIO_MESSAGING_SERVICE_SID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC0123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", SECRET_TOKEN)
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15005550006")
    return monkeypatch


def test_twilio_error_code_and_message(twilio_env):
    payload = {"code": 21608, "message": "The number is unverified. Trial accounts cannot send messages to "
                                         "unverified numbers", "status": 400}
    with mock.patch("requests.post", return_value=_response(400, payload)) as post:
        with pytest.raises(backends.NotifyError) as exc:
            backends.TwilioFcmBackend().send_sms("+263771234567", "hi")
    assert str(exc.value).startswith("Twilio 21608: The number is unverified.")
    assert SECRET_TOKEN not in str(exc.value)
    args, kwargs = post.call_args
    assert args[0] == "https://api.twilio.com/2010-04-01/Accounts/AC0123/Messages.json"
    assert kwargs["auth"] == ("AC0123", SECRET_TOKEN)
    assert kwargs["data"]["From"] == "+15005550006" and "MessagingServiceSid" not in kwargs["data"]

    with mock.patch("requests.post", return_value=_response(503)):
        with pytest.raises(backends.NotifyError, match=r"^Twilio HTTP 503$"):
            backends.TwilioFcmBackend().send_sms("+263771234567", "hi")


def test_twilio_network_error_never_echoes_secrets(twilio_env):
    import requests

    boom = requests.ConnectionError(f"failed with auth {SECRET_TOKEN}")
    with mock.patch("requests.post", side_effect=boom):
        with pytest.raises(backends.NotifyError) as exc:
            backends.TwilioFcmBackend().send_sms("+263771234567", "hi")
    assert SECRET_TOKEN not in str(exc.value) and "ConnectionError" in str(exc.value)


def test_twilio_api_key_auth_and_messaging_service(twilio_env):
    twilio_env.delenv("TWILIO_AUTH_TOKEN")
    twilio_env.delenv("TWILIO_FROM_NUMBER")
    twilio_env.setenv("TWILIO_API_KEY_SID", "SK0456")
    twilio_env.setenv("TWILIO_API_KEY_SECRET", SECRET_KEY)
    twilio_env.setenv("TWILIO_MESSAGING_SERVICE_SID", "MG0789")
    assert backends.twilio_config() == {"configured": True, "missing": [], "auth": "api_key",
                                        "sender": "messaging_service"}
    with mock.patch("requests.post", return_value=_response(201, {"sid": "SM1"})) as post:
        backends.TwilioFcmBackend().send_sms("+263771234567", "hi")
    args, kwargs = post.call_args
    assert "/Accounts/AC0123/" in args[0]
    assert kwargs["auth"] == ("SK0456", SECRET_KEY)
    assert kwargs["data"]["MessagingServiceSid"] == "MG0789" and "From" not in kwargs["data"]


def test_twilio_missing_config(twilio_env):
    twilio_env.delenv("TWILIO_AUTH_TOKEN")
    twilio_env.delenv("TWILIO_FROM_NUMBER")
    cfg = backends.twilio_config()
    assert cfg["configured"] is False and cfg["missing"] == ["TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"]
    with pytest.raises(backends.NotifyError, match="TWILIO_AUTH_TOKEN is not configured"):
        backends.TwilioFcmBackend().send_sms("+263771234567", "hi")


def test_twilio_failure_is_logged_with_code(ctx, settings, twilio_env):
    settings.NOTIFY_BACKEND = "twilio_fcm"
    payload = {"code": 21408, "message": "Permission to send an SMS has not been enabled for the region"}
    with mock.patch("requests.post", return_value=_response(400, payload)):
        _, r = sos(ctx)
    assert r.status_code == 201
    rows = NotificationLog.objects.filter(channel="sms")
    assert rows.count() == 2
    assert all(row.error == "Twilio 21408: Permission to send an SMS has not been enabled for the region"
               for row in rows)
    assert not NotificationLog.objects.filter(error__contains=SECRET_TOKEN).exists()


# --- notification health API ---------------------------------------------------------------------------

def test_status_shape_and_masking(ctx, smtp):
    ctx["admin"].phone = ""
    ctx["admin"].save()
    NotificationLog.objects.create(organisation_id=ctx["org"].pk, channel="sms", to="+263771111111",
                                   title="PATROLIQ PANIC ALERT", body="x", backend="twilio_fcm", success=False,
                                   error="Twilio 21608: The number is unverified.")
    NotificationLog.objects.create(organisation_id=ctx["other"].pk, channel="sms", to="+263770000000",
                                   title="FOREIGN", body="x", backend="twilio_fcm", success=False, error="x")
    NotificationLog.objects.create(organisation_id=ctx["org"].pk, channel="email", to="grace@grtts.co.zw",
                                   title="ok", body="x", backend="smtp", success=True)
    r = client_for(ctx["manager"]).get("/api/v1/notify/status/")
    assert r.status_code == 200, r.content
    body = r.json()
    assert set(body) == {"sms", "email", "push", "recipients", "problems", "last_success", "recent_failures"}
    assert body["sms"] == {"backend": "console", "configured": False, "missing": ["NOTIFY_BACKEND=twilio_fcm"],
                           "auth": None, "sender": None}
    assert body["email"] == {"configured": True, "host": "smtp.example.org",
                             "from_email": "PATROLIQ Alerts <alerts@example.org>", "alerts": True,
                             "sync_summaries": True}
    assert body["push"] == {"configured": False}
    recipients = {r["full_name"]: r for r in body["recipients"]}
    assert set(recipients) == {"Grace Manager", "Admin Person"}  # own org, manager-level only
    grace = recipients["Grace Manager"]
    assert grace == {"id": str(ctx["manager"].pk), "full_name": "Grace Manager", "role": "manager",
                     "phone_masked": "+26377•••••11", "phone_ok": True, "email_masked": "g•••@grtts.co.zw",
                     "email_ok": True, "problems": []}
    assert recipients["Admin Person"]["problems"] == ["no_phone"] and recipients["Admin Person"]["phone_masked"] is None
    assert body["problems"] == ["sms_not_configured"]
    assert body["last_success"]["email"] and body["last_success"]["sms"] is None
    assert body["recent_failures"] == [{"at": mock.ANY, "channel": "sms", "title": "PATROLIQ PANIC ALERT",
                                        "to_masked": "+26377•••••11",
                                        "error": "Twilio 21608: The number is unverified."}]
    assert "grace@grtts.co.zw" not in r.content.decode() and "+263771111111" not in r.content.decode()


def test_status_problems_and_invalid_phone(ctx, settings, twilio_env):
    settings.NOTIFY_BACKEND = "twilio_fcm"
    User = type(ctx["manager"])
    User.objects.filter(pk=ctx["manager"].pk).update(phone="garbage", email=None)
    User.objects.filter(pk=ctx["admin"].pk).update(phone="")
    body = client_for(ctx["admin"]).get("/api/v1/notify/status/").json()
    assert body["sms"]["configured"] is True and body["sms"]["auth"] == "auth_token"
    assert body["sms"]["sender"] == "number" and body["sms"]["backend"] == "twilio_fcm"
    assert set(body["problems"]) == {"no_sms_recipient", "email_not_configured"}
    grace = next(r for r in body["recipients"] if r["id"] == str(ctx["manager"].pk))
    assert grace["problems"] == ["invalid_phone", "no_email"] and grace["phone_ok"] is False


def test_status_role_gating(ctx):
    assert client_for(ctx["ranger"]).get("/api/v1/notify/status/").status_code == 403
    researcher = make_user(ctx["org"], "researcher")
    assert client_for(researcher).get("/api/v1/notify/status/").status_code == 403
    assert client_for(researcher).post("/api/v1/notify/test/", {"channel": "sms"}, format="json").status_code == 403
    assert client_for().get("/api/v1/notify/status/").status_code == 401
    platform = make_user(None, "platform_admin", email="ops@example.org")
    assert client_for(platform).get("/api/v1/notify/status/").status_code == 403


def test_test_endpoint_sends_only_to_caller(ctx, smtp):
    c = client_for(ctx["manager"])
    r = c.post("/api/v1/notify/test/", {"channel": "email"}, format="json")
    assert r.status_code == 200, r.content
    assert r.json() == {"ok": True, "channel": "email", "to_masked": "g•••@grtts.co.zw", "error": None}
    assert [m.to for m in mail.outbox] == [["grace@grtts.co.zw"]]

    r = c.post("/api/v1/notify/test/", {"channel": "sms"}, format="json")
    assert r.json() == {"ok": True, "channel": "sms", "to_masked": "+26377•••••11", "error": None}
    rows = NotificationLog.objects.filter(data__type="test")
    assert {(row.channel, row.recipient_id, row.to) for row in rows} == {
        ("email", ctx["manager"].pk, "grace@grtts.co.zw"), ("sms", ctx["manager"].pk, "+263771111111")}
    audits = AuditLog.objects.filter(action="notify.test", actor_id=ctx["manager"].pk)
    assert audits.count() == 2 and {a.detail["channel"] for a in audits} == {"sms", "email"}

    assert c.post("/api/v1/notify/test/", {"channel": "fax"}, format="json").json()["error"]["code"] == \
        "validation_error"
    assert c.post("/api/v1/notify/test/", {"channel": "sms", "to": "+263779999999"},
                  format="json").json()["error"]["code"] == "unexpected_fields"


def test_test_endpoint_reports_missing_phone(ctx):
    ctx["admin"].phone = ""
    ctx["admin"].save()
    r = client_for(ctx["admin"]).post("/api/v1/notify/test/", {"channel": "sms"}, format="json")
    assert r.json() == {"ok": False, "channel": "sms", "to_masked": None, "error": "recipient has no phone number"}
    r = client_for(ctx["admin"]).post("/api/v1/notify/test/", {"channel": "email"}, format="json")
    out = r.json()
    assert out["ok"] is False and out["error"].startswith("skipped:")  # EMAIL_HOST not set


def test_test_endpoint_throttled_per_user(ctx):
    c = client_for(ctx["manager"])
    for _ in range(5):
        assert c.post("/api/v1/notify/test/", {"channel": "sms"}, format="json").status_code == 200
    r = c.post("/api/v1/notify/test/", {"channel": "sms"}, format="json")
    assert r.status_code == 429 and r.json()["error"]["code"] == "throttled"
    # another user has their own budget
    assert client_for(ctx["admin"]).post("/api/v1/notify/test/", {"channel": "sms"}, format="json").status_code == 200
