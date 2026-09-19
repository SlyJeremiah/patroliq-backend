"""
Upsert the Zimbabwe species catalogue (spec v1.5 §E) so production databases carry it without
running ``seed_demo``.

Rows are keyed on ``uuid5(SPECIES_NAMESPACE, scientific_name.lower())``, the same ids every server
and the bundled Android asset use, so re-running the migration (or a later one regenerated from an
extended :mod:`field.species_data`) updates rows in place instead of duplicating them. Species
rows that are not in the list are left alone — an operator may have added their own.
"""
import uuid

from django.db import migrations

SPECIES_NAMESPACE = uuid.UUID("5b0a4a53-7f4e-4d1a-9d64-5f0c1b7b8e11")


def species_id(scientific_name: str) -> uuid.UUID:
    return uuid.uuid5(SPECIES_NAMESPACE, scientific_name.strip().lower())


def load(apps, schema_editor):
    from django.utils import timezone

    from field.species_data import SPECIES

    Species = apps.get_model("field", "Species")
    now = timezone.now()
    existing = {s.pk: s for s in Species.objects.filter(pk__in=[species_id(row[1]) for row in SPECIES])}
    create, update = [], []
    for common, scientific, shona, ndebele, iucn, taxon in SPECIES:
        pk = species_id(scientific)
        row = existing.get(pk)
        if row is None:
            create.append(Species(id=pk, common_name=common, scientific_name=scientific, shona_name=shona,
                                  ndebele_name=ndebele, iucn_status=iucn, taxon_group=taxon,
                                  created_at=now, updated_at=now))
            continue
        row.common_name, row.scientific_name = common, scientific
        row.shona_name, row.ndebele_name = shona, ndebele
        row.iucn_status, row.taxon_group, row.updated_at = iucn, taxon, now
        update.append(row)
    Species.objects.bulk_create(create, batch_size=200)
    if update:
        Species.objects.bulk_update(
            update, ["common_name", "scientific_name", "shona_name", "ndebele_name", "iucn_status",
                     "taxon_group", "updated_at"], batch_size=200)


def unload(apps, schema_editor):
    """Reverse is a no-op: species may already be referenced by observations."""


class Migration(migrations.Migration):

    dependencies = [("field", "0003_hwc_alerts_and_taxon_group")]

    operations = [migrations.RunPython(load, unload)]
