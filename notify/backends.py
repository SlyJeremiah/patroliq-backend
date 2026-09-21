"""
Notification provider interface (SMS + push). Email goes through Django's email framework
(:mod:`notify.email`).

``NOTIFY_BACKEND``:
  * ``console`` (default, dev/test) — logs the message; nothing leaves the server.
  * ``twilio_fcm`` — SMS through Twilio's REST API and push through FCM HTTP v1. Credentials are read
    from the environment at send time:

      - ``TWILIO_ACCOUNT_SID`` (AC…, always required — it is part of the URL);
      - auth: ``TWILIO_API_KEY_SID`` (SK…) + ``TWILIO_API_KEY_SECRET`` when both are set, otherwise
        ``TWILIO_AUTH_TOKEN``;
      - sender: ``TWILIO_MESSAGING_SERVICE_SID`` (MG…) when set, otherwise ``TWILIO_FROM_NUMBER``.

    The FCM part is a stub (FCM_PROJECT_ID, FCM_ACCESS_TOKEN): obtaining the OAuth access token from a
    service account is left to deployment; pushes go to the topic ``org-<organisation_id>-managers``.
All server-side: no provider key is ever exposed to clients (PRD 7.2), and no secret ever appears in
an error message or NotificationLog row.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("patroliq.notify")

TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


class NotifyError(Exception):
    pass


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def twilio_config() -> dict:
    """What the Twilio SMS path would use, without secrets: ``{configured, missing, auth, sender}``."""
    missing = []
    if not _env("TWILIO_ACCOUNT_SID"):
        missing.append("TWILIO_ACCOUNT_SID")
    if _env("TWILIO_API_KEY_SID") and _env("TWILIO_API_KEY_SECRET"):
        auth = "api_key"
    elif _env("TWILIO_AUTH_TOKEN"):
        auth = "auth_token"
    else:
        auth = None
        if _env("TWILIO_API_KEY_SID") or _env("TWILIO_API_KEY_SECRET"):
            missing.append("TWILIO_API_KEY_SECRET" if _env("TWILIO_API_KEY_SID") else "TWILIO_API_KEY_SID")
        else:
            missing.append("TWILIO_AUTH_TOKEN")
    if _env("TWILIO_MESSAGING_SERVICE_SID"):
        sender = "messaging_service"
    elif _env("TWILIO_FROM_NUMBER"):
        sender = "number"
    else:
        sender = None
        missing.append("TWILIO_FROM_NUMBER")
    return {"configured": not missing, "missing": missing, "auth": auth, "sender": sender}


def twilio_error(resp) -> str:
    """``Twilio 21608: The number is unverified…`` from Twilio's JSON error body, else ``Twilio HTTP <code>``."""
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and (payload.get("code") or payload.get("message")):
        code = payload.get("code")
        message = str(payload.get("message") or "").strip()
        return f"Twilio {code}: {message}" if code else f"Twilio HTTP {resp.status_code}: {message}"
    return f"Twilio HTTP {resp.status_code}"


class NotificationBackend:
    name = "base"

    def send_sms(self, to: str, body: str) -> None:
        raise NotImplementedError

    def send_push(self, topic: str, title: str, body: str, data: dict) -> None:
        raise NotImplementedError

    def sms_status(self) -> dict:
        raise NotImplementedError

    def push_configured(self) -> bool:
        raise NotImplementedError


class ConsoleBackend(NotificationBackend):
    name = "console"

    def send_sms(self, to, body):
        if not to:
            raise NotifyError("recipient has no phone number")
        logger.warning("[SMS -> %s] %s", to, body)

    def send_push(self, topic, title, body, data):
        logger.warning("[PUSH -> %s] %s: %s %s", topic, title, body, data)

    def sms_status(self):
        return {"configured": False, "missing": ["NOTIFY_BACKEND=twilio_fcm"], "auth": None, "sender": None}

    def push_configured(self):
        return False


class TwilioFcmBackend(NotificationBackend):
    name = "twilio_fcm"

    @staticmethod
    def _require(name: str) -> str:
        value = _env(name)
        if not value:
            raise NotifyError(f"{name} is not configured")
        return value

    def sms_status(self):
        return twilio_config()

    def push_configured(self):
        return bool(_env("FCM_PROJECT_ID") and _env("FCM_ACCESS_TOKEN"))

    def send_sms(self, to, body):
        import requests

        config = twilio_config()
        if config["missing"]:
            raise NotifyError(f"{config['missing'][0]} is not configured")
        if not to:
            raise NotifyError("recipient has no phone number")
        sid = _env("TWILIO_ACCOUNT_SID")
        if config["auth"] == "api_key":
            auth = (_env("TWILIO_API_KEY_SID"), _env("TWILIO_API_KEY_SECRET"))
        else:
            auth = (sid, _env("TWILIO_AUTH_TOKEN"))
        data = {"To": to, "Body": body[:1600]}
        if config["sender"] == "messaging_service":
            data["MessagingServiceSid"] = _env("TWILIO_MESSAGING_SERVICE_SID")
        else:
            data["From"] = _env("TWILIO_FROM_NUMBER")
        try:
            resp = requests.post(TWILIO_URL.format(sid=sid), data=data, auth=auth, timeout=10)
        except requests.RequestException as exc:
            # The exception text can echo request details; report only its type.
            raise NotifyError(f"Twilio request failed ({type(exc).__name__})") from None
        if resp.status_code >= 300:
            raise NotifyError(twilio_error(resp))

    def send_push(self, topic, title, body, data):
        import requests

        project, access_token = self._require("FCM_PROJECT_ID"), self._require("FCM_ACCESS_TOKEN")
        try:
            resp = requests.post(
                f"https://fcm.googleapis.com/v1/projects/{project}/messages:send",
                json={"message": {"topic": topic, "notification": {"title": title, "body": body},
                                  "data": {k: str(v) for k, v in data.items()}, "android": {"priority": "high"}}},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
        except requests.RequestException as exc:
            raise NotifyError(f"FCM request failed ({type(exc).__name__})") from None
        if resp.status_code >= 300:
            raise NotifyError(f"FCM HTTP {resp.status_code}")


BACKENDS = {"console": ConsoleBackend, "twilio_fcm": TwilioFcmBackend}


def get_backend() -> NotificationBackend:
    from django.conf import settings

    return BACKENDS.get(settings.NOTIFY_BACKEND, ConsoleBackend)()
