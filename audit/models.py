"""
Append-only audit log (PRD 7.3: immutable, no role can modify or delete).

Enforced at three levels:
1. ``AuditLog.save()`` refuses to update an existing row and ``delete()`` always raises.
2. The manager's QuerySet refuses bulk ``update()`` / ``delete()``.
3. PostgreSQL: ``sql/postgres_rls.sql`` installs a trigger rejecting UPDATE/DELETE and the app
   role only has INSERT/SELECT on the table.

Actor/organisation are stored as plain UUID columns (no FK), so deleting a user or organisation can
never cascade into, or null out, audit history.
"""
import uuid

from django.db import models


class AuditLogImmutable(Exception):
    pass


class AuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AuditLogImmutable("Audit log entries cannot be updated.")

    def delete(self):
        raise AuditLogImmutable("Audit log entries cannot be deleted.")

    def for_org(self, organisation):
        if organisation is None:
            return self.none()
        return self.filter(organisation_id=getattr(organisation, "pk", organisation))


class AuditLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation_id = models.UUIDField(null=True, blank=True, db_index=True)
    actor_id = models.UUIDField(null=True, blank=True)
    actor_label = models.CharField(max_length=255, blank=True, default="")
    action = models.CharField(max_length=64, db_index=True)
    target_type = models.CharField(max_length=64, blank=True, default="")
    target_id = models.CharField(max_length=64, blank=True, default="")
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    detail = models.JSONField(default=dict, blank=True)

    objects = AuditQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AuditLogImmutable("Audit log entries cannot be updated.")
        kwargs["force_insert"] = True
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditLogImmutable("Audit log entries cannot be deleted.")

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action}"
