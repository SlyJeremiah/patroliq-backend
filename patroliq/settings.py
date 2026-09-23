"""
Django settings for the PATROLIQ backend.

Security decisions (PRD 7.2):
* Every secret is read from the environment (optionally loaded from a git-ignored ``.env``).
  Nothing sensitive is hard-coded; production refuses to start without DJANGO_SECRET_KEY.
* DEBUG is off unless DJANGO_DEBUG=true.
* SQLite is the zero-config dev/test database; production uses PostgreSQL (Neon) via DATABASE_URL
  (pooled) / DATABASE_URL_DIRECT (migrations), where row-level security (sql/postgres_rls.sql) can add
  a second tenant-isolation layer.
"""
from __future__ import annotations

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def env_list(name: str, default: str = "") -> list[str]:
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or os.environ.get("SECRET_KEY") or ""
if not SECRET_KEY:
    if DEBUG:
        # Ephemeral per-process key for local development only. API tokens are stored in the
        # database, so they survive restarts; only Django-admin sessions are invalidated.
        from django.core.management.utils import get_random_secret_key

        SECRET_KEY = get_random_secret_key()
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,10.0.2.2")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
# Render sets RENDER_EXTERNAL_HOSTNAME (e.g. patroliq-api.onrender.com) on every web service.
_RENDER_HOST = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
if _RENDER_HOST:
    ALLOWED_HOSTS.append(_RENDER_HOST)
    CSRF_TRUSTED_ORIGINS.append(f"https://{_RENDER_HOST}")
if DEBUG:
    ALLOWED_HOSTS = ["*"]

ADMIN_ENABLED = env_bool("DJANGO_ADMIN_ENABLED", True)

INSTALLED_APPS = [
    "accounts.admin_apps.PatrolIQAdminConfig",  # django.contrib.admin with a TOTP-protected login
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "core",
    "accounts",
    "areas",
    "field",
    "audit",
    "notify",
    "platform_admin",
    "dashboard",
]

MIDDLEWARE = [
    "core.middleware.HealthCheckMiddleware",  # before host validation / SSL redirect (platform health checks)
    "corsheaders.middleware.CorsMiddleware",  # early, so every response (incl. 403 ip_not_allowed) gets CORS headers
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "core.middleware.GzipRequestMiddleware",
    "core.middleware.WebIpAllowlistMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.PostgresTenantMiddleware",
]

ROOT_URLCONF = "patroliq.urls"
WSGI_APPLICATION = "patroliq.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --- Database ---------------------------------------------------------------------------------
# DATABASE_URL        the web process's URL. On Neon this is the *pooled* endpoint (PgBouncer in
#                     transaction mode, host contains "-pooler").
# DATABASE_URL_DIRECT optional direct (non-pooled) endpoint. Used instead of DATABASE_URL when
#                     DJANGO_DB_DIRECT=true, which manage.py sets automatically for migrate and other
#                     schema/session-level commands (see DIRECT_DB_COMMANDS in manage.py).
# Transaction pooling means consecutive transactions may run on different server connections, so:
#   * no server-side cursors (named cursors span transactions),
#   * no session state: the tenant id is set per transaction (core.db / core.middleware),
#   * psycopg 3 prepared statements stay disabled (Django's default prepare_threshold=None).
# sslmode / channel_binding query parameters of the URL are preserved in OPTIONS.
_SQLITE_URL = f"sqlite:///{(BASE_DIR / 'db.sqlite3').as_posix()}"
_DB_DIRECT = env_bool("DJANGO_DB_DIRECT", False) and bool(os.environ.get("DATABASE_URL_DIRECT"))
_DB_URL = os.environ.get("DATABASE_URL_DIRECT" if _DB_DIRECT else "DATABASE_URL") or _SQLITE_URL
_DB_IS_POSTGRES = _DB_URL.startswith(("postgres://", "postgresql://", "pgsql://"))

DATABASES = {
    "default": dj_database_url.parse(
        _DB_URL,
        # Persistent connections are safe (no session state is relied on) and save a TLS handshake
        # per request; health checks drop connections the pooler or Neon's autosuspend closed.
        conn_max_age=env_int("DB_CONN_MAX_AGE", 60 if _DB_IS_POSTGRES else 0),
        conn_health_checks=_DB_IS_POSTGRES,
        disable_server_side_cursors=_DB_IS_POSTGRES,
    )
}
if _DB_IS_POSTGRES:
    DATABASES["default"].setdefault("OPTIONS", {}).setdefault("connect_timeout", env_int("DB_CONNECT_TIMEOUT", 15))

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "en"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT") or (BASE_DIR / "media"))

