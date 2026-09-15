# PATROLIQ backend

Django REST API for the PATROLIQ offline-first wildlife-ranger patrol platform, operated by
zrGISsolutions for multiple licensed organisations. The contract shared with the Android app is
**[`docs/PATROLIQ_Platform_Spec_v1.2.md`](../docs/PATROLIQ_Platform_Spec_v1.2.md)** — paths and field
names there are authoritative; clarifications are in its "Backend notes" section.

Stack: Python 3.11 · Django 5.2 · Django REST Framework · shapely / pyproj / pyshp (no GDAL) · SQLite
(dev/test) or PostgreSQL (production, with row-level security) · pyotp (TOTP).

## Layout

| Path | What |
|---|---|
| `patroliq/` | settings (env-driven), URLs (`api_urls.py` = spec §5 routes) |
| `core/` | tenant base models & mixins, strict validation, error envelope, permissions, RLS middleware |
| `geo/` | **all spatial logic**: boundary import, repair, areas, reprojection, GRTS grid, cell lookup |
| `accounts/` | Organisation, Licence, User (custom), AuthToken, login lockout, TOTP, licensing rules |
| `areas/` | Area, ApuBase, Sector, GrtsCell, Team, Assignment, RiskScore, FeatureLayer, risk engine |
| `field/` | Species, Patrol, TrackPoint, Observation, Media, SafetyAlert, PositionPing, sync services |
| `audit/` | append-only AuditLog |
| `notify/` | SMS/push provider interface (console, Twilio/FCM) + NotificationLog |
| `platform_admin/` | zrGISsolutions `/platform/` endpoints (named to avoid shadowing stdlib `platform`) |
| `sql/postgres_rls.sql` | PostgreSQL RLS policies, audit-log trigger, app-role grants |
| `tests/` | pytest suite |

## Setup (Windows, PowerShell)

```powershell
cd D:\freelance\PatrolIQ\backend
& "C:\Program Files\Python311\python.exe" -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env        # then set DJANGO_DEBUG=true for local dev (or a DJANGO_SECRET_KEY)
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py seed_demo
.venv\Scripts\python manage.py runserver 0.0.0.0:8000
```

macOS/Linux: same with `.venv/bin/python`.

### Environment variables

All secrets come from the environment (optionally a git-ignored `.env`). See `.env.example`.

| Variable | Default | Notes |
|---|---|---|
| `DJANGO_SECRET_KEY` | — | **required** unless `DJANGO_DEBUG=true` (then an ephemeral key is generated) |
| `DJANGO_DEBUG` | `false` | never enable in production |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,10.0.2.2` | ignored (`*`) when DEBUG |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | — | for the ops admin behind HTTPS |
| `DATABASE_URL` | SQLite `backend/db.sqlite3` | web process URL; on Neon the **pooled** endpoint (see below) |
| `DATABASE_URL_DIRECT` | — | direct (non-pooled) endpoint; used by `migrate` & co. |
| `DJANGO_DB_DIRECT` | `false` (auto `true` for `migrate`, `makemigrations`, `showmigrations`, `sqlmigrate`, `dbshell`, `flush`) | force the direct URL for any command |
| `DB_CONN_MAX_AGE` / `DB_CONNECT_TIMEOUT` | 60 on PostgreSQL (0 on SQLite) / 15 s | persistent connections + health checks |
| `DJANGO_SECURE_SSL_REDIRECT`, `DJANGO_HSTS_SECONDS` | off / 0 | enable in production |
| `RANGER_TOKEN_IDLE_HOURS` / `WEB_TOKEN_IDLE_HOURS` | 168 / 8 | sliding idle expiry of API tokens |
| `LOGIN_MAX_FAILURES` / `LOGIN_LOCKOUT_MINUTES` | 5 / 15 | per-identifier lockout |
| `LOGIN_THROTTLE_RATE` | `30/min` | per-IP throttle on `auth/login/` |
| `DJANGO_ADMIN_ENABLED` | `true` | Django admin at `/ops-admin/` (superusers, password **+ TOTP**) |
| `MEDIA_ROOT` / `MEDIA_MAX_BYTES` | `backend/media` / 25 MB | uploaded photos/video/audio |
| `DATA_UPLOAD_MAX_BYTES` | 20 MB | max JSON body (sync push), also the gzip decompression cap |
| `NOTIFY_BACKEND` | `console` | `console` or `twilio_fcm` |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | — | SMS (twilio_fcm) |
| `FCM_PROJECT_ID`, `FCM_ACCESS_TOKEN` | — | push stub (FCM HTTP v1, topic `org-<id>-managers`) |

## Demo data (`manage.py seed_demo`)

Idempotent; resets the demo passwords each run and prints the TOTP secrets (they are generated once
per database — scan the printed `otpauth://` URI into Google Authenticator/Authy, or
`python -c "import pyotp; print(pyotp.TOTP('SECRET').now())"`). Refuses to run with DEBUG off unless
`--allow-production`.

