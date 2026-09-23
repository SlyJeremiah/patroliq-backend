"""Patrol track sanitising (geo/track.py): the filter, the track endpoint and ``manage.py clean_tracks``."""
from __future__ import annotations

import io
import math
from datetime import datetime, timedelta, timezone as dt_timezone

import pytest
from django.core.management import call_command

import geo
from areas.models import GrtsCell, Team
from field.models import Patrol, TrackPoint

from .conftest import client_for, make_area, make_org, make_user, new_uuid

pytestmark = pytest.mark.django_db  # the filter tests below need no database, the API ones do

T0 = datetime(2026, 9, 15, 6, 0, tzinfo=dt_timezone.utc)
M_PER_DEG_LAT = 110_574.0


def fix(i, north_m=0.0, east_m=0.0, *, accuracy_m=5.0, speed_mps=None, step_s=10, lat0=-17.5, lon0=30.95):
    """A fix ``i`` steps after T0, offset from (lat0, lon0) by whole metres."""
    lat = lat0 + north_m / M_PER_DEG_LAT
    lon = lon0 + east_m / (111_320.0 * math.cos(math.radians(lat0)))
    return (lat, lon, accuracy_m, speed_mps, T0 + timedelta(seconds=i * step_s))


def straight_walk(n=60, step_m=15.0, **kw):
    """``n`` fixes marching due north at ``step_m`` every 10 s (1.5 m/s — a walking pace)."""
    return [fix(i, north_m=i * step_m, **kw) for i in range(n)]


# --- the filter ----------------------------------------------------------------------------------

def test_clean_walk_is_left_alone():
    points = straight_walk(n=60, step_m=15.0)
    track = geo.clean_track(points, patrol_type="foot")
    expected = 59 * 15.0
    assert track.kept == 60 and track.points_dropped == 0
    assert track.dropped == {"invalid": 0, "accuracy": 0, "time": 0, "jump": 0, "drift": 0}
    assert abs(track.distance_m - expected) / expected < 0.02  # within a few %
    # the served geometry and the served distance always agree
    assert abs(track.distance_m - geo.path_length_m([(p.lat, p.lon) for p in track.points])) < 0.01


def test_injected_300m_outliers_are_dropped():
    clean = straight_walk(n=60, step_m=15.0)
    dirty = list(clean)
    for i in (7, 23, 41):  # a network fix 300 m east, then back on the path
        lat, lon, acc, speed, at = dirty[i]
        dirty[i] = fix(i, north_m=i * 15.0, east_m=300.0, accuracy_m=acc)
    baseline = geo.clean_track(clean, patrol_type="foot")
    track = geo.clean_track(dirty, patrol_type="foot")
    assert track.dropped["jump"] == 3 and track.kept == 57
    assert abs(track.distance_m - baseline.distance_m) / baseline.distance_m < 0.06
    # without the filter the same track measures far more than the real walk
    raw = geo.path_length_m([(p[0], p[1]) for p in dirty])
    assert raw > baseline.distance_m * 1.9


def test_stationary_noise_measures_about_zero():
    """A phone lying still on a rock: every fix jitters inside the accuracy radius."""
    noise = [(-3.0, 4.0), (2.0, -5.0), (5.0, 1.0), (-4.0, -2.0), (1.0, 6.0), (-6.0, 0.0)] * 5
    points = [fix(i, north_m=n, east_m=e, accuracy_m=10.0) for i, (n, e) in enumerate(noise)]
    track = geo.clean_track(points, patrol_type="foot")
    assert track.distance_m == 0.0
    assert track.kept == 1 and track.dropped["drift"] == len(points) - 1


def test_drift_radius_grows_with_accuracy_and_is_anchor_relative():
    # 12 m steps: above the 8 m floor with a good fix, below the 2 x 20 m radius with a poor one
    good = geo.clean_track([fix(i, north_m=i * 12.0, accuracy_m=3.0) for i in range(5)], patrol_type="foot")
    poor = geo.clean_track([fix(i, north_m=i * 12.0, accuracy_m=20.0) for i in range(5)], patrol_type="foot")
    assert good.kept == 5 and good.distance_m > 45
    # drift is measured from the last *kept* fix, never from the previous one, so jitter cannot
    # accumulate — but real movement still registers once it leaves the radius (here at 48 m).
    assert poor.kept == 2 and poor.dropped["drift"] == 3
    assert poor.distance_m == pytest.approx(48.0, rel=0.02)