# --- File storage (uploads + generated reports) ------------------------------------------------
# FILE_STORAGE = r2 | database | local. Default: r2 when the Cloudflare R2 variables are all set; otherwise
# database on Render (its filesystem is wiped on every deploy/restart, which silently loses photos);
# otherwise the local filesystem under MEDIA_ROOT. With r2 the default storage is the S3-compatible
# R2 bucket (django-storages); with database files are rows in core.StoredFile. Files are never served from
# public bucket URLs: every download goes through an authenticated, tenant-scoped API view that
# streams the object (the bucket should stay private).
R2_BUCKET = os.environ.get("R2_BUCKET", "").strip()
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
USE_R2 = all([R2_BUCKET, R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY])
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
FILE_STORAGE = (os.environ.get("FILE_STORAGE", "").strip().lower()
                or ("r2" if USE_R2 else "database" if os.environ.get("RENDER") else "local"))
if FILE_STORAGE not in {"r2", "database", "local"}:
    raise ImproperlyConfigured("FILE_STORAGE must be r2, database or local.")
if FILE_STORAGE == "r2" and not USE_R2:
    raise ImproperlyConfigured("FILE_STORAGE=r2 needs R2_BUCKET, R2_ACCOUNT_ID, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY.")
if FILE_STORAGE == "database":
    STORAGES["default"] = {"BACKEND": "core.storage.DatabaseStorage"}
if FILE_STORAGE == "r2":
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": R2_BUCKET,
            "endpoint_url": os.environ.get("R2_ENDPOINT_URL") or f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            "access_key": R2_ACCESS_KEY_ID,
            "secret_key": R2_SECRET_ACCESS_KEY,
            "region_name": "auto",
            "signature_version": "s3v4",
            "location": os.environ.get("R2_LOCATION", ""),
            "default_acl": None,
            "querystring_auth": True,
            "file_overwrite": False,
        },
    }
MEDIA_MAX_BYTES = env_int("MEDIA_MAX_BYTES", 25 * 1024 * 1024)
DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("DATA_UPLOAD_MAX_BYTES", 20 * 1024 * 1024)
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# --- PATROLIQ auth policy -------------------------------------------------------------------
RANGER_TOKEN_IDLE_HOURS = env_int("RANGER_TOKEN_IDLE_HOURS", 168)
WEB_TOKEN_IDLE_HOURS = env_int("WEB_TOKEN_IDLE_HOURS", 8)
# A ranger off patrol shows "online" on the dashboard when their phone reached the API within this many minutes
# (idle phones sync every 30 min).
RANGER_ONLINE_MINUTES = env_int("RANGER_ONLINE_MINUTES", 35)
LOGIN_MAX_FAILURES = env_int("LOGIN_MAX_FAILURES", 5)
LOGIN_LOCKOUT_MINUTES = env_int("LOGIN_LOCKOUT_MINUTES", 15)
TOTP_ISSUER = os.environ.get("TOTP_ISSUER", "PATROLIQ")

# Two-factor sign-in (TOTP) for managers, org admins and platform admins (PRD 6.1 / 7.3). On by default.
# WEB_TOTP_REQUIRED=false accepts email + password only (demos, pilots). Not recommended in production.
WEB_TOTP_REQUIRED = env_bool("WEB_TOTP_REQUIRED", True)

NOTIFY_BACKEND = os.environ.get("NOTIFY_BACKEND", "console")

# --- Notifications v1.6: background delivery, email (SMTP), SMS numbers --------------------------
# Provider calls run after the request's transaction commits on a small thread pool so an SMTP/Twilio
# timeout never delays or fails a ranger's sync or SOS. NOTIFY_ASYNC=false sends inline (tests).
NOTIFY_ASYNC = env_bool("NOTIFY_ASYNC", True)
NOTIFY_WORKERS = env_int("NOTIFY_WORKERS", 2)
# Numbers without a country code are read as this country (Zimbabwe): 0771234567 -> +263771234567.
SMS_DEFAULT_COUNTRY_CODE = os.environ.get("SMS_DEFAULT_COUNTRY_CODE", "263").strip().lstrip("+") or "263"
# Email is "configured" when EMAIL_HOST is set; otherwise the console backend prints messages (dev)
# and every email is logged in NotificationLog as skipped.
EMAIL_HOST = os.environ.get("EMAIL_HOST", "").strip()
EMAIL_PORT = env_int("EMAIL_PORT", 587)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "").strip()
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
if EMAIL_USE_SSL:
    EMAIL_USE_TLS = False  # mutually exclusive in Django's SMTP backend (SSL wins: port 465)
