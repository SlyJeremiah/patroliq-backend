"""
Test settings: fast hashing, isolated media root, relaxed per-IP throttle.

Database: SQLite in-memory by default. ``PATROLIQ_TEST_DB=postgres`` runs the suite against the
PostgreSQL server in DATABASE_URL_DIRECT (or DATABASE_URL) from the environment / ``.env``;
pytest-django creates and drops a ``test_<dbname>`` database there, so the role needs CREATEDB.
"""
import os
import tempfile

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-not-a-secret")
if os.environ.get("PATROLIQ_TEST_DB", "sqlite").strip().lower() in ("postgres", "postgresql"):
    os.environ["DJANGO_DB_DIRECT"] = "true"  # CREATE/DROP DATABASE must not go through the pooler
else:
    os.environ["DATABASE_URL"] = "sqlite://:memory:"
    os.environ["DJANGO_DB_DIRECT"] = "false"

from .settings import *  # noqa: E402,F401,F403

DEBUG = False
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
MEDIA_ROOT = tempfile.mkdtemp(prefix="patroliq-test-media-")
NOTIFY_BACKEND = "console"
NOTIFY_ASYNC = False  # deliver inline so tests can assert on NotificationLog / mail.outbox
EMAIL_HOST = ""  # never an SMTP server from a developer's .env; tests opt in via the settings fixture
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_ALERTS = True
EMAIL_SYNC_SUMMARIES = True
SMS_DEFAULT_COUNTRY_CODE = "263"
REST_FRAMEWORK = {**REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {  # noqa: F405
    "login": "10000/min", "notify_test": "5/hour"}}
ALLOWED_HOSTS = ["*"]