def test_accuracy_gate_and_unknown_accuracy(settings):
    points = straight_walk(n=10, step_m=15.0)
    points[3] = fix(3, north_m=45.0, accuracy_m=120.0)  # far worse than the 35 m gate
    points[6] = fix(6, north_m=90.0, accuracy_m=None)  # unknown: kept, still jump/drift checked
    track = geo.clean_track(points, patrol_type="foot")
    assert track.dropped["accuracy"] == 1 and track.kept == 9
    assert any(p.accuracy_m is None for p in track.points)
    # the gate is configurable
    settings.TRACK_MAX_ACCURACY_M = 200
    assert geo.clean_track(points, patrol_type="foot").dropped["accuracy"] == 0
    assert geo.clean_track(points, patrol_type="foot", max_accuracy_m=10).dropped["accuracy"] == 1


def test_duplicate_and_out_of_order_timestamps_are_dropped():
    points = straight_walk(n=6, step_m=15.0)
    duplicate = points[2]
    earlier = fix(1, north_m=900.0)  # both out of order and far away
    track = geo.clean_track(points[:3] + [duplicate, earlier] + points[3:], patrol_type="foot")
    assert track.dropped["time"] == 2 and track.kept == 6


def test_invalid_points_are_dropped():
    points = straight_walk(n=4, step_m=15.0)
    bad = [(None, 30.95, 5.0, None, T0), (95.0, 30.95, 5.0, None, T0), (-17.5, 30.95, 5.0, None, None)]
    track = geo.clean_track(points + bad, patrol_type="foot")
    assert track.dropped["invalid"] == 3 and track.kept == 4 and track.total == 7


@pytest.mark.parametrize("patrol_type,ceiling", [("foot", 6), ("horseback", 10), ("boat", 20), ("vehicle", 35)])
def test_patrol_type_speed_ceilings(patrol_type, ceiling):
    assert geo.max_speed_for(patrol_type) == ceiling
    # one step just under the ceiling, one just over, both over 10 s
    under = [fix(0), fix(1, north_m=(ceiling - 1) * 10)]
    over = [fix(0), fix(1, north_m=(ceiling + 5) * 10)]
    assert geo.clean_track(under, patrol_type=patrol_type).kept == 2
    assert geo.clean_track(over, patrol_type=patrol_type).dropped["jump"] == 1
    # the same "over" leg is fine for any looser type
    looser = [t for t, v in geo.MAX_SPEED_MPS.items() if v > ceiling + 5]
    assert all(geo.clean_track(over, patrol_type=t).kept == 2 for t in looser)


def test_a_reported_speed_above_the_ceiling_is_a_jump():
    points = [fix(0), fix(1, north_m=15.0, speed_mps=80.0), fix(2, north_m=30.0)]
    assert geo.clean_track(points, patrol_type="foot").dropped["jump"] == 1


def test_a_bad_first_fix_does_not_reject_the_whole_track():
    points = [fix(0, north_m=5000.0)] + straight_walk(n=20, step_m=15.0)[1:]
    track = geo.clean_track(points, patrol_type="foot")
    # the first four legs are unbelievable, then the sanitiser re-anchors on the real track
    assert track.kept >= 15 and track.distance_m > 200


def test_empty_and_single_point_tracks():
    empty = geo.clean_track([], patrol_type="foot")
    assert empty.total == 0 and empty.kept == 0 and empty.distance_m == 0.0 and empty.coordinates == []
    one = geo.clean_track([fix(0)], patrol_type="foot")
    assert one.kept == 1 and one.distance_m == 0.0


# --- endpoint and command ------------------------------------------------------------------------

@pytest.fixture
def tracked():
    """An org with one ended foot patrol whose stored track holds three 300 m outliers."""
    org = make_org("TRK")
    area = make_area(org)
    team = Team.objects.create(organisation=org, area=area, apu_base=area.apu_bases.first(), name="Track Team")
    ranger = make_user(org, "ranger", team=team)
    manager = make_user(org, "manager")
    cell = GrtsCell.objects.filter(area=area).order_by("grts_order").first()
    lon0, lat0 = cell.centroid["coordinates"]
    rows = [fix(i, north_m=i * 15.0, lat0=lat0, lon0=lon0) for i in range(40)]
    for i in (5, 17, 31):
        rows[i] = fix(i, north_m=i * 15.0, east_m=300.0, lat0=lat0, lon0=lon0)
    patrol = Patrol.objects.create(
        client_uuid=new_uuid(), organisation=org, ranger=ranger, team=team, area=area, patrol_type="foot",
        started_at=T0, ended_at=T0 + timedelta(minutes=10), status="ended",
        distance_m=round(geo.path_length_m([(r[0], r[1]) for r in rows]), 1), distance_from_client=True)
    TrackPoint.objects.bulk_create([
        TrackPoint(organisation=org, patrol=patrol, lat=lat, lon=lon, accuracy_m=acc, speed_mps=spd, recorded_at=at)
        for lat, lon, acc, spd, at in rows])
    return {"org": org, "area": area, "patrol": patrol, "ranger": ranger, "mgr": client_for(manager)}


