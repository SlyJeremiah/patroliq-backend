import pytest

from field.models import Observation

from .conftest import client_for, make_area, make_org, make_user, new_uuid

pytestmark = pytest.mark.django_db


@pytest.fixture
def ctx():
    org = make_org()
    area = make_area(org)
    ranger = make_user(org, "ranger")
    return area, client_for(ranger)


def push(ctx, **fields):
    area, c = ctx
    item = {"client_uuid": new_uuid(), "area_id": str(area.pk), "category": "wildlife", "species_name": "Elephant",
            "lat": -17.5, "lon": 30.95, "recorded_at": "2026-09-15T06:15:00Z", **fields}
    body = c.post("/api/v1/sync/push/", {"observations": [item]}, format="json").json()
    return item["client_uuid"], body


def test_wildlife_sex_defaults_to_unknown(ctx):
    cu, body = push(ctx, count=3)
    assert body["accepted"]["observations"] == [cu]
    assert Observation.objects.get(pk=cu).sex == "unknown"


def test_non_wildlife_sex_not_forced(ctx):
    cu, _ = push(ctx, category="habitat")
    assert Observation.objects.get(pk=cu).sex is None


def test_mixed_counts_within_count_accepted(ctx):
    cu, body = push(ctx, count=7, sex="mixed", male_count=2, female_count=5, age_class="mixed")
    assert body["rejected"] == []
    obs = Observation.objects.get(pk=cu)
    assert (obs.count, obs.male_count, obs.female_count) == (7, 2, 5)


def test_mixed_optional_counts(ctx):
    _, body = push(ctx, count=4, sex="mixed")
    assert body["rejected"] == []


@pytest.mark.parametrize("fields, bad_field", [
    ({"count": 3, "sex": "mixed", "male_count": 4}, "male_count"),
    ({"count": 3, "sex": "mixed", "female_count": 5}, "female_count"),
    ({"count": 5, "sex": "mixed", "male_count": 3, "female_count": 3}, "male_count"),  # sum exceeds count
    ({"count": 5, "sex": "male", "male_count": 5}, "sex"),
    ({"sex": "mixed", "male_count": 1}, "count"),
    ({"count": 2, "sex": "both"}, "sex"),
])
def test_invalid_sex_counts_rejected(ctx, fields, bad_field):
    cu, body = push(ctx, **fields)
    assert body["accepted"]["observations"] == []
    rej = body["rejected"][0]
    assert rej["client_uuid"] == cu and rej["code"] == "validation_error"
    assert rej["message"].startswith(bad_field)
    assert not Observation.objects.filter(pk=cu).exists()


def test_notes_length_checked_after_html_strip(ctx):
    ok_notes = "<p>" + "a" * 1000 + "</p>"
    cu, body = push(ctx, category="other", notes=ok_notes)
    assert body["rejected"] == [] and Observation.objects.get(pk=cu).notes == "a" * 1000
    _, body = push(ctx, category="other", notes="a" * 1001)
    assert body["rejected"][0]["code"] == "validation_error" and body["rejected"][0]["message"].startswith("notes")