EMAIL_TIMEOUT = env_int("EMAIL_TIMEOUT", 15)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "").strip() or EMAIL_HOST_USER or "PATROLIQ Alerts <alerts@localhost>"
SERVER_EMAIL = DEFAULT_FROM_EMAIL
# IPv4-only SMTP by default: hosts like Render have no IPv6 egress, and SMTP providers publish AAAA records.
EMAIL_FORCE_IPV4 = env_bool("EMAIL_FORCE_IPV4", True)
EMAIL_BACKEND = (("notify.smtp4.EmailBackend" if EMAIL_FORCE_IPV4 else "django.core.mail.backends.smtp.EmailBackend")
                 if EMAIL_HOST else "django.core.mail.backends.console.EmailBackend")
EMAIL_ALERTS = env_bool("EMAIL_ALERTS", True)  # safety + threat alert emails
EMAIL_SYNC_SUMMARIES = env_bool("EMAIL_SYNC_SUMMARIES", True)  # sync summary email after reportable pushes

# --- Web dashboard: CORS + network ---------------------------------------------------------------
# Token auth only (no cookies), so credentials are never allowed cross-origin.
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS")
CORS_ALLOWED_ORIGIN_REGEXES = [r for r in [os.environ.get("CORS_ALLOWED_ORIGIN_REGEX", "").strip()] if r]
CORS_ALLOW_HEADERS = ["authorization", "content-type"]
CORS_EXPOSE_HEADERS = ["retry-after", "content-disposition"]
CORS_ALLOW_CREDENTIALS = False
CORS_URLS_REGEX = r"^/(api/|healthz/)"
CORS_PREFLIGHT_MAX_AGE = 3600

# Optional allowlist (PRD 7.3) for web roles: comma-separated CIDRs; empty = no restriction.
WEB_IP_ALLOWLIST = env_list("WEB_IP_ALLOWLIST")
# Number of reverse proxies in front of the app that append to X-Forwarded-For (Render: 1).
# The client IP is the right-most untrusted hop. 0 = use REMOTE_ADDR.
TRUSTED_PROXY_COUNT = env_int("TRUSTED_PROXY_COUNT", 1 if os.environ.get("RENDER") else 0)
TRUSTED_PROXY_HOPS = TRUSTED_PROXY_COUNT  # backwards-compatible name used by audit.utils

# Optional public URL of the web dashboard; report share links point there when set
# (``<DASHBOARD_URL>/reports/shared/<token>``), otherwise at the API endpoint.
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").strip().rstrip("/")
REPORT_SHARE_HOURS = env_int("REPORT_SHARE_HOURS", 48)

# Kernel density heat map (spec v1.5 §C): how long a computed surface stays cached. The cache key
# already carries the newest source record's updated_at, so new synced data recomputes immediately.
HEATMAP_CACHE_SECONDS = env_int("HEATMAP_CACHE_SECONDS", 600)

# Patrol track sanitising (geo/track.py): a stored track point whose reported accuracy is worse
# than this many metres is ignored when a patrol's distance and drawn track are computed. The raw
# rows are always kept. 35 m matches the accuracy gate the Android app applies on the device.
TRACK_MAX_ACCURACY_M = env_int("TRACK_MAX_ACCURACY_M", 35)

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["accounts.auth.ExpiringTokenAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
        "core.permissions.LicenceNotSuspended",
    ],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
        "rest_framework.parsers.FormParser",
    ],
    "EXCEPTION_HANDLER": "core.exceptions.api_exception_handler",
    # Plain JSON arrays unless the client passes ?limit= (then {count,next,previous,results}).
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": None,
    "DATETIME_FORMAT": "%Y-%m-%dT%H:%M:%SZ",
    "DEFAULT_THROTTLE_RATES": {"login": os.environ.get("LOGIN_THROTTLE_RATE", "30/min"), "notify_test": "5/hour"},
    "UNAUTHENTICATED_USER": None,
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# --- Transport / header hardening ------------------------------------------------------------
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", False)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = env_int("DJANGO_HSTS_SECONDS", 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
SESSION_COOKIE_AGE = 8 * 3600

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "patroliq": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
    },
}