def test_track_endpoint_defaults_to_clean(tracked):
    patrol = tracked["patrol"]
    feat = tracked["mgr"].get(f"/api/v1/patrols/{patrol.pk}/track/").json()
    props = feat["properties"]
    assert feat["type"] == "Feature" and feat["geometry"]["type"] == "LineString"
    assert props["points_dropped"] == 3 and props["clean"] is True
    assert len(feat["geometry"]["coordinates"]) == 37 == props["point_count"] == len(props["times"])
    assert props["distance_m"] == int(round(patrol.distance_m))  # as stored, untouched
    assert 0 < props["distance_clean_m"] < props["distance_m"] * 0.6
    # the response shape the dashboard reads is otherwise unchanged
    assert {"client_uuid", "ranger_id", "ranger_name", "started_at", "ended_at", "distance_m", "duration_s",
            "status", "patrol_type", "area_id", "point_count", "times"} <= set(props)


def test_track_endpoint_clean_false_returns_every_point(tracked):
    feat = tracked["mgr"].get(f"/api/v1/patrols/{tracked['patrol'].pk}/track/", {"clean": "false"}).json()
    props = feat["properties"]
    assert len(feat["geometry"]["coordinates"]) == 40 == props["point_count"] and props["clean"] is False
    # both numbers are reported either way, so a client can compare them
    assert props["points_dropped"] == 3 and props["distance_clean_m"] < props["distance_m"]
    assert tracked["mgr"].get(f"/api/v1/patrols/{tracked['patrol'].pk}/track/",
                              {"clean": "maybe"}).json()["error"]["code"] == "validation_error"


def test_push_stores_a_sanitised_distance_beside_a_client_one(tracked):
    """A client-supplied distance is kept; the sanitised one is stored next to it."""
    from field.services import recompute_patrol_metrics

    patrol = tracked["patrol"]
    stored = patrol.distance_m
    recompute_patrol_metrics(patrol)
    patrol.refresh_from_db()
    assert patrol.distance_m == stored and patrol.distance_from_client
    assert patrol.distance_clean_m is not None and patrol.distance_clean_m < stored * 0.6
    assert patrol.effective_distance_m == patrol.distance_clean_m

    # a patrol with no track points at all stays NULL: nothing to measure is not "walked nowhere"
    bare = Patrol.objects.create(client_uuid=new_uuid(), organisation=tracked["org"], ranger=tracked["ranger"],
                                 area=tracked["area"], started_at=T0, status="active", distance_m=500,
                                 distance_from_client=True)
    recompute_patrol_metrics(bare)
    bare.refresh_from_db()
    assert bare.distance_clean_m is None and bare.effective_distance_m == 500


def test_clean_tracks_command(tracked):
    patrol = tracked["patrol"]
    stored = patrol.distance_m

    out = io.StringIO()
    call_command("clean_tracks", "--org", "TRK", "--dry-run", stdout=out)
    report = out.getvalue()
    assert "Dry run" in report and "1 patrols examined, 1 would change" in report
    assert "3 points dropped" in report
    patrol.refresh_from_db()
    assert patrol.distance_clean_m is None  # --dry-run writes nothing

    out = io.StringIO()
    call_command("clean_tracks", "--org", "TRK", stdout=out)
    assert "1 patrols examined, 1 changed" in out.getvalue()
    patrol.refresh_from_db()
    assert patrol.distance_m == stored  # client-supplied: never overwritten
    assert 0 < patrol.distance_clean_m < stored * 0.6

    # idempotent, and the filters narrow the run
    out = io.StringIO()
    call_command("clean_tracks", "--org", "TRK", stdout=out)
    assert "1 patrols examined, 0 changed" in out.getvalue()
    out = io.StringIO()
    call_command("clean_tracks", "--org", "TRK", "--since", "2026-09-16", stdout=out)
    assert "0 patrols examined" in out.getvalue()
    out = io.StringIO()
    call_command("clean_tracks", "--area", str(tracked["area"].pk), stdout=out)
    assert "1 patrols examined" in out.getvalue()
    with pytest.raises(Exception):
        call_command("clean_tracks", "--since", "last-tuesday", stdout=io.StringIO())


def test_clean_tracks_corrects_a_server_owned_distance(tracked):
    """Without ``distance_from_client`` the command fixes ``distance_m`` in place too."""
    patrol = tracked["patrol"]
    Patrol.objects.filter(pk=patrol.pk).update(distance_from_client=False)
    call_command("clean_tracks", "--org", "TRK", stdout=io.StringIO())
    patrol.refresh_from_db()
    assert patrol.distance_m == patrol.distance_clean_m > 0
