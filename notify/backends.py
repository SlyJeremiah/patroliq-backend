"""
Notification provider interface.

``NOTIFY_BACKEND``:
  * ``console`` (default, dev/test) — logs the message; nothing leaves the server.
  * ``twilio_fcm`` — SMS through Twilio's REST API and push through FCM HTTP v1. Credentials are read
    from the environment at send time (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER,
    FCM_PROJECT_ID, FCM_ACCESS_TOKEN). The FCM part is a stub: obtaining the OAuth access token
    from a service account (google-auth) is left to deployment; pushes go to the topic
    ``org-<organisation_id>-managers`` so no device-token registry is needed yet.
All server-side: no provider key is ever exposed to clients (PRD 7.2).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("patroliq.notify")


class NotifyError(Exception):
    pass


class NotificationBackend:
    name = "base"

    def send_sms(self, to: str, body: str) -> None:
        raise NotImplementedError

    def send_push(self, topic: str, title: str, body: str, data: dict) -> None:
        raise NotImplementedError


class ConsoleBackend(NotificationBackend):
    name = "console"

    def send_sms(self, to, body):
        logger.warning("[SMS -> %s] %s", to, body)

    def send_push(self, topic, title, body, data):
        logger.warning("[PUSH -> %s] %s: %s %s", topic, title, body, data)


class TwilioFcmBackend(NotificationBackend):
    name = "twilio_fcm"

    @staticmethod
    def _env(name: str) -> str:
        value = os.environ.get(name, "")
        if not value:
            raise NotifyError(f"{name} is not configured")
        return value

    def send_sms(self, to, body):
        import requests

        sid, token, sender = self._env("TWILIO_ACCOUNT_SID"), self._env("TWILIO_AUTH_TOKEN"), self._env("TWILIO_FROM_NUMBER")
        if not to:
            raise NotifyError("recipient has no phone number")
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data={"To": to, "From": sender, "Body": body[:1600]},
            auth=(sid, token),
            timeout=10,
        )
        if resp.status_code >= 300:
            raise NotifyError(f"Twilio HTTP {resp.status_code}")

    def send_push(self, topic, title, body, data):
        import requests

        project, access_token = self._env("FCM_PROJECT_ID"), self._env("FCM_ACCESS_TOKEN")
        resp = requests.post(
            f"https://fcm.googleapis.com/v1/projects/{project}/messages:send",
            json={"message": {"topic": topic, "notification": {"title": title, "body": body},
                              "data": {k: str(v) for k, v in data.items()}, "android": {"priority": "high"}}},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if resp.status_code >= 300:
            raise NotifyError(f"FCM HTTP {resp.status_code}")


BACKENDS = {"console": ConsoleBackend, "twilio_fcm": TwilioFcmBackend}


def get_backend() -> NotificationBackend:
    from django.conf import settings

    return BACKENDS.get(settings.NOTIFY_BACKEND, ConsoleBackend)()
