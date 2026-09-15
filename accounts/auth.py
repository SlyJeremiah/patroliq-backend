"""
Token authentication (spec §5: DRF token, ``Authorization: Token <key>``, no JWT).

Tokens are opaque random keys stored server-side (``AuthToken``) so they can be revoked at any time
(logout, deactivation). They expire after a period of *inactivity* and are renewed on use:
rangers default to 168 h (a week offline in the field — PRD 6.1 "does not lock mid-patrol"),
web roles to 8 h (PRD 6.1 manager sessions). Expired -> ``401 token_expired``.

Safety endpoints use :class:`SafetyTokenAuthentication`, which ignores idle expiry (but not
revocation or deactivation): an SOS from a ranger who has been offline for weeks must still land.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import TokenAuthentication

from core.roles import RANGER

from .models import AuthToken

TOUCH_INTERVAL = timedelta(minutes=1)


def idle_limit(user) -> timedelta:
    hours = settings.RANGER_TOKEN_IDLE_HOURS if user.role == RANGER else settings.WEB_TOKEN_IDLE_HOURS
    return timedelta(hours=hours)


class ExpiringTokenAuthentication(TokenAuthentication):
    keyword = "Token"
    model = AuthToken
    enforce_idle_expiry = True

    def authenticate_credentials(self, key):
        try:
            token = AuthToken.objects.select_related("user", "user__organisation", "user__organisation__licence").get(key=key)
        except AuthToken.DoesNotExist:
            raise exceptions.AuthenticationFailed("Invalid token.", code="invalid_token")
        user = token.user
        if not user.is_active:
            raise exceptions.AuthenticationFailed("User inactive or deleted.", code="account_disabled")
        now = timezone.now()
        expired = now - token.last_used_at > idle_limit(user)
        if expired and self.enforce_idle_expiry:
            token.delete()
            raise exceptions.AuthenticationFailed("Session expired; sign in again.", code="token_expired")
        # An expired token accepted for a safety call is NOT renewed by it.
        if not expired and now - token.last_used_at > TOUCH_INTERVAL:
            AuthToken.objects.filter(pk=token.pk).update(last_used_at=now)
        return user, token


class SafetyTokenAuthentication(ExpiringTokenAuthentication):
    enforce_idle_expiry = False
