# PATROLIQ backend

Django REST API for the PATROLIQ offline-first wildlife-ranger patrol platform, operated by
zrGISsolutions for multiple licensed organisations. The contract shared with the Android app is
**[`docs/PATROLIQ_Platform_Spec_v1.2.md`](../docs/PATROLIQ_Platform_Spec_v1.2.md)** — paths and field
names there are authoritative; clarifications are in its "Backend notes" sections (latest:
**Backend notes v1.5**, covering the human–wildlife conflict alert, user personal details, the
kernel density heat map and the species catalogue, specified in
[`docs/PATROLIQ_v1.5_changes.md`](../docs/PATROLIQ_v1.5_changes.md)).

Stack: Python 3.11 · Django 5.2 · Django REST Framework · shapely / pyproj / pyshp (no GDAL) · SQLite
(dev/test) or PostgreSQL (production, with row-level security) · pyotp (TOTP).

## Layout

| Path | What |
|---|---|
| `patroliq/` | settings (env-driven), URLs (`api_urls.py` = spec §5 routes) |
| `core/` | tenant base models & mixins, strict validation, error envelope, permissions, RLS middleware |
| `geo/` | **all spatial logic**: boundary import, repair, areas, reprojection, GRTS grid, cell lookup, kernel density (`density.py`), track sanitising (`track.py`) |
| `accounts/` | Organisation, Licence, User (custom), AuthToken, login lockout, TOTP, licensing rules |
| `areas/` | Area, ApuBase, Sector, GrtsCell, Team, Assignment, RiskScore, FeatureLayer, risk engine |
| `field/` | Species, Patrol, TrackPoint, Observation, Media, SafetyAlert (panic / DMS / HWC), PositionPing, sync services, `hwc.py` details log |
| `audit/` | append-only AuditLog |
| `notify/` | SMS/push provider interface (console, Twilio/FCM), SMTP email, E.164 phones, background delivery, NotificationLog, `notify/status/` + `notify/test/` |
| `platform_admin/` | zrGISsolutions `/platform/` endpoints (named to avoid shadowing stdlib `platform`) |
| `dashboard/` | manager dashboard API (spec §7): summary, live rangers, coverage, risk, KDE heat map (`heatmap.py`), reports (PDF/CSV/GeoJSON) |
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
| `MEDIA_ROOT` / `MEDIA_MAX_BYTES` | `backend/media` / 25 MB | uploaded photos/video/audio and generated reports (local storage) |
| `R2_BUCKET`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | — | all four set → uploads + reports go to a **private** Cloudflare R2 bucket (django-storages S3); downloads still stream through the API. Optional `R2_ENDPOINT_URL`, `R2_LOCATION` |
| `CORS_ALLOWED_ORIGINS` | — | comma-separated dashboard origins, e.g. `https://patroliq-dashboard.vercel.app` |
| `CORS_ALLOWED_ORIGIN_REGEX` | — | e.g. `^https://patroliq-dashboard-[a-z0-9-]+\.vercel\.app$` (preview deploys) |
| `WEB_IP_ALLOWLIST` | — (off) | comma-separated CIDRs; web roles + web sign-in from other IPs → `403 ip_not_allowed` |
| `TRUSTED_PROXY_COUNT` | 1 on Render (`RENDER` set), else 0 | proxies appending to `X-Forwarded-For`; client IP = right-most untrusted hop |
| `DASHBOARD_URL` / `REPORT_SHARE_HOURS` | — / 48 | base URL for report share links / share lifetime |
| `HEATMAP_CACHE_SECONDS` | 600 | TTL of a computed KDE surface (`areas/{id}/heatmap/`); newly synced records invalidate it regardless |
| `TRACK_MAX_ACCURACY_M` | 35 | accuracy gate of the patrol track sanitiser (see **Patrol track quality**) |
| `DATA_UPLOAD_MAX_BYTES` | 20 MB | max JSON body (sync push), also the gzip decompression cap |
| `NOTIFY_BACKEND` | `console` | `console` or `twilio_fcm` |
| `NOTIFY_ASYNC` / `NOTIFY_WORKERS` | `true` / 2 | deliver after commit on a background thread pool |
| `SMS_DEFAULT_COUNTRY_CODE` | `263` | country of numbers entered without a code |
| `TWILIO_ACCOUNT_SID` | — | SMS (twilio_fcm), always required |
| `TWILIO_API_KEY_SID` + `TWILIO_API_KEY_SECRET` **or** `TWILIO_AUTH_TOKEN` | — | Twilio auth (API key preferred) |
| `TWILIO_MESSAGING_SERVICE_SID` **or** `TWILIO_FROM_NUMBER` | — | SMS sender |
| `FCM_PROJECT_ID`, `FCM_ACCESS_TOKEN` | — | push stub (FCM HTTP v1, topic `org-<id>-managers`) |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | —, 587 | SMTP; empty host = email not configured (console + logged as skipped) |
| `EMAIL_USE_TLS` / `EMAIL_USE_SSL` / `EMAIL_TIMEOUT` | `true` / `false` / 15 | STARTTLS on 587, or SSL on 465 |
| `DEFAULT_FROM_EMAIL` | `EMAIL_HOST_USER` | e.g. `PATROLIQ Alerts <alerts@example.org>` |
| `EMAIL_ALERTS` / `EMAIL_SYNC_SUMMARIES` | `true` / `true` | alert emails / sync summary emails |

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
`APU-2 Mazowe River`, `APU-3 Boundary Road`; 1 km GRTS grid; 3 sectors; demo roads/water layers. The
species catalogue (220 Zimbabwe species with `taxon_group` and IUCN status) is installed by the
`field/0004_species_catalogue` migration, so `seed_demo` is not needed for it.

