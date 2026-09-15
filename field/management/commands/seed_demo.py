"""
``python manage.py seed_demo`` — demo data per Platform Spec v1.2 §6. Idempotent: re-running updates
the same rows (natural keys / fixed UUIDs) and never duplicates. Demo passwords are reset to their
documented values on every run. Refuses to run with DEBUG off unless --allow-production.
"""
from __future__ import annotations

import uuid
import zoneinfo
from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from shapely.geometry import Point, Polygon

import geo
from accounts.models import MODULE_CHOICES, Licence, LoginLockout, Organisation, User
from accounts.security import new_totp_secret, totp_uri
from areas import services as area_services
from areas.models import ApuBase, Area, Assignment, FeatureLayer, GrtsCell, Team
from areas.risk import score_area
from field.models import SPECIES_NAMESPACE, Observation, Patrol, Species, TrackPoint
from field.species_data import SPECIES

DEMO_NS = uuid.UUID("0b3c7f5e-2d7a-4a8e-9f61-7e3d2a1c9b40")


def demo_uuid(name: str) -> uuid.UUID:
    return uuid.uuid5(DEMO_NS, name)


MAZOWE_BOUNDARY = [
    (30.894, -17.455), (30.960, -17.452), (31.006, -17.462), (31.007, -17.520), (30.998, -17.545),
    (30.930, -17.546), (30.893, -17.530), (30.896, -17.490), (30.894, -17.455),
]
MAZOWE_BASES = [
    ("APU-1", "HQ Camp", "Mazowe One", (30.955, -17.505)),
    ("APU-2", "Mazowe River", "Mazowe Two", (30.915, -17.470)),
    ("APU-3", "Boundary Road", "Mazowe Three", (30.990, -17.530)),
]
SVT_BOUNDARY = [(32.020, -20.230), (32.078, -20.228), (32.080, -20.282), (32.022, -20.285), (32.020, -20.230)]


