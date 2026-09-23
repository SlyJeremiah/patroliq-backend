"""
``manage.py clean_tracks`` — recompute sanitised patrol distances for tracks already stored.

Patrols recorded by an app older than v1.7 (GPS *and* network fixes, a loose 50 m accuracy gate,
no jump/drift filtering) have an inflated ``distance_m``. This command runs ``geo.clean_track``
over the stored points and fills ``Patrol.distance_clean_m``; where the server owns the distance
(``distance_from_client`` is false) it corrects ``distance_m`` as well. Client-supplied distances
are left exactly as they were — nothing is lost, and reports read ``effective_distance_m``.

Track points themselves are never modified or deleted.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

import geo
from accounts.models import Organisation
from core.db import tenant_context
from field.models import Patrol, TrackPoint

BATCH = 200  # patrols per track-point query


class Command(BaseCommand):
    help = "Recompute sanitised patrol distances (geo/track.py) for stored tracks and report the change."

    def add_arguments(self, parser):
        parser.add_argument("--org", help="Organisation code (default: all)")
        parser.add_argument("--area", help="Area UUID (default: all areas)")
        parser.add_argument("--since", help="Only patrols started on/after this date, YYYY-MM-DD")
        parser.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing")

    def handle(self, *args, **opts):
        since = None
        if opts["since"]:
            since = parse_date(opts["since"])
            if since is None:
                raise CommandError("--since must be YYYY-MM-DD")
        orgs = Organisation.objects.all().order_by("code")
        if opts["org"]:
            orgs = orgs.filter(code__iexact=opts["org"])
            if not orgs.exists():
                raise CommandError(f"Unknown organisation {opts['org']}")
        dry = opts["dry_run"]

        grand = Totals()
        for org in orgs:
            with tenant_context(org.pk):
                totals = self._clean_org(org, area=opts["area"], since=since, dry=dry)
            if totals.patrols:
                self.stdout.write(f"{org.code}: {totals.line()}")
            grand.add(totals)
        verb = "would change" if dry else "changed"
        self.stdout.write(self.style.SUCCESS(
            f"{'Dry run: ' if dry else ''}{grand.patrols} patrols examined, {grand.changed} {verb}. {grand.line()}"))

    def _clean_org(self, org, area, since, dry) -> "Totals":
        qs = Patrol.objects.for_org(org)
        if area:
            qs = qs.filter(area_id=area)
        if since:
            qs = qs.filter(started_at__date__gte=since)
        patrols = list(qs.order_by("started_at").only(
            "client_uuid", "patrol_type", "distance_m", "distance_clean_m", "distance_from_client"))
        totals = Totals()
        for start in range(0, len(patrols), BATCH):
            chunk = patrols[start:start + BATCH]
            fixes = self._fixes_by_patrol(org, [p.pk for p in chunk])
            updates = []
            for patrol in chunk:
                rows = fixes.get(patrol.pk, [])
                track = geo.clean_track(rows, patrol_type=patrol.patrol_type)
                clean = round(track.distance_m, 1) if track.total else None
                totals.count(patrol, clean, track.points_dropped)
                if clean == patrol.distance_clean_m and (patrol.distance_from_client
                                                         or patrol.distance_m == (clean or 0.0)):
                    continue
                patrol.distance_clean_m = clean
                if not patrol.distance_from_client:
                    patrol.distance_m = clean or 0.0
                updates.append(patrol)
            totals.changed += len(updates)
            if updates and not dry:
                Patrol.objects.bulk_update(updates, ["distance_clean_m", "distance_m"], batch_size=BATCH)
        return totals

    @staticmethod
    def _fixes_by_patrol(org, patrol_ids) -> dict:
        out: dict = {}
        for pid, lat, lon, acc, speed, at in (
                TrackPoint.objects.filter(organisation_id=org.pk, patrol_id__in=patrol_ids)
                .order_by("patrol_id", "recorded_at")
                .values_list("patrol_id", "lat", "lon", "accuracy_m", "speed_mps", "recorded_at")):
            out.setdefault(pid, []).append((lat, lon, acc, speed, at))
        return out


class Totals:
    """Running counts for one organisation (and, summed, for the whole run)."""

    def __init__(self):
        self.patrols = self.changed = self.with_track = self.points_dropped = self.moved = 0
        self.stored_m = self.clean_m = 0.0

    def count(self, patrol, clean, points_dropped):
        """``changed`` counts rows the command writes; ``moved`` counts distances that really shift."""
        self.patrols += 1
        self.points_dropped += points_dropped
        if clean is None:
            return
        self.with_track += 1
        self.stored_m += patrol.distance_m or 0.0
        self.clean_m += clean
        self.moved += abs(clean - (patrol.distance_m or 0.0)) >= 1.0

    def add(self, other: "Totals"):
        for name in ("patrols", "changed", "with_track", "points_dropped", "moved"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.stored_m += other.stored_m
        self.clean_m += other.clean_m

    def line(self) -> str:
        delta = self.clean_m - self.stored_m
        pct = (delta / self.stored_m * 100) if self.stored_m else 0.0
        return (f"{self.with_track} with a track, {self.points_dropped} points dropped, "
                f"{self.moved} distances move by 1 m or more, "
                f"{self.stored_m / 1000:.1f} km stored -> {self.clean_m / 1000:.1f} km clean "
                f"({delta / 1000:+.1f} km, {pct:+.1f}%)")
