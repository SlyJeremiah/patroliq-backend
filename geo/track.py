"""
Patrol track sanitising — turn a raw recorded track into a plausible one.

Why
    Up to v1.6 the Android app recorded positions from both the GPS *and* the network
    (cell / Wi-Fi) provider behind a loose 50 m accuracy gate, with no jump or drift filtering.
    A network fix routinely lands hundreds of metres from the ranger, so a stored track holds
    outliers that (a) inflate ``Patrol.distance_m`` and (b) make the line drawn on the dashboard
    zig-zag. The app now filters on the device, but tracks already stored — and any older app
    version still in the field — mean the server has to be robust too.

What ``clean_track`` does
    One pass over a *time-ordered* sequence of fixes, applying in order:

    1. **Sanity** — no timestamp, or a lat/lon that is not a finite WGS84 coordinate: dropped
       (``"invalid"``).
    2. **Accuracy** — reported accuracy worse than ``max_accuracy_m`` (default
       ``settings.TRACK_MAX_ACCURACY_M``, 35 m): dropped (``"accuracy"``). A **null** accuracy
       means *unknown*, not *bad* — the fix is kept and the jump/drift rules still apply to it.
    3. **Time** — a timestamp that is not strictly after the previous in-order fix: dropped
       (``"time"``). This covers duplicates and out-of-order points. The input is never
       re-sorted; callers order by ``recorded_at``.
    4. **Jump** — a fix implying a speed above the ceiling for the patrol type (foot 6,
       horseback 10, boat 20, vehicle 35 m/s), or *reporting* a ``speed_mps`` above it: dropped
       (``"jump"``). Speed is measured against the last **kept** fix, so a lone 300 m outlier is
       discarded and the track carries on from the last good fix. After
       ``MAX_CONSECUTIVE_JUMPS`` rejections in a row the next fix is accepted as a new anchor
       with a zero-length leg — otherwise one bad *first* fix would reject the whole track, and
       a genuine gap (phone off, then resumed elsewhere) would too.
    5. **Drift** — movement shorter than ``max(drift_floor_m, 2 × accuracy)`` from the last kept
       fix is jitter around a stationary ranger, not walking: dropped (``"drift"``), and the
       anchor stays put so jitter can never accumulate. ``accuracy`` is the worse of the two
       fixes' accuracies (an unknown accuracy counts as 0, leaving the plain floor).

    Surviving legs are summed, ignoring any leg shorter than ``min_leg_m``. With the default
    drift floor of 8 m no surviving leg can be shorter than 3 m anyway; the floor only bites if
    a caller lowers ``drift_floor_m``, and keeps the total honest when it does.

Result
    ``CleanTrack(points, distance_m, dropped, total)``. With the default thresholds
    ``distance_m == geo.path_length_m((p.lat, p.lon) for p in points)``, so the served geometry
    and the served distance always agree.

Deliberately not done here
    Nothing is deleted: the raw ``field_trackpoint`` rows stay exactly as the device sent them,
    so a better filter can be applied later. This module touches no database and no Django model
    (settings are read lazily, and only for the default accuracy gate), which is what makes it
    cheap to unit-test.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, NamedTuple

from .core import haversine_m

#: Fallback accuracy gate in metres when ``settings.TRACK_MAX_ACCURACY_M`` is unset.
MAX_ACCURACY_M = 35.0
#: Movement below this many metres is stationary drift even with a perfect fix.
DRIFT_FLOOR_M = 8.0
#: The drift radius grows with the fix accuracy: ``max(DRIFT_FLOOR_M, FACTOR × accuracy_m)``.
DRIFT_ACCURACY_FACTOR = 2.0
#: Legs shorter than this never contribute distance (see the module docstring).
MIN_LEG_M = 3.0
#: Consecutive jump rejections tolerated before the next fix becomes a new anchor.
MAX_CONSECUTIVE_JUMPS = 3

#: Plausible ground speed ceiling per ``Patrol.patrol_type`` (metres per second).
MAX_SPEED_MPS = {"foot": 6.0, "horseback": 10.0, "boat": 20.0, "vehicle": 35.0}
#: Ceiling for an unknown patrol type: the most permissive one, so nothing is dropped blindly.
DEFAULT_MAX_SPEED_MPS = MAX_SPEED_MPS["vehicle"]

#: Every reason a fix can be dropped for, in the order the rules are applied.
DROP_REASONS = ("invalid", "accuracy", "time", "jump", "drift")


class Fix(NamedTuple):
    """One recorded position. ``accuracy_m`` / ``speed_mps`` may be ``None`` (unknown)."""

    lat: float
    lon: float
    accuracy_m: float | None
    speed_mps: float | None
    recorded_at: datetime


@dataclass(frozen=True)
class CleanTrack:
    """Outcome of :func:`clean_track`."""

    points: list[Fix]
    distance_m: float
    dropped: dict[str, int]
    total: int

    @property
    def kept(self) -> int:
        return len(self.points)

    @property
    def points_dropped(self) -> int:
        return self.total - len(self.points)

    @property
    def coordinates(self) -> list[list[float]]:
        """GeoJSON ``[lon, lat]`` pairs, rounded to 7 dp (≈ 1 cm) like the rest of the API."""
        return [[round(p.lon, 7), round(p.lat, 7)] for p in self.points]


def max_speed_for(patrol_type: str | None) -> float:
    """Speed ceiling in m/s for a ``Patrol.patrol_type`` (unknown types get the loosest one)."""
    return MAX_SPEED_MPS.get((patrol_type or "").strip().lower(), DEFAULT_MAX_SPEED_MPS)


def accuracy_limit(value: float | None = None) -> float:
    """``value`` if given, else ``settings.TRACK_MAX_ACCURACY_M``, else ``MAX_ACCURACY_M``."""
    if value is not None:
        return float(value)
    try:
        from django.conf import settings

        configured = getattr(settings, "TRACK_MAX_ACCURACY_M", None)
    except Exception:  # noqa: BLE001 - geo must stay importable without a configured Django
        configured = None
    return float(configured) if configured else MAX_ACCURACY_M


def _number(value: Any) -> float | None:
    """A finite float, or ``None`` for ``None`` / non-numeric / NaN / infinity."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _as_fix(raw: Any) -> Fix | None:
    """
    Coerce one input item to a :class:`Fix`, or ``None`` when it is unusable.

    Accepts a 5-sequence ``(lat, lon, accuracy_m, speed_mps, recorded_at)`` — the shape of
    ``values_list("lat", "lon", "accuracy_m", "speed_mps", "recorded_at")`` — a mapping, or any
    object with those attributes (a ``field.models.TrackPoint`` row, for instance).
    """
    if isinstance(raw, (tuple, list)):
        if len(raw) < 5:
            return None
        lat, lon, acc, speed, at = raw[0], raw[1], raw[2], raw[3], raw[4]
    elif hasattr(raw, "keys"):
        lat, lon = raw.get("lat"), raw.get("lon")
        acc, speed, at = raw.get("accuracy_m"), raw.get("speed_mps"), raw.get("recorded_at")
    else:
        lat, lon = getattr(raw, "lat", None), getattr(raw, "lon", None)
        acc = getattr(raw, "accuracy_m", None)
        speed = getattr(raw, "speed_mps", None)
        at = getattr(raw, "recorded_at", None)
    lat, lon = _number(lat), _number(lon)
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None
    if not isinstance(at, datetime):
        return None
    return Fix(lat, lon, _number(acc), _number(speed), at)