Dashboard demo data (GRTTS, relative to the time the seed runs, so re-run it to refresh "today"):

* 9 rangers in three teams — Mazowe River Team at APU-2 (Tendai Moyo, Farai Ncube, Rudo Chikore),
  HQ Camp Team at APU-1 (Sipho Ndlovu, Kuda Dube, Blessing Nyathi), Boundary Road Team at APU-3
  (Precious Mpofu, Lindiwe Sibanda, Tatenda Gumbo); all `GRTTS` / `RGR-2026-038…048` / `patrol123`.
* ~65 patrols over the past 30 days with realistic foot/vehicle tracks (~7 000 track points) inside
  Mazowe cells and ~160 observations (wildlife with sex/counts, snares, fence cuts, carcasses).
* Live now: Tendai **active** (pings every few minutes, critical **poacher camp** alert), Sipho **paused**,
  Precious with an open patrol but **offline** (last ping 55 min ago).
* A poached elephant carcass alert (acknowledged), a dead man's switch alert for Kuda (acknowledged,
  responders dispatched, resolved), yesterday's high snare alert (open).
* Risk scores for the last 31 days (trend charts) and two generated reports (patrol summary PDF,
  wildlife census CSV). Everything is bulk inserted with fixed ids, so re-running replaces the same rows.

Recompute risk: `manage.py score_risk --date 2026-09-15 [--org GRTTS] [--area <uuid>] [--hour 20]`
(schedule nightly with cron / Task Scheduler).

Recompute sanitised distances: `manage.py clean_tracks [--org GRTTS] [--area <uuid>]
[--since 2026-09-01] [--dry-run]` — see **Patrol track quality** below.

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
(notifications + audit), audit immutability, risk engine, and the dashboard API (`test_dashboard.py`,
`test_reports.py`, `test_network.py`: roles, tenancy, live status, coverage statuses, risk trend, dispatch,
grid preview, every report type/format, anonymisation, share expiry, CORS, IP allowlist, seeded summary).