| Who | Sign-in |
|---|---|
| Ranger Tendai Moyo (team at APU-2, assigned today to 5 cells incl. GRTS-047) | `GRTTS` / `RGR-2026-041` / `patrol123` |
| Ranger Farai Ncube | `GRTTS` / `RGR-2026-038` / `patrol123` |
| Manager Grace Mutasa | `grace.mutasa@grtts.co.zw` / `manager123` + TOTP |
| Org admin Tafadzwa Shumba | `tafadzwa.shumba@grtts.co.zw` / `admin123` + TOTP |
| Platform admin | `admin@zrgissolutions.com` / `platform123` + TOTP |
| Save Valley Trust ranger Blessing Dube (isolation proof) | `SVT` / `SVT-R-001` / `patrol123` |
| Save Valley Trust org admin | `admin@savevalley.example` / `admin123` + TOTP |

GRTTS: licence `standard`, all modules, expires 2027-09-30. **Mazowe Conservancy** (client Mazowe
Landholders Trust): ~12 × 10 km boundary near -17.50, 30.95; bases `APU-1 HQ Camp`,
`APU-2 Mazowe River`, `APU-3 Boundary Road`; 1 km GRTS grid; 3 sectors; demo roads/water layers; one
ended patrol with a snare report; 35 species; risk scores for today.

Recompute risk: `manage.py score_risk --date 2026-09-15 [--org GRTTS] [--area <uuid>] [--hour 20]`
(schedule nightly with cron / Task Scheduler).

## Tests

```powershell
.venv\Scripts\python -m pytest                      # SQLite in-memory (fast)
$env:PATROLIQ_TEST_DB="postgres"; .venv\Scripts\python -m pytest --create-db   # PostgreSQL from .env
```

