"""
Database-layer tenant context for PostgreSQL row-level security (spec §1 isolation rule 2).

``sql/postgres_rls.sql`` installs policies of the form::

    organisation_id = NULLIF(current_setting('app.org_id', true), '')::uuid

The setting is always **transaction-local** (``set_config(..., is_local => true)``). Production runs
behind a transaction-mode connection pooler (Neon's PgBouncer endpoint), where two transactions of
the same Django connection may be served by different PostgreSQL backends, so session-level
``SET`` would leak a tenant id to another client or be missing on the next query. Consequences:

* the value must be set inside the transaction whose queries need it — the request transaction
  opened by :class:`core.middleware.PostgresTenantMiddleware`, or the atomic block opened by
  :func:`tenant_context`;
* it disappears automatically at COMMIT/ROLLBACK, so nothing can leak between requests.

On SQLite (dev/tests) everything here is a no-op, and the application-layer queryset scoping in
:mod:`core.tenancy` is the only layer.
"""
from __future__ import annotations

from contextlib import contextmanager

from django.db import connection, transaction


def is_postgres() -> bool:
    return connection.vendor == "postgresql"


def set_db_org(org_id) -> None:
    """Set ``app.org_id`` for the rest of the current transaction (call inside ``atomic``)."""
    if not is_postgres():
        return
    with connection.cursor() as cur:
        cur.execute("SELECT set_config('app.org_id', %s, true)", [str(org_id) if org_id else ""])


def current_db_org() -> str:
    """The transaction's ``app.org_id`` ('' when unset). Always '' on SQLite."""
    if not is_postgres():
        return ""
    with connection.cursor() as cur:
        cur.execute("SELECT current_setting('app.org_id', true)")
        return cur.fetchone()[0] or ""


@contextmanager
def tenant_context(org_id):
    """
    Run the block as ``org_id`` (management commands, pre-auth audit rows, platform usage counts).

    Opens an atomic block (a savepoint when already inside the request transaction), sets the tenant
    for it and restores the previous value on success. On an exception the savepoint/transaction is
    rolled back, which also reverts the setting.
    """
    if not is_postgres():
        yield
        return
    with transaction.atomic():
        previous = current_db_org()
        set_db_org(org_id)
        yield
        set_db_org(previous or None)
