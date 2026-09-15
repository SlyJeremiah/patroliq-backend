from __future__ import annotations

import gzip
import io
import logging

from django.db import transaction
from django.http import JsonResponse

from .db import is_postgres, set_db_org
from .exceptions import error_body

logger = logging.getLogger("patroliq.middleware")


class PostgresTenantMiddleware:
    """
    Runs each request in one transaction with ``app.org_id`` set transaction-locally (PostgreSQL only).

    Why a request transaction: behind a transaction-mode pooler (Neon/PgBouncer) a session-level
    ``SET`` is unsafe, and a transaction-local one only reaches queries in the same transaction. So
    the whole request — token lookup, DRF authentication, the view and its audit rows — runs inside a
    single ``atomic`` block and the setting vanishes at COMMIT/ROLLBACK (no cross-request leak).

    DRF authenticates inside the view, i.e. after middleware, so the token is resolved here
    independently (a cheap lookup on the token table, which is not under RLS). Requests without a
    token (login, healthz, the ops admin) get an empty tenant; auth tables are outside RLS so login
    still resolves organisation/user, and ``audit()`` switches tenant just for its INSERT.

    Commit semantics match the previous autocommit behaviour for handled errors: DRF only marks a
    transaction for rollback when ``ATOMIC_REQUESTS`` is on (it is not), so e.g. a failed-login
    counter written before ``401 invalid_credentials`` is kept. A 5xx response rolls back.
    Inner ``transaction.atomic()`` blocks become savepoints. No-op on SQLite.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not is_postgres():
            return self.get_response(request)
        with transaction.atomic():
            org_id = None
            parts = request.META.get("HTTP_AUTHORIZATION", "").split()
            if len(parts) == 2 and parts[0] == "Token":
                from accounts.models import AuthToken

                org_id = (
                    AuthToken.objects.filter(key=parts[1]).values_list("user__organisation_id", flat=True).first()
                )
            set_db_org(org_id)
            response = self.get_response(request)
            if response.status_code >= 500:
                transaction.set_rollback(True)
            return response


class HealthCheckMiddleware:
    """
    Answers ``GET /healthz/`` before host validation and the HTTPS redirect: platform health checks
    (Render) call the instance over plain HTTP with an internal Host header, which ALLOWED_HOSTS or
    SECURE_SSL_REDIRECT would otherwise turn into a 400/301. Reveals nothing but DB reachability.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path_info == "/healthz/" and request.method in ("GET", "HEAD"):
            from patroliq.urls import healthz

            return healthz(request)
        return self.get_response(request)


class WebIpAllowlistMiddleware:
    """
    Optional web IP allowlist (spec §7, PRD 7.3). Active only when ``WEB_IP_ALLOWLIST`` is set.

    Restricted (``403 ip_not_allowed`` from a non-listed client IP):
      * requests carrying a token of a manager, org_admin, researcher, viewer or platform_admin;
      * the web form of ``auth/login/`` (body with ``email``);
      * the ops admin (``/ops-admin/``).
    Never restricted: rangers (their tokens and the organisation_code/employee_id login form),
    ``sync/``, ``safety/``, ``POST positions/``, ``/healthz/`` and CORS preflights.
    Runs after GzipRequestMiddleware (so the login body is readable) and below CorsMiddleware (so the
    403 still carries CORS headers for the dashboard). Invalid tokens pass through to DRF's 401.
    """

    EXEMPT_PREFIXES = ("/healthz/", "/api/v1/sync/", "/api/v1/safety/")
    EXEMPT_EXACT = {"/api/v1/positions/"}
    WEB_ROLES = {"manager", "org_admin", "researcher", "viewer", "platform_admin"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings

        allowlist = getattr(settings, "WEB_IP_ALLOWLIST", None)
        if allowlist and self._restricted(request):
            from .net import client_ip, ip_allowed

            if not ip_allowed(client_ip(request), allowlist):
                return JsonResponse(error_body("ip_not_allowed", "Access from this network is not allowed."),
                                    status=403)
        return self.get_response(request)

    def _restricted(self, request) -> bool:
        path = request.path
        if request.method == "OPTIONS" or path.startswith(self.EXEMPT_PREFIXES) or path in self.EXEMPT_EXACT:
            return False
        if path.startswith("/ops-admin/"):
            return True
        if path == "/api/v1/auth/login/":
            import json

            try:
                body = json.loads(request.body or b"{}")
            except (ValueError, UnicodeDecodeError):
                body = request.POST
            return bool(hasattr(body, "get") and body.get("email"))
        parts = request.META.get("HTTP_AUTHORIZATION", "").split()
        if len(parts) == 2 and parts[0] == "Token":
            from accounts.models import AuthToken

            role = AuthToken.objects.filter(key=parts[1]).values_list("user__role", flat=True).first()
            return role in self.WEB_ROLES
        return False


class GzipRequestMiddleware:
    """
    Accepts ``Content-Encoding: gzip`` request bodies (the Android OkHttp GzipRequestInterceptor,
    TechStack §8.1). Decompressed size is capped at DATA_UPLOAD_MAX_MEMORY_SIZE (zip-bomb guard).
    Multipart uploads are left alone.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.META.get("HTTP_CONTENT_ENCODING", "").lower() == "gzip" and request.path.startswith("/api/"):
            from django.conf import settings

            limit = settings.DATA_UPLOAD_MAX_MEMORY_SIZE or 20 * 1024 * 1024
            try:
                raw = request.read()
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                    body = gz.read(limit + 1)
            except (OSError, EOFError):
                return JsonResponse(error_body("parse_error", "Invalid gzip request body."), status=400)
            if len(body) > limit:
                return JsonResponse(error_body("payload_too_large", "Request body too large."), status=413)
            request._body = body
            request._stream = io.BytesIO(body)
            request.META["CONTENT_LENGTH"] = str(len(body))
            request.META.pop("HTTP_CONTENT_ENCODING", None)
        return self.get_response(request)