v1.5 additions: `test_safety.py` covers the human–wildlife conflict alert (raise, notification, details
merge on replay, never rejected by malformed details, severity rules, area filter, tenancy, push path);
`test_heatmap.py` unit-tests the KDE maths in `geo/density.py` (symmetry, mass conservation, the
Silverman bandwidth rule) and then the `areas/{id}/heatmap/` response shape, caching, sources, empty
case, role/module gating and the risk-engine factor; `test_user_profile.py` covers the personal fields
(validation, `full_name` derivation, org_admin-only writes, and that they never leak into
`me/`/`auth/login`/`rangers/`/`sync/bootstrap`); `test_risk_and_validation.py` checks that the species
catalogue migration is applied.

v1.6 additions: `test_notifications.py` covers E.164 normalisation and masking, phone validation on
`users/`, alert emails (recipients, body, no personal fields), the sync summary (new observation, patrol
start/end, urgent prefix; none for track points only, replays, or with `EMAIL_SYNC_SUMMARIES=false`),
email failures not breaking a push, Twilio error formatting / API-key auth / messaging service (mocked
`requests.post`), the single `no manager has a phone number` row, and `notify/status/` + `notify/test/`
(shape, masking, role gating, tenancy, throttle, audit).

v1.7 additions: `test_track_clean.py` unit-tests the track sanitiser in `geo/track.py` (a clean
synthetic walk measured within a few %, the same walk with injected 300 m outliers coming back within
6 % of it, a stationary noisy sequence measuring 0 m, the anchor-relative drift radius, the accuracy
gate and null accuracies, duplicate/out-of-order timestamps, each patrol-type speed ceiling, and a bad
first fix not rejecting the whole track), then `patrols/{id}/track/` with and without `?clean=` and the
`clean_tracks` command (dry run writes nothing, a client-supplied `distance_m` is never overwritten, a
server-owned one is corrected, idempotent, `--org`/`--area`/`--since` filters).

## Patrol track quality

Up to app v1.6 the ranger app recorded positions from the GPS **and** the network (cell / Wi-Fi)
provider behind a loose 50 m accuracy gate, with no jump or drift filtering. Network fixes land
hundreds of metres away, so stored `field_trackpoint` rows contain outliers that inflate patrol
distance and make the dashboard track zig-zag. The app now filters on the device; the server
sanitises too, so tracks already stored — and any older app still in the field — are handled.

`geo/track.py` (`geo.clean_track`) takes a time-ordered sequence of
`(lat, lon, accuracy_m, speed_mps, recorded_at)` and returns the kept fixes plus the distance over
them. It is pure — no database, no models — and unit-tested in `tests/test_track_clean.py`. Rules,
in order, mirroring the app's new filter:

| Rule | Threshold | Effect |
|---|---|---|
| Sanity | finite WGS84 lat/lon + a timestamp | dropped (`invalid`) |
| Accuracy | worse than `TRACK_MAX_ACCURACY_M` (35 m) | dropped (`accuracy`). A **null** accuracy is *unknown*, not *bad*: kept, and still jump/drift checked |
| Time | not strictly after the previous in-order fix | dropped (`time`) — duplicates and out-of-order points; the input is never re-sorted |
| Jump | implied **or** reported speed above the patrol-type ceiling: foot 6, horseback 10, boat 20, vehicle 35 m/s | dropped (`jump`). Speed is measured from the last *kept* fix, so a lone outlier is discarded and the track continues. After 3 rejections in a row the next fix becomes a new anchor with a zero-length leg, so one bad *first* fix (or a recording gap) cannot reject the whole track |
| Drift | movement under `max(8 m, 2 × accuracy)` from the last kept fix | dropped (`drift`); the anchor stays put, so jitter around a stationary ranger never accumulates |
| Leg floor | legs under 3 m add no distance | a backstop below the drift floor |

With the defaults the served distance always equals the length of the served geometry.

**Raw track points are never modified or deleted** — only derived numbers change:

