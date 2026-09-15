import pytest

from areas.models import Area
from field.models import Observation

from .conftest import client_for, make_area, make_org, make_user, new_uuid, utm_square

pytestmark = pytest.mark.django_db


@pytest.fixture
def world():
    a, b = make_org("AAA"), make_org("BBB")
    area_a = make_area(a, name="Area A")
    area_b = make_area(b, name="Area B", boundary=utm_square(32.05, -20.25))
    ranger_b = make_user(b, "ranger")
    obs_b = Observation.objects.create(
        client_uuid=new_uuid(), organisation=b, area=area_b, observer=ranger_b, category="threat", subtype="snare",
        lat=-20.25, lon=32.05, recorded_at="2026-09-01T10:00:00Z")
    return {
        "a": a, "b": b, "area_a": area_a, "area_b": area_b, "obs_b": obs_b,
        "admin_a": make_user(a, "org_admin"), "manager_a": make_user(a, "manager"), "ranger_a": make_user(a, "ranger"),
    }


def test_areas_list_and_detail_are_scoped(world):
    c = client_for(world["admin_a"])
    ids = {x["id"] for x in c.get("/api/v1/areas/").json()}
    assert ids == {str(world["area_a"].pk)}
    r = c.get(f"/api/v1/areas/{world['area_b'].pk}/")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert c.get(f"/api/v1/areas/{world['area_b'].pk}/cells/").status_code == 404


def test_cannot_modify_other_org_area(world):
    c = client_for(world["admin_a"])
    area_b = world["area_b"]
    assert c.patch(f"/api/v1/areas/{area_b.pk}/", {"name": "Hijacked"}, format="json").status_code == 404
    assert c.delete(f"/api/v1/areas/{area_b.pk}/").status_code == 404
    assert c.put(f"/api/v1/areas/{area_b.pk}/boundary/", {"boundary": area_b.boundary}, format="json").status_code == 404
    assert c.post(f"/api/v1/areas/{area_b.pk}/grid/generate/", {"cell_size_m": 500}, format="json").status_code == 404
    area_b.refresh_from_db()
    assert area_b.name == "Area B"


def test_foreign_ids_in_request_bodies_are_rejected(world):
    c = client_for(world["admin_a"])
    r = c.post("/api/v1/apu-bases/", {"area_id": str(world["area_b"].pk), "name": "X", "code": "X-1",
                                      "location": {"type": "Point", "coordinates": [32.05, -20.25]}}, format="json")
    assert r.status_code == 400
    assert "area_id" in r.json()["error"]["fields"]


def test_observations_scoped_by_list_and_id(world):
    c = client_for(world["manager_a"])
    assert c.get("/api/v1/observations/").json() == []
    assert c.get(f"/api/v1/observations/{world['obs_b'].pk}/").status_code == 404
    # A ranger of org A pushing into org B's area is rejected per item.
    ranger = client_for(world["ranger_a"])
    r = ranger.post("/api/v1/sync/push/", {"observations": [{
        "client_uuid": new_uuid(), "area_id": str(world["area_b"].pk), "category": "other", "lat": -20.25,
        "lon": 32.05, "recorded_at": "2026-09-01T10:00:00Z"}]}, format="json")
    assert r.json()["rejected"][0]["code"] == "invalid_area"
    # Re-using org B's client_uuid cannot overwrite or reveal it.
    r = ranger.post("/api/v1/sync/push/", {"observations": [{
        "client_uuid": str(world["obs_b"].pk), "area_id": str(world["area_a"].pk), "category": "other",
        "lat": -17.5, "lon": 30.95, "recorded_at": "2026-09-01T10:00:00Z"}]}, format="json")
    assert r.json()["rejected"][0]["code"] == "client_uuid_conflict"
    assert Observation.objects.get(pk=world["obs_b"].pk).organisation_id == world["b"].pk


def test_bootstrap_never_contains_other_org(world):
    body = client_for(world["ranger_a"]).get("/api/v1/sync/bootstrap/").json()
    assert body["areas"] == []  # ranger A is not assigned to any area yet
    world["ranger_a"].areas.add(world["area_a"])
    body = client_for(world["ranger_a"]).get("/api/v1/sync/bootstrap/").json()
    assert [a["id"] for a in body["areas"]] == [str(world["area_a"].pk)]
    assert {c["area_id"] for c in body["cells"]} == {str(world["area_a"].pk)}


def test_platform_admin_cannot_read_operational_data(world):
    platform = make_user(None, "platform_admin", email="ops@zrgis.example")
    c = client_for(platform)
    for path in ("/api/v1/observations/", "/api/v1/patrols/", "/api/v1/areas/", "/api/v1/positions/latest/",
                 "/api/v1/alerts/", "/api/v1/sync/bootstrap/", f"/api/v1/observations/{world['obs_b'].pk}/"):
        r = c.get(path)
        assert r.status_code == 403, path
    usage = c.get(f"/api/v1/platform/organisations/{world['b'].pk}/usage/").json()
    assert usage["areas"]["total"] == 1
    assert "observations" not in usage


def test_org_users_cannot_use_platform_endpoints(world):
    r = client_for(world["admin_a"]).get("/api/v1/platform/organisations/")
    assert r.status_code == 403


def test_ranger_sees_only_own_observations(world):
    area = world["area_a"]
    other = make_user(world["a"], "ranger")
    Observation.objects.create(client_uuid=new_uuid(), organisation=world["a"], area=area, observer=other,
                               category="other", lat=-17.5, lon=30.95, recorded_at="2026-09-01T10:00:00Z")
    assert client_for(world["ranger_a"]).get("/api/v1/observations/").json() == []
    assert len(client_for(world["manager_a"]).get("/api/v1/observations/").json()) == 1


def test_role_permissions(world):
    manager = client_for(world["manager_a"])
    r = manager.post("/api/v1/areas/", {"name": "New"}, format="json")
    assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied"
    assert manager.put(f"/api/v1/areas/{world['area_a'].pk}/boundary/", {"boundary": world["area_a"].boundary},
                       format="json").status_code == 403
    assert client_for(world["ranger_a"]).get("/api/v1/users/").status_code == 403
    assert client_for(world["ranger_a"]).get("/api/v1/audit-log/").status_code == 403
    assert Area.objects.count() == 2
