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

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        # Ephemeral per-process key for local development only. API tokens are stored in the
        # database, so they survive restarts; only Django-admin sessions are invalidated.
        from django.core.management.utils import get_random_secret_key

        SECRET_KEY = get_random_secret_key()
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,10.0.2.2")
if DEBUG:
    ALLOWED_HOSTS = ["*"]
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

ADMIN_ENABLED = env_bool("DJANGO_ADMIN_ENABLED", True)

INSTALLED_APPS = [
    "accounts.admin_apps.PatrolIQAdminConfig",  # django.contrib.admin with a TOTP-protected login
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "core",
    "accounts",
    "areas",
    "field",
    "audit",
    "notify",
    "platform_admin",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "core.middleware.GzipRequestMiddleware",
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
MEDIA_MAX_BYTES = env_int("MEDIA_MAX_BYTES", 25 * 1024 * 1024)
DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("DATA_UPLOAD_MAX_BYTES", 20 * 1024 * 1024)
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# --- PATROLIQ auth policy -------------------------------------------------------------------
RANGER_TOKEN_IDLE_HOURS = env_int("RANGER_TOKEN_IDLE_HOURS", 168)
WEB_TOKEN_IDLE_HOURS = env_int("WEB_TOKEN_IDLE_HOURS", 8)
LOGIN_MAX_FAILURES = env_int("LOGIN_MAX_FAILURES", 5)
LOGIN_LOCKOUT_MINUTES = env_int("LOGIN_LOCKOUT_MINUTES", 15)
TOTP_ISSUER = os.environ.get("TOTP_ISSUER", "PATROLIQ")

NOTIFY_BACKEND = os.environ.get("NOTIFY_BACKEND", "console")

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
    "DEFAULT_THROTTLE_RATES": {"login": os.environ.get("LOGIN_THROTTLE_RATE", "30/min")},
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