`PATROLIQ_TEST_DB=postgres` uses `DATABASE_URL_DIRECT` (or `DATABASE_URL`) and lets pytest-django create
and drop `test_<dbname>` (the role needs CREATEDB; Neon's owner role has it). Against a remote Neon
branch this is slow (every query is a network round trip); `tests/test_postgres_tenant_context.py` only
runs on PostgreSQL and checks the transaction-local tenant id.

Covers tenant isolation, licence limits/suspension/SOS, login + lockout + TOTP, unexpected fields,
boundary import (shapefile zip in UTM with `.prj`, no-`.prj`, multi-feature, GeoJSON, KML, drawn),
bases outside boundary, grid counts/labels/sectors/GRTS balance, bootstrap + `since` + deletions, push
idempotency + cell assignment, media, positions, gzip bodies, sex/count validation, safety alerts
(notifications + audit), audit immutability, risk engine.

## API overview (`/api/v1/`)

Full contract: spec §5. Auth header `Authorization: Token <key>`. JSON snake_case, datetimes
`YYYY-MM-DDTHH:MM:SSZ` (UTC), geometries GeoJSON lon/lat.

* **Auth** — `auth/login/`, `auth/logout/`, `me/` (+ additive `auth/password/`)
* **Ranger sync** — `sync/bootstrap/?since=`, `sync/push/`, `media/` (+ `media/{id}/`, `media/{id}/file/`),
  `positions/`, `safety/alerts/`, `safety/alerts/{client_uuid}/cancel/`
* **Admin/manager** — `areas/` (+ `boundary/import/`, `boundary/`, `grid/generate/`, `activate/`, `cells/`,
  `sectors/`, additive `layers/roads|water/`), `apu-bases/`, `teams/`, `assignments/`, `users/`,
  `alerts/` (+ `acknowledge/`, additive `resolve/`), `observations/`, `patrols/`, `positions/latest/`,
  `audit-log/`, `species/`
* **Platform** — `platform/organisations/`, `…/{id}/`, `…/{id}/licence/`, `…/{id}/usage/`
* `GET /healthz/` — unauthenticated liveness + DB check

Errors are always `{"error": {"code", "message", "fields"?}}`. Lists are plain JSON arrays; add
`?limit=&offset=` to get `{count, next, previous, results}`.

### Android emulator

The emulator reaches the host's `localhost` at **`http://10.0.2.2:8000/api/v1/`**. Run the server with
`runserver 0.0.0.0:8000` (and `DJANGO_DEBUG=true`, or add your LAN IP to `DJANGO_ALLOWED_HOSTS` for a
physical device at `http://<pc-ip>:8000/api/v1/`). Plain HTTP is for development only; allow cleartext
for `10.0.2.2` in the debug `network_security_config.xml`.

## Tenancy & licensing model

* Each licensee is an **Organisation** with exactly one **Licence** (plan, `max_rangers`,
  `max_managers`, `max_areas`, `modules`, dates, `grace_days`).
* Every tenant table has `organisation_id`; every queryset is filtered by the caller's organisation
  (`core.tenancy.TenantScopedMixin`, `TenantPKField`), so foreign IDs behave as non-existent (404/400).
  IDs are UUIDs. Platform admins have no organisation and get 403 on tenant endpoints; their
  `/platform/` endpoints expose metadata and counts only.
* Effective status: `active` → after `expires_at` `grace` (sync works) → after `grace_days`
  `suspended` (login `403 licence_suspended`, all endpoints 403 except `me/`, `auth/logout/`,
  `safety/alerts/…`, `positions/` and the safety-alert part of `sync/push/`). zrGISsolutions can also
  suspend manually (organisation or licence `status`).
* Seats: rangers count against `max_rangers`; every other active org role (org_admin, manager,
  researcher, viewer) against `max_managers` → `402 licence_seat_limit`. Non-archived areas count
  against `max_areas` → `402 licence_area_limit`. Disabled modules → `403 module_disabled`
  (`grts` gates grid generation; `ai_risk` gates risk scores).
* Deployment: shared cloud (this isolation model) or a dedicated instance (same code, own DB; set
  `deployment=dedicated` for bookkeeping).

## Neon PostgreSQL

Neon gives two hosts for the same database:

| Endpoint | Host | Use |
|---|---|---|
| **Pooled** (PgBouncer, *transaction* mode) | `<endpoint>-pooler.<region>.aws.neon.tech` | `DATABASE_URL` — web process, `seed_demo`, `score_risk` |
| **Direct** | `<endpoint>.<region>.aws.neon.tech` | `DATABASE_URL_DIRECT` — migrations, `dbshell`, test database creation, `psql -f sql/postgres_rls.sql` |

```dotenv
# backend/.env (git-ignored) — placeholders, never commit real values
DATABASE_URL=postgresql://<user>:<password>@<endpoint>-pooler.<region>.aws.neon.tech/<db>?sslmode=require&channel_binding=require
DATABASE_URL_DIRECT=postgresql://<user>:<password>@<endpoint>.<region>.aws.neon.tech/<db>?sslmode=require&channel_binding=require
```

```powershell
.venv\Scripts\python manage.py migrate            # goes over DATABASE_URL_DIRECT automatically
.venv\Scripts\python manage.py seed_demo          # demo data (idempotent; DEBUG on or --allow-production)
.venv\Scripts\python manage.py score_risk         # nightly
```

How the code copes with transaction pooling (`patroliq/settings.py`, `core/db.py`, `core/middleware.py`):

* `DISABLE_SERVER_SIDE_CURSORS = True`, psycopg prepared statements off (Django default
  `prepare_threshold=None`), `CONN_MAX_AGE=60` with `CONN_HEALTH_CHECKS`, `sslmode`/`channel_binding`
  kept from the URL.
* No session state. `PostgresTenantMiddleware` runs every request in **one transaction** and sets the
  tenant with `set_config('app.org_id', <org>, true)` (transaction-local), so every query of the request
  sees it and it disappears at COMMIT — it can never leak to another client sharing the pooled server
  connection. `ATOMIC_REQUESTS` is deliberately **not** used: DRF would then roll back handled errors,
  losing e.g. failed-login lockout counters. A 5xx response rolls the request back (clients retry).
  Requests without a token (login, healthz) run with an empty tenant; `audit()` and
  `tenant_context()` scope a savepoint to a specific organisation.

### RLS status on Neon: **not applied** (application-layer isolation only)

`sql/postgres_rls.sql` has not been run against the Neon database, deliberately:

1. The only role is Neon's default owner role (`<db>_owner`), which owns the tables **and has `BYPASSRLS`** — policies (even with
   `FORCE ROW LEVEL SECURITY`) would not apply to it, so enabling RLS while the API connects as the owner
   adds nothing.
2. Real enforcement needs a separate non-owner app role for the web process, and the suite cannot verify
   that: tests (fixtures, `seed_demo`-style setup) write rows across tenants without a tenant id and run
   as the owner that creates the test database. There is no verified "suite passes with RLS on" run yet.

To enable it:

1. In the Neon console (or as the owner over the **direct** URL) create a role without BYPASSRLS, e.g.
   `CREATE ROLE patroliq_app LOGIN PASSWORD '<strong password>';` (Neon console roles are also fine, but
   check `SELECT rolbypassrls FROM pg_roles WHERE rolname='patroliq_app'` is `false`).
2. `python manage.py migrate` (owner, direct URL), then
   `psql "<owner DATABASE_URL_DIRECT>" -v app_role=patroliq_app -f sql/postgres_rls.sql`; re-run after
   every migration that adds tables.
