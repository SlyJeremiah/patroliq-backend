"""
Abstract base models shared by every PATROLIQ app.

Tenancy (spec §1, isolation rule 1): every tenant-owned table inherits :class:`TenantModel`
and therefore carries a non-null ``organisation`` FK. Application code must go through
``Model.objects.for_org(org)`` (or the view mixins in :mod:`core.tenancy`), and production
PostgreSQL additionally enforces ``organisation_id = current_setting('app.org_id')`` through
row-level security (``sql/postgres_rls.sql``).

Primary keys are UUID4 (isolation rule 3) so IDs cannot be enumerated across tenants.
"""
import uuid

from django.db import models


class UUIDModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        abstract = True


class TenantQuerySet(models.QuerySet):
    def for_org(self, organisation):
        """Scope to one organisation. ``None`` (e.g. platform admins) yields nothing."""
        if organisation is None:
            return self.none()
        org_id = getattr(organisation, "pk", organisation)
        return self.filter(organisation_id=org_id)


class TenantModel(models.Model):
    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.CASCADE, related_name="+", db_index=True
    )

    objects = TenantQuerySet.as_manager()

    class Meta:
        abstract = True


class Tombstone(TenantModel):
    """
    Records deletions of synced rows so ``sync/bootstrap/?since=`` can tell devices to drop them
    (returned under the additive ``deleted`` key of the bootstrap response).
    """

    id = models.BigAutoField(primary_key=True)
    kind = models.CharField(max_length=32)  # areas | apu_bases | sectors | cells | assignments
    object_id = models.UUIDField()
    area_id = models.UUIDField(null=True, blank=True)
    deleted_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["organisation", "kind", "deleted_at"])]

    @classmethod
    def record(cls, organisation_id, kind, object_ids, area_id=None):
        cls.objects.bulk_create(
            [cls(organisation_id=organisation_id, kind=kind, object_id=oid, area_id=area_id) for oid in object_ids]
        )


class StoredFile(models.Model):
    """Blob row for :class:`core.storage.DatabaseStorage` (``FILE_STORAGE=database``)."""

    name = models.CharField(max_length=255, primary_key=True)
    content = models.BinaryField()
    size = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name
