-- =====================================================================================================
-- PATROLIQ — PostgreSQL row-level security (Platform Spec v1.2 §1, isolation rule 2; PRD 6.3 layer 1)
--
-- Run AFTER `python manage.py migrate`, as the database owner (the role that owns the tables):
--     psql "$DATABASE_OWNER_URL" -v app_role=patroliq_app -f sql/postgres_rls.sql
--
-- Model:
--   * patroliq_owner  — owns the schema; runs migrations and management commands (seed_demo,
--                       score_risk). Table owners bypass RLS unless FORCE is set; we do NOT force it,
--                       so maintenance jobs can work across tenants.
--   * patroliq_app    — the role in the web process's DATABASE_URL. Subject to every policy below.
--                       It must NOT own the tables and must NOT have BYPASSRLS. (On Neon the default
--                       owner role created with the project has BYPASSRLS, so it ignores RLS even with FORCE ROW LEVEL
--                       SECURITY — the app has to connect as a separate role.)
--   * Each request: core.middleware.PostgresTenantMiddleware opens one transaction for the request and runs
--         SELECT set_config('app.org_id', '<organisation uuid>', true)   -- transaction-local
--     so it is safe behind a transaction-mode pooler (Neon/PgBouncer) and vanishes at COMMIT/ROLLBACK.
--     Without a tenant id no tenant row is visible.
--
-- Auth tables (accounts_organisation, accounts_licence, accounts_user, accounts_authtoken,
-- accounts_loginlockout) are intentionally NOT under RLS: sign-in must resolve an organisation code /
-- email before a tenant is known. They hold no operational field data and remain scoped in the API.
-- Species (field_species) is global reference data.
-- Idempotent: safe to re-run after new migrations.
-- =====================================================================================================

\set ON_ERROR_STOP on
\if :{?app_role}
\else
  \set app_role patroliq_app
\endif

-- psql variables are not expanded inside dollar-quoted DO blocks, so pass the role name via a setting.
SELECT set_config('patroliq.app_role', :'app_role', false);

DO $$
DECLARE r text := current_setting('patroliq.app_role');
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    EXECUTE format('CREATE ROLE %I LOGIN', r);
    RAISE NOTICE 'Created role %; set its password with ALTER ROLE ... PASSWORD', r;
  END IF;
END $$;

-- Helper: current tenant id or NULL (never raises when the setting is missing/empty).
CREATE OR REPLACE FUNCTION app_current_org() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.org_id', true), '')::uuid
$$;

-- -----------------------------------------------------------------------------------------------------
-- Tenant tables: every row carries organisation_id.
-- -----------------------------------------------------------------------------------------------------
DO $$
DECLARE
  t text;
  tenant_tables text[] := ARRAY[
    'areas_area', 'areas_featurelayer', 'areas_apubase', 'areas_sector', 'areas_grtscell', 'areas_team',
    'areas_assignment', 'areas_riskscore',
    'field_patrol', 'field_trackpoint', 'field_observation', 'field_media', 'field_safetyalert',
    'field_positionping', 'field_alertevent',
    'dashboard_report', 'dashboard_reportshare',
    'core_tombstone', 'audit_auditlog', 'notify_notificationlog'
  ];
BEGIN
  FOREACH t IN ARRAY tenant_tables LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I
         USING (organisation_id = app_current_org())
         WITH CHECK (organisation_id = app_current_org())', t);
  END LOOP;
END $$;

-- Many-to-many join tables have no organisation_id: visible only when the parent row is.
ALTER TABLE areas_assignment_cells ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON areas_assignment_cells;
CREATE POLICY tenant_isolation ON areas_assignment_cells
  USING (EXISTS (SELECT 1 FROM areas_assignment a WHERE a.id = assignment_id AND a.organisation_id = app_current_org()))
  WITH CHECK (EXISTS (SELECT 1 FROM areas_assignment a WHERE a.id = assignment_id AND a.organisation_id = app_current_org()));

ALTER TABLE accounts_user_areas ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON accounts_user_areas;
CREATE POLICY tenant_isolation ON accounts_user_areas
  USING (EXISTS (SELECT 1 FROM areas_area a WHERE a.id = area_id AND a.organisation_id = app_current_org()))
  WITH CHECK (EXISTS (SELECT 1 FROM areas_area a WHERE a.id = area_id AND a.organisation_id = app_current_org()));

-- Audit and notification rows written before sign-in (e.g. failed logins for unknown accounts) have no
-- tenant: allow INSERT of NULL-organisation rows but never reading them through the app role.
-- (audit.utils.audit() switches app.org_id to the row's organisation for the INSERT itself, e.g. the
-- auth.login entry written before the request has a tenant.)
DROP POLICY IF EXISTS pre_auth_insert ON audit_auditlog;
CREATE POLICY pre_auth_insert ON audit_auditlog FOR INSERT WITH CHECK (organisation_id IS NULL);
DROP POLICY IF EXISTS pre_auth_insert ON notify_notificationlog;
CREATE POLICY pre_auth_insert ON notify_notificationlog FOR INSERT WITH CHECK (organisation_id IS NULL);

-- -----------------------------------------------------------------------------------------------------
-- Audit log: append-only for everyone (PRD 7.3) — privileges AND a trigger (also stops the owner).
-- -----------------------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_auditlog is append-only (% refused)', TG_OP;
END $$;

DROP TRIGGER IF EXISTS audit_log_no_update ON audit_auditlog;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON audit_auditlog
  FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_auditlog;
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_auditlog
  FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable();

-- -----------------------------------------------------------------------------------------------------
-- Privileges for the application role.
-- -----------------------------------------------------------------------------------------------------
DO $$
DECLARE r text := current_setting('patroliq.app_role');
BEGIN
  EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', r);
  EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I', r);
  EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I', r);
  EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON audit_auditlog FROM %I', r);
  EXECUTE format('REVOKE TRUNCATE ON ALL TABLES IN SCHEMA public FROM %I', r);
END $$;

-- Tables created by later migrations are owned by the migrating role; re-run this file after each migration
-- that adds tables (it re-grants and re-applies policies), or additionally:
--   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO patroliq_app;
--   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO patroliq_app;

-- Verification (run as patroliq_app, e.g. over the direct endpoint):
--   BEGIN;
--   SELECT set_config('app.org_id', '<org uuid>', true);
--   SELECT count(*) FROM field_observation;          -- only that organisation's rows
--   SELECT set_config('app.org_id', '', true);
--   SELECT count(*) FROM field_observation;          -- 0
--   COMMIT;
