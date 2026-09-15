from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date

from accounts.licensing import effective_status, module_enabled
from accounts.models import Organisation
from areas.models import Area
from areas.risk import score_area
from core.db import tenant_context


class Command(BaseCommand):
    help = "Compute heuristic risk scores (PRD 6.4 Phase 1) for active areas of organisations with the ai_risk module."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="YYYY-MM-DD (default: today)")
        parser.add_argument("--org", help="Organisation code (default: all)")
        parser.add_argument("--area", help="Area UUID (default: all active areas)")
        parser.add_argument("--hour", type=int, default=20, help="Local hour for time-of-day/moon factors (default 20)")

    def handle(self, *args, **opts):
        day: date = timezone.localdate()
        if opts["date"]:
            day = parse_date(opts["date"])
            if day is None:
                raise CommandError("--date must be YYYY-MM-DD")
        orgs = Organisation.objects.all()
        if opts["org"]:
            orgs = orgs.filter(code__iexact=opts["org"])
            if not orgs.exists():
                raise CommandError(f"Unknown organisation {opts['org']}")
        total = 0
        for org in orgs:
            if not module_enabled(org, "ai_risk") or effective_status(org) == "suspended":
                self.stdout.write(f"skip {org.code}: ai_risk disabled or licence suspended")
                continue
            with tenant_context(org.pk):
                areas = Area.objects.for_org(org).filter(status="active")
                if opts["area"]:
                    areas = areas.filter(pk=opts["area"])
                for area in areas:
                    n = score_area(area, day, hour=opts["hour"])
                    total += n
                    self.stdout.write(f"{org.code} / {area.name}: {n} cells scored for {day}")
        self.stdout.write(self.style.SUCCESS(f"Done: {total} risk scores."))