* `Patrol.distance_clean_m` (migration `field/0005_patrol_distance_clean_m`, nullable) holds the
  sanitised distance. It is **NULL** when the patrol has no track points at all — nothing to
  measure is not the same as "walked nowhere".
* `Patrol.distance_m` keeps its old meaning: the client's figure when `distance_from_client`, else
  the server's. When the server owns it, it is now the sanitised total.
* `Patrol.effective_distance_m` = the sanitised distance when there is one, else the stored one.
  **Aggregates prefer it** (`dashboard/summary/`, `rangers/` today totals and current patrol, every
  report table and the report map's `distance_m`), because the sanitised figure is the better
  estimate and reports should not quote an inflated number. Payloads that mirror the row itself
  (`GET patrols/`, `patrols/{id}/track/`) carry `distance_m` **and** `distance_clean_m` side by
  side, so a client can still see exactly what was stored.
* Report map tracks (GeoJSON) are drawn from the sanitised points too.

`GET patrols/{client_uuid}/track/?clean=true|false` (**default true**) chooses which points the
LineString and `times` use. The response shape is otherwise unchanged; the properties always carry
`distance_m` (as stored on the patrol), `distance_clean_m` (measured over the sanitised track) and
`points_dropped`, plus `clean` echoing the flag.

```powershell
.venv\Scripts\python manage.py clean_tracks --dry-run              # report, write nothing
.venv\Scripts\python manage.py clean_tracks --org GRTTS --since 2026-09-01
```

`clean_tracks [--org CODE] [--area UUID] [--since YYYY-MM-DD] [--dry-run]` backfills
`distance_clean_m` for patrols already stored (and corrects `distance_m` where the server owns it),
printing per-organisation and overall totals: patrols examined, rows changed, points dropped, how
many distances move by 1 m or more, and stored → clean kilometres with the delta and percentage.
It is idempotent. Against the seeded demo data (67 patrols, 7 824 points) it drops 54 drift points
and changes the total by −0.2 %: `seed_demo` generates plausible fixes, so there is little to fix —
almost all of that delta is one hand-made test patrol whose client-supplied 1 850 m sits on a track
that really measures 133 m (its `distance_m` is left alone; `distance_clean_m` records the truth).

## Local API for dashboard development

```powershell
cd D:\freelance\PatrolIQ\backend
$env:DJANGO_DEBUG="true"; $env:DATABASE_URL="sqlite:///db.sqlite3"; $env:DATABASE_URL_DIRECT=""
$env:CORS_ALLOWED_ORIGIN_REGEX='^http://(localhost|127\.0\.0\.1):\d+$'
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py seed_demo          # prints the TOTP secrets for Grace / Tafadzwa
.venv\Scripts\python manage.py runserver 0.0.0.0:8000
```

The dashboard then calls `http://localhost:8000/api/v1/` from any `localhost` port (Vite, Next.js …).
Sign in as `grace.mutasa@grtts.co.zw` / `manager123` + TOTP.

## API overview (`/api/v1/`)

Full contract: spec §5. Auth header `Authorization: Token <key>`. JSON snake_case, datetimes
`YYYY-MM-DDTHH:MM:SSZ` (UTC), geometries GeoJSON lon/lat.

* **Auth** — `auth/login/`, `auth/logout/`, `me/` (+ additive `auth/password/`)
* **Ranger sync** — `sync/bootstrap/?since=`, `sync/push/`, `media/` (+ `media/{id}/`, `media/{id}/file/`),
  `positions/`, `safety/alerts/`, `safety/alerts/{client_uuid}/cancel/`
* **Admin/manager** — `areas/` (+ `boundary/import/`, `boundary/`, `grid/generate/`, `activate/`, `cells/`,
  `sectors/`, additive `layers/roads|water/`), `apu-bases/`, `teams/`, `assignments/`, `users/`,
  `alerts/` (+ `acknowledge/`, additive `resolve/`), `observations/`, `patrols/`, `positions/latest/`,
  `audit-log/`, `species/` (incl. `taxon_group`)
* **Manager dashboard (spec §7)** — `dashboard/summary/`, `rangers/` (+ `{id}/`, `{id}/message/`),
  `patrols/{client_uuid}/track/` (+ `?clean=true|false`), `positions/history/`, `areas/{id}/risk/` (+ `trend/`),
  `areas/{id}/heatmap/` (v1.5 kernel density surface),
  `areas/{id}/coverage/` (+ `export/`), `reports/` (+ `{id}/`, `{id}/download/`, `{id}/share/`,
  `shared/{token}/`), `alerts/{id}/`, `alerts/{id}/dispatch/`; `areas/` setup counters,
  `grid/generate/` `dry_run`, `areas/{id}/cells/` as a GeoJSON FeatureCollection
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

## Notifications

> **Render free instances block outbound SMTP on ports 25, 465 and 587** (the error is `[Errno 101] Network is unreachable` or a timeout). Use a provider that also listens on **port 2525** — e.g. Brevo (`smtp-relay.brevo.com`, port 2525, free tier), Mailgun or SendGrid — or move to a paid instance. SMTP connects over IPv4 by default (`EMAIL_FORCE_IPV4=true`) because Render has no IPv6 egress.

Managers and org admins (active, same organisation) are alerted by **SMS** (Twilio), **email** (SMTP)
and a push stub (FCM). Everything is sent after the request's transaction commits, on a background
thread pool (`NOTIFY_ASYNC`), so a slow or failing provider never delays or fails a ranger's SOS or
sync. Every attempt, success or failure, is a `NotificationLog` row (ops admin → Notifications).
The dashboard's Settings → Notifications page reads `GET /api/v1/notify/status/` (what is configured,
which managers lack a usable phone/email, recent failures) and can send a test to yourself with
`POST /api/v1/notify/test/ {"channel": "sms" | "email"}` (5 per hour per user).