def clean_track(
    points: Iterable[Any],
    *,
    patrol_type: str | None = "foot",
    max_accuracy_m: float | None = None,
    max_speed_mps: float | None = None,
    drift_floor_m: float = DRIFT_FLOOR_M,
    min_leg_m: float = MIN_LEG_M,
) -> CleanTrack:
    """
    Sanitise a time-ordered track and measure it. See the module docstring for the rules.

    ``points`` is any iterable of 5-sequences / mappings / objects (see :func:`_as_fix`), ordered
    by ``recorded_at``. Nothing is mutated and the input may be a lazy queryset.
    """
    acc_limit = accuracy_limit(max_accuracy_m)
    speed_limit = float(max_speed_mps) if max_speed_mps is not None else max_speed_for(patrol_type)
    kept: list[Fix] = []
    dropped = {reason: 0 for reason in DROP_REASONS}
    total = 0
    distance_m = 0.0
    anchor: Fix | None = None  # last kept fix: what the next leg is measured from
    last_time: datetime | None = None  # last in-order timestamp, kept or not
    consecutive_jumps = 0

    for raw in points:
        total += 1
        fix = _as_fix(raw)
        if fix is None:
            dropped["invalid"] += 1
            continue
        if fix.accuracy_m is not None and fix.accuracy_m > acc_limit:
            dropped["accuracy"] += 1
            continue
        if last_time is not None and fix.recorded_at <= last_time:
            dropped["time"] += 1
            continue
        last_time = fix.recorded_at
        if anchor is None:
            kept.append(fix)
            anchor = fix
            continue

        dt_s = (fix.recorded_at - anchor.recorded_at).total_seconds()
        leg_m = haversine_m(anchor.lat, anchor.lon, fix.lat, fix.lon)
        implied_mps = leg_m / dt_s if dt_s > 0 else math.inf
        if implied_mps > speed_limit or (fix.speed_mps is not None and fix.speed_mps > speed_limit):
            consecutive_jumps += 1
            if consecutive_jumps <= MAX_CONSECUTIVE_JUMPS:
                dropped["jump"] += 1
                continue
            # Too many rejections in a row: the *anchor* is the outlier, or the device stopped
            # recording and resumed elsewhere. Re-anchor here and charge nothing for the leg we
            # have no reason to trust.
            consecutive_jumps = 0
            kept.append(fix)
            anchor = fix
            continue
        consecutive_jumps = 0

        worst_accuracy_m = max(anchor.accuracy_m or 0.0, fix.accuracy_m or 0.0)
        if leg_m < max(drift_floor_m, DRIFT_ACCURACY_FACTOR * worst_accuracy_m):
            dropped["drift"] += 1
            continue
        if leg_m >= min_leg_m:
            distance_m += leg_m
        kept.append(fix)
        anchor = fix

    return CleanTrack(points=kept, distance_m=distance_m, dropped=dropped, total=total)


def clean_distance_m(points: Iterable[Any], *, patrol_type: str | None = "foot", **kwargs) -> float:
    """Convenience wrapper: just the sanitised distance in metres."""
    return clean_track(points, patrol_type=patrol_type, **kwargs).distance_m
