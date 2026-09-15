"""
PostgreSQL-only: the RLS tenant id is transaction-local, so it works behind a transaction-mode pooler
(Neon / PgBouncer) and can never leak between requests. Run with ``PATROLIQ_TEST_DB=postgres``.
"""
from __future__ import annotations

import pytest
from django.conf import settings
from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory

from core.db import current_db_org, tenant_context
from core.middleware import PostgresTenantMiddleware
from tests.conftest import client_for, make_org, make_user

pytestmark = pytest.mark.skipif("postgresql" not in settings.DATABASES["default"]["ENGINE"],
                                reason="PostgreSQL only")


def _probe():
    seen = {}

    def view(request):
        seen["org"] = current_db_org()
        seen["in_atomic"] = connection.in_atomic_block
        return HttpResponse("ok")

    return seen, PostgresTenantMiddleware(view)


def test_request_transaction_carries_token_tenant(org_a):
    token = client_for(make_user(org_a)).token
    seen, mw = _probe()
    mw(RequestFactory().get("/api/v1/me/", HTTP_AUTHORIZATION=f"Token {token.key}"))
    assert seen == {"org": str(org_a.pk), "in_atomic": True}

    mw(RequestFactory().post("/api/v1/auth/login/"))  # no token: pre-auth, empty tenant
    assert seen["org"] == ""


def test_tenant_context_nests_restores_and_reverts_on_error(org_a, org_b):
    with tenant_context(org_a.pk):
        assert current_db_org() == str(org_a.pk)
        with tenant_context(org_b.pk):
            assert current_db_org() == str(org_b.pk)
        assert current_db_org() == str(org_a.pk)
        with pytest.raises(RuntimeError):
            with tenant_context(org_b.pk):
                raise RuntimeError("boom")
        assert current_db_org() == str(org_a.pk)


@pytest.mark.django_db(transaction=True)
def test_tenant_does_not_survive_the_request():
    org = make_org("PGLEAK")
    token = client_for(make_user(org)).token
    seen, mw = _probe()
    mw(RequestFactory().get("/api/v1/me/", HTTP_AUTHORIZATION=f"Token {token.key}"))
    assert seen["org"] == str(org.pk)
    assert not connection.in_atomic_block
    assert current_db_org() == ""  # a new transaction: nothing left behind for the next client