What is sent:

* **Alert SMS + email** for SOS panic, dead man's switch, HWC raised, first HWC details, SOS cancelled
  and high/critical threat or carcass observations. The email adds employee ID, time in the area's
  timezone, coordinates + Google Maps link, area, GRTS cell, HWC details, battery and signal.
* **Sync summary email** after a `sync/push/` or `safety/alerts/` call that created something
  reportable: a patrol started or ended (brief: type, team, base, times, duration, distance, notes,
  debrief audio yes/no), new observations, new safety alerts or first HWC details. Track points,
  position pings and re-uploads are never reported, so routine syncs of an ongoing patrol send nothing.
  Subject `PATROLIQ sync · <ranger> · <n> new record(s)`, prefixed `⚠` for SOS/HWC/critical items.
* Emails are plain text + simple HTML with no external images, and never contain personal-record
  fields (national ID, date of birth, address, next of kin).

### Email (SMTP)

Set `EMAIL_HOST` (email is "not configured" without it), `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` and
`DEFAULT_FROM_EMAIL`. Port 587 + `EMAIL_USE_TLS=true` works for all of these; for port 465 set
`EMAIL_USE_SSL=true` (and TLS is then ignored).

| Provider | `EMAIL_HOST` | `EMAIL_HOST_USER` | `EMAIL_HOST_PASSWORD` | Notes |
|---|---|---|---|---|
| Gmail / Google Workspace | `smtp.gmail.com` | the full address | a 16-character **app password** (Google account → Security → 2-Step Verification → App passwords) | the normal password is refused; `DEFAULT_FROM_EMAIL` must be that address or a verified alias; ~500 messages/day |
| Outlook / Microsoft 365 | `smtp.office365.com` | the full address | the mailbox password (or app password with MFA) | the tenant must allow *Authenticated SMTP* for the mailbox; personal outlook.com accounts may have SMTP basic auth disabled |
| Brevo (ex-Sendinblue) | `smtp-relay.brevo.com` | the SMTP login shown under SMTP & API | the SMTP key (not the API key) | verify the sender/domain in Brevo first; free plan 300 emails/day |