class Command(BaseCommand):
    help = "Seed demo organisations, areas, users, species and risk scores (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--allow-production", action="store_true", help="Run even when DEBUG is off.")

    def handle(self, *args, **opts):
        if not settings.DEBUG and not opts["allow_production"]:
            raise CommandError("seed_demo creates accounts with known passwords; set DJANGO_DEBUG=true or pass "
                               "--allow-production.")
        with transaction.atomic():
            self.seed_species()
            platform_admin = self.user(None, email="admin@zrgissolutions.com", full_name="zrGISsolutions Admin",
                                       role="platform_admin", password="platform123", is_staff=True, is_superuser=True)
            grtts, mazowe, grtts_users = self.seed_grtts()
            svt_users = self.seed_svt()
        self.report(platform_admin, grtts_users, svt_users, mazowe)

    # --- helpers ---------------------------------------------------------------------------------

    def seed_species(self):
        for common, sci, sn, nd, iucn in SPECIES:
            Species.objects.update_or_create(
                id=uuid.uuid5(SPECIES_NAMESPACE, sci.strip().lower()),
                defaults=dict(common_name=common, scientific_name=sci, shona_name=sn, ndebele_name=nd, iucn_status=iucn),
            )

    def user(self, org, *, password, email=None, employee_id=None, **fields):
        if email:
            u = User.objects.filter(email__iexact=email).first()
        else:
            u = User.objects.filter(organisation=org, employee_id=employee_id).first()
        if u is None:
            u = User(organisation=org, email=email, employee_id=employee_id)
        for k, v in fields.items():
            setattr(u, k, v)
        u.organisation, u.email, u.employee_id, u.is_active = org, email, employee_id, True
        u.set_password(password)
        if u.role in ("manager", "org_admin", "platform_admin") and not u.totp_secret:
            u.totp_secret = new_totp_secret()
        u.save()
        LoginLockout.objects.filter(identifier__icontains=(email or employee_id or "").lower()).delete()
        LoginLockout.objects.filter(identifier__icontains=(employee_id or email or "").upper()).delete()
        u._demo_password = password
        return u

    def org(self, code, name, country, plan, expires, modules, **limits):
        org, _ = Organisation.objects.update_or_create(code=code, defaults=dict(name=name, country=country, status="active"))
        Licence.objects.update_or_create(organisation=org, defaults=dict(
            plan=plan, modules=modules, starts_at=datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
            expires_at=expires, grace_days=30, status="active", **limits))
        return org

    def area(self, org, name, client, area_type, ring, bases, cell_size=1000):
        area, _ = Area.objects.update_or_create(organisation=org, name=name, defaults=dict(
            client_name=client, area_type=area_type, timezone="Africa/Harare"))
        boundary = geo.normalise_boundary(Polygon(ring))
        if area.boundary != geo.geojson_from_shape(boundary):
            area_services.set_boundary(area, boundary, "drawn")
        for code, bname, call_sign, (lon, lat) in bases:
            ApuBase.objects.update_or_create(area=area, code=code, defaults=dict(
                organisation=org, name=bname, call_sign=call_sign, location={"type": "Point", "coordinates": [lon, lat]}))
        if not GrtsCell.objects.filter(area=area).exists():
            area_services.generate_grid(area, cell_size, seed=f"demo-{org.code}-{name}")
        if area.status != "active":
            area.status = "active"
            area.save(update_fields=["status", "updated_at"])
        return area

    # --- GRTTS -----------------------------------------------------------------------------------

    def seed_grtts(self):
        grtts = self.org("GRTTS", "GRTTS", "Zimbabwe", "standard", datetime(2027, 9, 30, 23, 59, 59, tzinfo=dt_timezone.utc),
                         list(MODULE_CHOICES), max_rangers=50, max_managers=10, max_areas=5)
        mazowe = self.area(grtts, "Mazowe Conservancy", "Mazowe Landholders Trust", "conservancy", MAZOWE_BOUNDARY, MAZOWE_BASES)
        FeatureLayer.objects.update_or_create(area=mazowe, kind="roads", defaults=dict(organisation=grtts, geometry={
            "type": "LineString", "coordinates": [[30.890, -17.548], [30.950, -17.541], [31.010, -17.535]]}))
        FeatureLayer.objects.update_or_create(area=mazowe, kind="water", defaults=dict(organisation=grtts, geometry={
            "type": "LineString", "coordinates": [[30.890, -17.462], [30.915, -17.474], [30.950, -17.489], [31.010, -17.472]]}))

        base2 = ApuBase.objects.get(area=mazowe, code="APU-2")
        base1 = ApuBase.objects.get(area=mazowe, code="APU-1")
        team, _ = Team.objects.update_or_create(organisation=grtts, area=mazowe, name="Mazowe River Team",
                                                defaults=dict(apu_base=base2))

        tendai = self.user(grtts, employee_id="RGR-2026-041", password="patrol123", full_name="Tendai Moyo", role="ranger",
                           phone="+263770000041", language="sn", team=team, apu_base=base2)
        farai = self.user(grtts, employee_id="RGR-2026-038", password="patrol123", full_name="Farai Ncube", role="ranger",
                          phone="+263770000038", language="nd", team=team, apu_base=base2)
        grace = self.user(grtts, email="grace.mutasa@grtts.co.zw", employee_id="MGR-2026-003", password="manager123",
                          full_name="Grace Mutasa", role="manager", phone="+263770000003", apu_base=base1)
        tafadzwa = self.user(grtts, email="tafadzwa.shumba@grtts.co.zw", employee_id="ADM-2026-001", password="admin123",
                             full_name="Tafadzwa Shumba", role="org_admin", phone="+263770000001", apu_base=base1)
        for u in (tendai, farai, grace, tafadzwa):
            u.areas.add(mazowe)
        team.leader = tendai
        team.save(update_fields=["leader", "updated_at"])

        # Today's assignment: GRTS-047 and its four nearest cells.
        today = timezone.localdate(timezone=zoneinfo.ZoneInfo("Africa/Harare"))
        cells = list(GrtsCell.objects.filter(area=mazowe))
        anchor = next((c for c in cells if c.label == "GRTS-047"), cells[0])
        ax, ay = anchor.centroid["coordinates"]
        nearest = sorted(cells, key=lambda c: Point(c.centroid["coordinates"]).distance(Point(ax, ay)))[:5]
        assignment, _ = Assignment.objects.update_or_create(organisation=grtts, team=team, date=today, defaults=dict(
            area=mazowe, visit_target=2, notes="Snare sweep along the Mazowe River; check GRTS-047 waterhole."))
        assignment.cells.set(nearest)

        # A patrol from yesterday with a snare report, so risk scores vary.
        yesterday = datetime.combine(today - timedelta(days=1), time(6, 0), tzinfo=dt_timezone.utc)
        pid = demo_uuid("grtts-patrol-1")
        Patrol.objects.update_or_create(client_uuid=pid, defaults=dict(
            organisation=grtts, ranger=farai, team=team, area=mazowe, apu_base=base2, patrol_type="foot",
            started_at=yesterday, ended_at=yesterday + timedelta(hours=4), status="ended",
            notes="Routine river patrol."))
        index = area_services.cell_index(mazowe.pk)
        TrackPoint.objects.filter(patrol_id=pid).delete()
        TrackPoint.objects.bulk_create([
            TrackPoint(organisation=grtts, patrol_id=pid, recorded_at=yesterday + timedelta(minutes=20 * i),
                       lat=-17.470 - 0.002 * i, lon=30.915 + 0.003 * i, accuracy_m=8,
                       cell_id=index.find(30.915 + 0.003 * i, -17.470 - 0.002 * i))
            for i in range(12)
        ])
        from field.services import recompute_patrol_metrics

        recompute_patrol_metrics(Patrol.objects.get(pk=pid))
        snare_lat, snare_lon = -17.480, 30.930
        Observation.objects.update_or_create(client_uuid=demo_uuid("grtts-obs-snare"), defaults=dict(
            organisation=grtts, patrol_id=pid, area=mazowe, observer=farai, category="threat", subtype="snare",
            severity="high", alert_manager=True, notes="Wire snare on game trail, removed.", lat=snare_lat,
            lon=snare_lon, accuracy_m=6, recorded_at=yesterday + timedelta(hours=1),
            cell_id=index.find(snare_lon, snare_lat)))
        elephant = Species.objects.get(scientific_name="Loxodonta africana")
        Observation.objects.update_or_create(client_uuid=demo_uuid("grtts-obs-elephant"), defaults=dict(
            organisation=grtts, patrol_id=pid, area=mazowe, observer=farai, category="wildlife", species=elephant,
            species_name=elephant.common_name, count=7, sex="mixed", male_count=2, female_count=4, age_class="mixed",
            behaviour="Drinking", lat=-17.486, lon=30.945, accuracy_m=10, recorded_at=yesterday + timedelta(hours=2),
            cell_id=index.find(30.945, -17.486)))

        score_area(mazowe, today)
        return grtts, mazowe, [tendai, farai, grace, tafadzwa]

    # --- Save Valley Trust (isolation proof) -------------------------------------------------------

    def seed_svt(self):
        svt = self.org("SVT", "Save Valley Trust", "Zimbabwe", "pilot", datetime(2027, 3, 31, 23, 59, 59, tzinfo=dt_timezone.utc),
                       ["grts", "ai_risk", "species_id"], max_rangers=10, max_managers=3, max_areas=1)
        area = self.area(svt, "Save Valley Block A", "Save Valley Conservancy Trust", "conservancy", SVT_BOUNDARY,
                         [("SV-1", "Main Camp", "Savanna One", (32.050, -20.255))])
        team, _ = Team.objects.update_or_create(organisation=svt, area=area, name="Block A Team",
                                                defaults=dict(apu_base=ApuBase.objects.get(area=area, code="SV-1")))
        ranger = self.user(svt, employee_id="SVT-R-001", password="patrol123", full_name="Blessing Dube", role="ranger",
                           team=team)
        admin = self.user(svt, email="admin@savevalley.example", password="admin123", full_name="Rudo Chikore",
                          role="org_admin")
        for u in (ranger, admin):
            u.areas.add(area)
        index = area_services.cell_index(area.pk)
        Observation.objects.update_or_create(client_uuid=demo_uuid("svt-obs-1"), defaults=dict(
            organisation=svt, area=area, observer=ranger, category="threat", subtype="poacher_camp", severity="critical",
            alert_manager=True, lat=-20.260, lon=32.040, accuracy_m=12, recorded_at=timezone.now() - timedelta(days=2),
            cell_id=index.find(32.040, -20.260)))
        score_area(area, timezone.localdate())
        return [ranger, admin]

    # --- output ----------------------------------------------------------------------------------

    def report(self, platform_admin, grtts_users, svt_users, mazowe):
        w = self.stdout.write
        cells = GrtsCell.objects.filter(area=mazowe).count()
        w(self.style.SUCCESS(f"Seeded. Mazowe Conservancy: {mazowe.area_km2:.1f} km2, "
                             f"{ApuBase.objects.filter(area=mazowe).count()} APU bases, {cells} cells, "
                             f"{mazowe.sectors.count()} sectors."))
        w("")
        w("Ranger app sign-in (organisation_code / employee_id / password):")
        for u in grtts_users[:2]:
            w(f"  GRTTS / {u.employee_id} / {u._demo_password}   ({u.full_name})")
        w(f"  SVT / {svt_users[0].employee_id} / {svt_users[0]._demo_password}   ({svt_users[0].full_name})")
        w("")
        w("Web sign-in (email / password / TOTP secret - add to Google Authenticator or use pyotp):")
        for u in [grtts_users[2], grtts_users[3], svt_users[1], platform_admin]:
            u.refresh_from_db()
            w(f"  {u.email} / {u._demo_password} / TOTP {u.totp_secret}   ({u.get_role_display()})")
            w(f"      {totp_uri(u)}")