3. Point the web process's `DATABASE_URL` at `patroliq_app` on the **pooled** host. Keep the owner URL
   for `DATABASE_URL_DIRECT`, `seed_demo`, `score_risk` and the ops admin (the owner is never locked out).
4. Verify on a Neon branch: the `psql` verification block at the end of the SQL file, then a login →
   bootstrap → push smoke test as the app role, and the cross-tenant tests in `tests/test_tenancy.py`
   against a deployment using the app role.

## Deploy (Render + Neon)

`render.yaml` (Blueprint), `Procfile` and `.python-version` (3.11) are included; `gunicorn` is pinned in
`requirements.txt`.

1. Render → New → Blueprint → this repository. Enter `DATABASE_URL` (Neon pooled) and
   `DATABASE_URL_DIRECT` (Neon direct) when prompted; `DJANGO_SECRET_KEY` is generated.
2. Build: `pip install -r requirements.txt && python manage.py collectstatic --noinput`.
   Pre-deploy: `python manage.py migrate --noinput` (over the direct URL). Start:
   `gunicorn patroliq.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 120`.
   (Plans without pre-deploy commands: append `&& python manage.py migrate --noinput` to the build.)
3. Service region `ohio` = Neon `us-east-2`. Health check `/healthz/`.
4. Set `DJANGO_ALLOWED_HOSTS` / `DJANGO_CSRF_TRUSTED_ORIGINS` to the real hostname; keep
   `DJANGO_DEBUG=false`, `DJANGO_SECURE_SSL_REDIRECT=true`, `DJANGO_HSTS_SECONDS=31536000`.
5. Uploads: `MEDIA_ROOT=/var/data/media` on the attached persistent disk (Render's filesystem is
   otherwise ephemeral). Schedule `python manage.py score_risk` as a Render cron job (daily).
6. Do not run `seed_demo` on a production database (it creates accounts with known passwords).

## PostgreSQL + RLS deployment (self-hosted)

1. Create the database and two roles: `patroliq_owner` (owns tables; runs migrations/commands) and
   `patroliq_app` (used by the web process).
2. `DATABASE_URL=postgres://patroliq_owner:…/patroliq python manage.py migrate`
3. `psql "postgres://patroliq_owner:…/patroliq" -v app_role=patroliq_app -f sql/postgres_rls.sql`
   (re-run after every migration that adds tables).
4. Run the web app with `DATABASE_URL=postgres://patroliq_app:…/patroliq`, `DJANGO_DEBUG=false`,
   `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_SECURE_SSL_REDIRECT=true`,
   `DJANGO_HSTS_SECONDS=31536000`, e.g. `gunicorn patroliq.wsgi` behind TLS.
5. Run `seed_demo` (never in production), `score_risk` and the ops admin with the **owner** URL:
   the app role only sees rows of the organisation in `app.org_id`, which
   `core.middleware.PostgresTenantMiddleware` sets transaction-locally for each request (one transaction
   per request) from the API token.

What RLS covers: all operational tables (areas, bases, sectors, cells, teams, assignments, risk,
patrols, track points, observations, media, safety alerts, positions, tombstones, audit, notifications).
Auth tables stay outside RLS because sign-in resolves an organisation before a tenant is known. The
audit log additionally has an UPDATE/DELETE/TRUNCATE-blocking trigger and no UPDATE/DELETE grant.

## PostGIS / GeoDjango migration path

Spatial data is GeoJSON in JSON columns and every spatial operation lives in `geo/` (see the
docstring in `geo/__init__.py`). When GDAL is available: switch to the PostGIS engine, add geometry
columns alongside the JSON ones and backfill, re-implement the `geo` functions with `ST_Covers`,
`ST_Area(geography)`, `ST_Distance`, `ST_Transform` and `ST_SquareGrid` (GRTS ordering stays in
Python), keep serialising GeoJSON so the API contract is unchanged, then drop the JSON columns.

## Security notes

* No JWT; opaque DB-backed tokens, revocable, idle-expiring (safety calls accept idle-expired tokens).
* 5 failed logins per identifier → 15-minute lockout (`429 locked_out`, `Retry-After`), unknown accounts
  behave identically; per-IP throttle; TOTP (RFC 6238, replay-protected) mandatory for manager,
  org_admin and platform_admin, also on the Django admin login.
* Strict input: unknown keys → `400 unexpected_fields`; free text HTML/script-stripped; notes ≤ 1000.
* Uploads stored under `MEDIA_ROOT/uploads/<org>/…` with server-generated names, type checked against
  `kind`, served only through the authenticated, tenant-scoped `media/{id}/file/`.
* Audit log for logins, admin changes, sync pushes, uploads and safety events; append-only.
* Not included yet: IP allow-listing for the dashboard, CORS/CSP for the future web frontend,
  encrypting TOTP secrets at rest (use a KMS-backed field when deploying).
