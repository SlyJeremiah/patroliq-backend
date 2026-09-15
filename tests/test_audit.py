import pytest

from audit.models import AuditLog, AuditLogImmutable
from audit.utils import audit

from .conftest import client_for, make_org, make_user

pytestmark = pytest.mark.django_db


def test_audit_log_is_append_only():
    org = make_org()
    entry = audit(None, "test.event", actor=make_user(org, "manager"), organisation_id=org.pk, detail={"a": 1})
    entry.action = "tampered"
    with pytest.raises(AuditLogImmutable):
        entry.save()
    with pytest.raises(AuditLogImmutable):
        entry.delete()
    with pytest.raises(AuditLogImmutable):
        AuditLog.objects.filter(pk=entry.pk).update(action="tampered")
    with pytest.raises(AuditLogImmutable):
        AuditLog.objects.all().delete()
    assert AuditLog.objects.get(pk=entry.pk).action == "test.event"


def test_audit_endpoint_is_read_only_and_scoped():
    a, b = make_org(), make_org()
    manager_a = make_user(a, "manager")
    audit(None, "a.event", actor=manager_a, organisation_id=a.pk)
    audit(None, "b.event", actor=make_user(b, "manager"), organisation_id=b.pk)
    c = client_for(manager_a)
    actions = [e["action"] for e in c.get("/api/v1/audit-log/").json()]
    assert actions == ["a.event"]
    assert c.post("/api/v1/audit-log/", {"action": "x"}, format="json").status_code == 405
    assert c.delete("/api/v1/audit-log/").status_code == 405


def test_admin_actions_are_audited():
    org = make_org()
    admin = make_user(org, "org_admin")
    client_for(admin).post("/api/v1/areas/", {"name": "Audited", "area_type": "other"}, format="json")
    entry = AuditLog.objects.get(action="area.create")
    assert entry.organisation_id == org.pk and entry.actor_id == admin.pk and entry.ip == "127.0.0.1"