Example (`.env` or Render):

```
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=true
EMAIL_HOST_USER=alerts@example.org
EMAIL_HOST_PASSWORD=<app password>
DEFAULT_FROM_EMAIL=PATROLIQ Alerts <alerts@example.org>
```

### SMS (Twilio)

`NOTIFY_BACKEND=twilio_fcm`, `TWILIO_ACCOUNT_SID` (AC…), auth by API key (`TWILIO_API_KEY_SID` SK… +
`TWILIO_API_KEY_SECRET`, preferred: revocable without rotating the account's auth token) or
`TWILIO_AUTH_TOKEN`, and a sender: `TWILIO_MESSAGING_SERVICE_SID` (MG…) or `TWILIO_FROM_NUMBER`.
Failures are logged with Twilio's own error code and message (e.g. `Twilio 21608: …`), never with a
credential.

Phone numbers are stored in E.164. The users API converts `0771234567`, `077 123 4567`,
`263771234567` and `+263 77 123 4567` to `+263771234567` (`SMS_DEFAULT_COUNTRY_CODE`, default 263) and
rejects anything else with `400 validation_error` on `phone`; older numbers are normalised at send time
and logged as `invalid phone number` if they can't be. If no manager-level user has a valid phone, one
`no manager has a phone number` row is logged per alert (email still goes out). Add one in Users.

**Twilio trial accounts** (the usual reason SMS "doesn't arrive"):

* a trial can only send to **Verified Caller IDs** (Console → Phone Numbers → Manage → Verified Caller
  IDs); anything else fails with `21608`. Upgrade the account to send to any number;
* enable **Zimbabwe** under Messaging → Settings → **Geo permissions**, or sends fail with `21408`;
* the sender must be an **SMS-capable number owned by the account** (or a Messaging Service containing
  one); a number without SMS capability, or one you do not own, fails with `21606`/`21659`. Trial
  messages are prefixed "Sent from your Twilio trial account".

Use Settings → Notifications → "Send test SMS" to check the setup; the error shown is Twilio's.

## Deploy on Render (+ Neon, Cloudflare R2, Vercel dashboard)

`render.yaml` (Blueprint), `Procfile` and `.python-version` (3.11) are included; `gunicorn` is pinned in
`requirements.txt`.

1. **Neon**: copy the pooled and the direct connection strings of the database.
2. **Cloudflare R2** (recommended; Render's filesystem is ephemeral): create a private bucket (no public
   access, no custom domain) and an R2 API token with *Object Read & Write* on that bucket. Note the
   account id, access key id and secret.
3. **Render** → New → Blueprint → select `SlyJeremiah/patroliq-backend` (branch `main`). Render reads
   `render.yaml` and prompts for every `sync: false` variable (table below). `DJANGO_SECRET_KEY` is generated.
4. The Blueprint configures: build `pip install -r requirements.txt && python manage.py collectstatic --noinput`;
   pre-deploy `python manage.py migrate --noinput` (over `DATABASE_URL_DIRECT`); start
   `gunicorn patroliq.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 120 --access-logfile -`;
   health check `/healthz/` (answered before host validation and the HTTPS redirect); region `ohio`
   (= Neon `us-east-2`). Plans without pre-deploy commands: append `&& python manage.py migrate --noinput`
   to the build command.
5. After the first deploy open `https://<service>.onrender.com/healthz/` → `{"status": "ok", "database": true}`.
6. **Vercel dashboard**: set its API base URL to `https://<service>.onrender.com/api/v1/` and put the
   dashboard origin in `CORS_ALLOWED_ORIGINS` (and the preview pattern in `CORS_ALLOWED_ORIGIN_REGEX`).
   Changing env vars on Render redeploys the service.
7. Optional: `score_risk` as a daily Render cron job (`python manage.py score_risk`, same env vars);
   create the first platform admin with `python manage.py createsuperuser` from the Render shell.
   Do not run `seed_demo` on a production database (it creates accounts with known passwords).

| Variable | Value on Render | Secret (`sync: false`) |
|---|---|---|
| `DATABASE_URL` | Neon **pooled** URL (`…-pooler…`, `sslmode=require`) | yes |
| `DATABASE_URL_DIRECT` | Neon **direct** URL (migrations) | yes |
| `DJANGO_SECRET_KEY` | generated by Render (`SECRET_KEY` is accepted as a fallback name) | generated |
| `DJANGO_DEBUG` | `false` | |
| `DJANGO_ALLOWED_HOSTS` | `.onrender.com` (+ your custom domain); `RENDER_EXTERNAL_HOSTNAME` is added automatically | |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://*.onrender.com` (ops admin at `/ops-admin/`) | |
| `DJANGO_SECURE_SSL_REDIRECT` / `DJANGO_HSTS_SECONDS` | `true` / `31536000` (`SECURE_PROXY_SSL_HEADER` trusts `X-Forwarded-Proto`) | |
| `TRUSTED_PROXY_COUNT` | `1` | |
| `CORS_ALLOWED_ORIGINS` | `https://patroliq-dashboard.vercel.app` | yes |
| `CORS_ALLOWED_ORIGIN_REGEX` | `^https://patroliq-dashboard-[a-z0-9-]+\.vercel\.app$` | yes |
| `DASHBOARD_URL` | `https://patroliq-dashboard.vercel.app` (share links) | yes |
| `WEB_IP_ALLOWLIST` | empty, or office/VPN CIDRs | yes |
| `R2_BUCKET`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | R2 bucket + token | yes |
| `NOTIFY_BACKEND` | `console` until Twilio/FCM are configured, then `twilio_fcm` | |
| `NOTIFY_ASYNC` / `SMS_DEFAULT_COUNTRY_CODE` | `true` / `263` | |
| `TWILIO_ACCOUNT_SID` | Twilio `AC…` | yes |
| `TWILIO_API_KEY_SID` + `TWILIO_API_KEY_SECRET` (or `TWILIO_AUTH_TOKEN`) | Twilio API key `SK…` + secret | yes |
| `TWILIO_MESSAGING_SERVICE_SID` (or `TWILIO_FROM_NUMBER`) | `MG…` / `+1…` SMS-capable number | yes |
| `FCM_PROJECT_ID`, `FCM_ACCESS_TOKEN` | Firebase | yes |
| `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | SMTP (see Notifications) | yes |
| `EMAIL_PORT` / `EMAIL_USE_TLS` / `EMAIL_USE_SSL` / `EMAIL_TIMEOUT` | `587` / `true` / `false` / `15` | |
| `EMAIL_ALERTS` / `EMAIL_SYNC_SUMMARIES` | `true` / `true` | |

Without R2, uncomment the `disk` block in `render.yaml` and set `MEDIA_ROOT=/var/data/media` (single
instance only). Reports whose stored file is missing (e.g. generated by `seed_demo` run from a laptop)
are regenerated from their saved parameters on download.

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
`ST_Area(geography)`, `ST_Distance`, `ST_Transform` and `ST_HexagonGrid` (GRTS ordering stays in
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
* Web dashboard: CORS only for configured origins (no credentials; exposes `retry-after`,
  `content-disposition`), optional `WEB_IP_ALLOWLIST` for web roles (rangers, sync, safety and `/healthz/`
  are never restricted). Reports are stored under `reports/<org>/` in the default storage and only
  streamed through authenticated, tenant-scoped views; researcher/viewer reports are always anonymised.
* Not included yet: encrypting TOTP secrets at rest (use a KMS-backed field when deploying).
