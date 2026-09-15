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
        base3 = ApuBase.objects.get(area=mazowe, code="APU-3")
        team, _ = Team.objects.update_or_create(organisation=grtts, area=mazowe, name="Mazowe River Team",
                                                defaults=dict(apu_base=base2))
        hq_team, _ = Team.objects.update_or_create(organisation=grtts, area=mazowe, name="HQ Camp Team",
                                                   defaults=dict(apu_base=base1))
        road_team, _ = Team.objects.update_or_create(organisation=grtts, area=mazowe, name="Boundary Road Team",
                                                     defaults=dict(apu_base=base3))

        now = timezone.now().replace(microsecond=0)
        ago = lambda **kw: now - timedelta(**kw)  # noqa: E731
        tendai = self.user(grtts, employee_id="RGR-2026-041", password="patrol123", full_name="Tendai Moyo", role="ranger",
                           phone="+263770000041", language="sn", team=team, apu_base=base2, last_sync_at=ago(minutes=2))
        farai = self.user(grtts, employee_id="RGR-2026-038", password="patrol123", full_name="Farai Ncube", role="ranger",
                          phone="+263770000038", language="nd", team=team, apu_base=base2, last_sync_at=ago(hours=3))
        grace = self.user(grtts, email="grace.mutasa@grtts.co.zw", employee_id="MGR-2026-003", password="manager123",
                          full_name="Grace Mutasa", role="manager", phone="+263770000003", apu_base=base1)
        tafadzwa = self.user(grtts, email="tafadzwa.shumba@grtts.co.zw", employee_id="ADM-2026-001", password="admin123",
                             full_name="Tafadzwa Shumba", role="org_admin", phone="+263770000001", apu_base=base1)
        # Dashboard demo rangers: three teams, one per APU base (spec §7 demo data).
        extra = [
            ("RGR-2026-042", "Rudo Chikore", "sn", team, base2, ago(hours=26)),
            ("RGR-2026-043", "Sipho Ndlovu", "nd", hq_team, base1, ago(minutes=25)),
            ("RGR-2026-044", "Kuda Dube", "sn", hq_team, base1, ago(hours=5)),
            ("RGR-2026-045", "Blessing Nyathi", "nd", hq_team, base1, ago(days=9)),
            ("RGR-2026-046", "Precious Mpofu", "nd", road_team, base3, ago(minutes=55)),
            ("RGR-2026-047", "Lindiwe Sibanda", "nd", road_team, base3, ago(hours=3, minutes=30)),
            ("RGR-2026-048", "Tatenda Gumbo", "sn", road_team, base3, ago(days=2)),
        ]
        rangers = {"tendai": tendai, "farai": farai}
        for emp, name, lang, t, b, synced in extra:
            rangers[name.split()[0].lower()] = self.user(
                grtts, employee_id=emp, password="patrol123", full_name=name, role="ranger", phone=f"+26377000{emp[-4:]}",
                language=lang, team=t, apu_base=b, last_sync_at=synced)
        for u in [grace, tafadzwa, *rangers.values()]:
            u.areas.add(mazowe)
        for t, leader in ((team, tendai), (hq_team, rangers["sipho"]), (road_team, rangers["precious"])):
            t.leader = leader
            t.save(update_fields=["leader", "updated_at"])

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

        for t, base in ((hq_team, base1), (road_team, base3)):
            bx, by = base.location["coordinates"]
            near = sorted(cells, key=lambda c: Point(c.centroid["coordinates"]).distance(Point(bx, by)))[:5]
            a, _ = Assignment.objects.update_or_create(organisation=grtts, team=t, date=today, defaults=dict(
                area=mazowe, visit_target=2, notes=f"Boundary and snare sweep around {base.code}."))
            a.cells.set(near)

        self.seed_activity(grtts, mazowe, {"river": team, "hq": hq_team, "road": road_team}, rangers, grace, now)
        for i in range(30, -1, -1):
            score_area(mazowe, today - timedelta(days=i))
        self.seed_reports(grtts, mazowe, grace, today)
        return grtts, mazowe, [tendai, farai, grace, tafadzwa, *[rangers[k] for k in list(rangers)[2:]]]

    # --- GRTTS dashboard activity (spec §7 demo data) ---------------------------------------------------

    def seed_activity(self, grtts, mazowe, teams, rangers, manager, now):
        """
        30 days of patrols with realistic foot/vehicle tracks inside Mazowe, observations, three open patrols
        (active with fresh pings, paused, and one whose ranger went offline), a critical poacher-camp alert,
        an acknowledged carcass alert and a resolved dead man's switch alert. Everything is bulk inserted
        with deterministic ids, so re-running replaces the same rows (timestamps move with "now").
        """
        import math
        import random

        from shapely.prepared import prep

        from field.models import AlertEvent, PositionPing, SafetyAlert

        rng = random.Random("patroliq-demo-grtts-v1")
        tz = zoneinfo.ZoneInfo("Africa/Harare")
        today = now.astimezone(tz).date()
        boundary = prep(geo.shape_from_geojson(mazowe.boundary))
        index = area_services.cell_index(mazowe.pk)
        bases = {b.pk: b for b in ApuBase.objects.filter(area=mazowe)}
        sp = {s.scientific_name: s for s in Species.objects.all()}
        members = {key: [u for u in rangers.values() if u.team_id == t.pk] for key, t in teams.items()}

        patrols, points, observations, pings = [], [], [], []

        def walk(start, start_dt, minutes, speed_kmh=2.6, step_min=2):
            lon, lat = start
            heading = rng.uniform(0, 360)
            out = []
            steps = max(2, minutes // step_min)
            for i in range(steps + 1):
                out.append((start_dt + timedelta(minutes=i * step_min), lon, lat))
                if i > steps * 0.55:  # head back towards the drop-off point
                    bearing = math.degrees(math.atan2((start[0] - lon) * math.cos(math.radians(lat)), start[1] - lat))
                    heading += 0.35 * (((bearing - heading) + 180) % 360 - 180)
                heading += rng.gauss(0, 22)
                step_m = speed_kmh * 1000 / 60 * step_min * rng.uniform(0.55, 1.2)
                nlat = lat + step_m * math.cos(math.radians(heading)) / 110574
                nlon = lon + step_m * math.sin(math.radians(heading)) / (111320 * math.cos(math.radians(lat)))
                if boundary.contains(Point(nlon, nlat)):
                    lon, lat = nlon, nlat
                else:
                    heading += 180 + rng.uniform(-50, 50)
            return out

        def start_near(base, spread_m=1800):
            bx, by = base.location["coordinates"]
            for _ in range(30):
                d, h = rng.uniform(0, spread_m), rng.uniform(0, 2 * math.pi)
                lon = bx + d * math.sin(h) / (111320 * math.cos(math.radians(by)))
                lat = by + d * math.cos(h) / 110574
                if boundary.contains(Point(lon, lat)):
                    return lon, lat
            return bx, by

        def add_patrol(key, ranger, team, start_dt, minutes, status="ended", patrol_type="foot", until=None, ping_every=None,
                       battery=90, notes=""):
            pid = demo_uuid(key)
            base = bases[team.apu_base_id]
            speed = 12.0 if patrol_type == "vehicle" else 2.6
            track = walk(start_near(base), start_dt, minutes, speed_kmh=speed)
            if until is not None:
                track = [p for p in track if p[0] <= until]
            dist = geo.path_length_m([(lat, lon) for _, lon, lat in track])
            ended = track[-1][0] if status == "ended" else None
            patrols.append(Patrol(
                client_uuid=pid, organisation=grtts, ranger=ranger, team=team, area=mazowe, apu_base=base,
                patrol_type=patrol_type, started_at=start_dt, ended_at=ended, status=status, distance_m=round(dist, 1),
                duration_s=int((track[-1][0] - start_dt).total_seconds()), notes=notes))
            for t, lon, lat in track:
                points.append(TrackPoint(organisation=grtts, patrol_id=pid, recorded_at=t, lat=round(lat, 7),
                                         lon=round(lon, 7), accuracy_m=rng.choice([4, 5, 6, 8, 10, 12]),
                                         speed_mps=round(speed / 3.6 * rng.uniform(0.5, 1.2), 2),
                                         cell_id=index.find(lon, lat)))
            if ping_every:
                for i, (t, lon, lat) in enumerate(track[::max(1, ping_every // 2)]):
                    pings.append(PositionPing(organisation=grtts, ranger=ranger, patrol_client_uuid=pid, recorded_at=t,
                                              lat=round(lat, 7), lon=round(lon, 7), accuracy_m=6,
                                              battery_pct=max(15, battery - i)))
            return pid, track

        wildlife_pool = [
            ("Aepyceros melampus", (4, 30), "Grazing"), ("Tragelaphus strepsiceros", (1, 6), "Browsing"),
            ("Loxodonta africana", (2, 14), "Moving to water"), ("Syncerus caffer", (5, 40), "Resting in shade"),
            ("Equus quagga", (3, 18), "Grazing"), ("Giraffa camelopardalis", (1, 5), "Browsing"),
            ("Phacochoerus africanus", (1, 6), "Foraging"), ("Kobus ellipsiprymnus", (2, 9), "Drinking"),
            ("Hippotragus niger", (3, 12), "Grazing"), ("Papio ursinus", (8, 35), "Foraging"),
            ("Crocuta crocuta", (1, 3), "Moving"), ("Panthera pardus", (1, 1), "Resting"),
            ("Sylvicapra grimmia", (1, 2), "Browsing"), ("Taurotragus oryx", (3, 15), "Moving"),
        ]
        threat_pool = [("snare", "low", "Old wire snare found and removed."),
                       ("snare", "medium", "Active snare line (3 snares) on a game trail, removed."),
                       ("footprints", "low", "Human footprints along the river bank, heading north."),
                       ("illegal_grazing", "low", "Cattle grazing inside the boundary; herder warned."),
                       ("fence_cut", "medium", "Boundary fence cut over 2 m; reported to maintenance."),
                       ("woodcutting", "low", "Fresh tree stumps, firewood collection.")]
        other_pool = [("habitat", "burnt_area", "Recent burn scar about 2 ha."),
                      ("infrastructure", "waterhole", "Borehole pump working, trough full."),
                      ("infrastructure", "fence_damage", "Elephant damage to internal fence."),
                      ("habitat", "erosion", "Gully erosion on the access track.")]

        def add_obs(key, ranger, patrol_id, t, lon, lat, **fields):
            observations.append(Observation(
                client_uuid=demo_uuid(key), organisation=grtts, patrol_id=patrol_id, area=mazowe, observer=ranger,
                lat=round(lat, 7), lon=round(lon, 7), accuracy_m=rng.choice([5, 6, 8, 10]), recorded_at=t,
                cell_id=index.find(lon, lat), **fields))

        def wildlife_fields():
            sci, (lo, hi), behaviour = rng.choice(wildlife_pool)
            s = sp.get(sci)
            count = rng.randint(lo, hi)
            fields = dict(category="wildlife", species=s, species_name=s.common_name if s else sci, count=count,
                          behaviour=behaviour,
                          age_class=rng.choice(["adult", "adult", "mixed", "unknown"]) if count > 2
                          else rng.choice(["adult", "adult", "juvenile"]))
            if count == 1:
                fields["sex"] = rng.choice(["male", "female", "unknown"])
            elif rng.random() < 0.7:
                males = rng.randint(0, count // 2)
                fields.update(sex="mixed", male_count=males, female_count=rng.randint(0, count - males))
            else:
                fields["sex"] = rng.choice(["unknown", "female"])
            return fields

        # --- 30 days of ended patrols ----------------------------------------------------------------
        for day in range(30, 0, -1):
            d = today - timedelta(days=day)
            for key, team in teams.items():
                if rng.random() > 0.62:
                    continue
                ranger = rng.choice(members[key])
                vehicle = rng.random() < 0.15
                start_dt = datetime.combine(d, time(rng.randint(5, 8), rng.choice([0, 15, 30, 45])), tzinfo=tz)
                minutes = rng.randint(100, 150) if vehicle else rng.randint(180, 320)
                pkey = f"grtts-demo-d{day}-{key}"
                pid, track = add_patrol(pkey, ranger, team, start_dt, minutes, patrol_type="vehicle" if vehicle else "foot",
                                        notes=rng.choice(["Routine patrol.", "Snare sweep.", "Boundary check.",
                                                          "Waterhole monitoring.", ""]))
                for k in range(rng.randint(1, 4)):
                    t, lon, lat = track[rng.randrange(1, len(track))]
                    roll = rng.random()
                    if roll < 0.68:
                        fields = wildlife_fields()
                    elif roll < 0.86:
                        subtype, severity, note = rng.choice(threat_pool)
                        fields = dict(category="threat", subtype=subtype, severity=severity, notes=note)
                    else:
                        category, subtype, note = rng.choice(other_pool)
                        fields = dict(category=category, subtype=subtype, notes=note)
                    add_obs(f"{pkey}-obs{k}", ranger, pid, t, lon, lat, **fields)

        # Acknowledged alert: poached elephant carcass 12 days ago.
        d12 = datetime.combine(today - timedelta(days=12), time(9, 40), tzinfo=tz)
        kuda, sipho, blessing = rangers["kuda"], rangers["sipho"], rangers["blessing"]
        pid, track = add_patrol("grtts-demo-carcass-patrol", kuda, teams["hq"], d12 - timedelta(hours=2), 240)
        _, lon, lat = track[len(track) // 2]
        elephant = sp.get("Loxodonta africana")
        carcass_id = demo_uuid("grtts-demo-obs-carcass")
        add_obs("grtts-demo-obs-carcass", kuda, pid, d12, lon, lat, category="carcass", subtype="elephant_carcass",
                species=elephant, species_name=elephant.common_name if elephant else "Elephant", count=1, sex="male",
                age_class="adult", severity="high", alert_manager=True,
                notes="Adult bull, tusks removed with an axe, estimated 3-4 days dead. Scene photographed.",
                acknowledged_at=d12 + timedelta(minutes=18), acknowledged_by=manager)
        add_obs("grtts-demo-obs-carcass-natural", rangers["lindiwe"], None, d12 - timedelta(days=8), 30.975, -17.520,
                category="carcass", subtype="impala_carcass", species=sp.get("Aepyceros melampus"), species_name="Impala",
                count=1, sex="female", severity="low", notes="Predator kill, likely leopard.")

        # --- today: open patrols ----------------------------------------------------------------------
        pid, track = add_patrol("grtts-demo-live-tendai", rangers["tendai"], teams["river"], now - timedelta(minutes=150), 150,
                                status="active", until=now - timedelta(minutes=2), ping_every=5, battery=88,
                                notes="Follow-up on the GRTS-047 snare report.")
        t, lon, lat = track[-20]
        add_obs("grtts-demo-obs-poacher-camp", rangers["tendai"], pid, t, lon, lat, category="threat",
                subtype="poacher_camp", severity="critical", alert_manager=True, count=3,
                notes="Fresh camp, 3 sleeping spots, fire still warm, bushmeat drying rack. Suspects not seen.")
        for k, idx in enumerate((10, 35)):
            t, lon, lat = track[min(idx, len(track) - 1)]
            add_obs(f"grtts-demo-live-tendai-obs{k}", rangers["tendai"], pid, t, lon, lat, **wildlife_fields())
        pid, track = add_patrol("grtts-demo-live-sipho", sipho, teams["hq"], now - timedelta(minutes=190), 190,
                                status="paused", until=now - timedelta(minutes=25), ping_every=5, battery=64,
                                notes="Paused at waterhole observation point.")
        t, lon, lat = track[len(track) // 2]
        add_obs("grtts-demo-live-sipho-obs0", sipho, pid, t, lon, lat, **wildlife_fields())
        pid, track = add_patrol("grtts-demo-live-precious", rangers["precious"], teams["road"], now - timedelta(minutes=240), 240,
                                status="active", until=now - timedelta(minutes=55), ping_every=5, battery=31,
                                notes="Boundary road fence line.")
        t, lon, lat = track[len(track) // 3]
        subtype, severity, note = threat_pool[4]
        add_obs("grtts-demo-live-precious-obs0", rangers["precious"], pid, t, lon, lat, category="threat", subtype=subtype,
                severity=severity, notes=note)
        pid, track = add_patrol("grtts-demo-today-lindiwe", rangers["lindiwe"], teams["road"], now - timedelta(hours=7), 200,
                                ping_every=5, battery=95, notes="Morning vehicle patrol.", patrol_type="vehicle")
        t, lon, lat = track[len(track) // 2]
        add_obs("grtts-demo-today-lindiwe-obs0", rangers["lindiwe"], pid, t, lon, lat, **wildlife_fields())

        # --- replace previous demo rows, then bulk insert ----------------------------------------------
        # Every id the generator can produce (not only this run's), so stale rows never survive a re-run.
        possible = [f"grtts-demo-d{day}-{key}" for day in range(1, 31) for key in teams]
        patrol_ids = list({p.pk for p in patrols} | {demo_uuid(k) for k in possible})
        obs_ids = list({o.pk for o in observations} | {demo_uuid(f"{k}-obs{i}") for k in possible for i in range(4)})
        TrackPoint.objects.filter(organisation=grtts, patrol_id__in=patrol_ids).delete()
        PositionPing.objects.filter(organisation=grtts, patrol_client_uuid__in=patrol_ids).delete()
        Observation.objects.filter(pk__in=obs_ids).delete()
        Patrol.objects.filter(pk__in=patrol_ids).delete()
        Patrol.objects.bulk_create(patrols, batch_size=500)
        TrackPoint.objects.bulk_create(points, batch_size=2000)
        Observation.objects.bulk_create(observations, batch_size=500)
        PositionPing.objects.bulk_create(pings, batch_size=1000)

        # Resolved dead man's switch alert 5 days ago (acknowledged, responders dispatched, resolved).
        dms_at = datetime.combine(today - timedelta(days=5), time(11, 20), tzinfo=tz)
        dms_id = demo_uuid("grtts-demo-dms-kuda")
        AlertEvent.objects.filter(alert_id__in=[dms_id, carcass_id]).delete()
        SafetyAlert.objects.filter(pk=dms_id).delete()
        bx, by = bases[teams["hq"].apu_base_id].location["coordinates"]
        SafetyAlert.objects.create(
            client_uuid=dms_id, organisation=grtts, ranger=kuda, kind="dead_mans_switch", status="resolved",
            lat=by - 0.012, lon=bx + 0.009, accuracy_m=9, battery_pct=7, signal_level=1, started_at=dms_at,
            acknowledged_at=dms_at + timedelta(minutes=4), acknowledged_by=manager, resolved_at=dms_at + timedelta(minutes=55),
            resolution_note="Ranger found safe: fell and sprained an ankle, phone battery died. Evacuated to HQ Camp.")
        AlertEvent.objects.bulk_create([
            AlertEvent(organisation=grtts, alert_id=dms_id, alert_type="safety", action="acknowledged", actor=manager,
                       at=dms_at + timedelta(minutes=4)),
            AlertEvent(organisation=grtts, alert_id=dms_id, alert_type="safety", action="dispatched", actor=manager,
                       note="Sipho and Blessing from APU-1 to the last known position.",
                       responder_ids=[str(sipho.pk), str(blessing.pk)], at=dms_at + timedelta(minutes=6)),
            AlertEvent(organisation=grtts, alert_id=dms_id, alert_type="safety", action="resolved", actor=manager,
                       note="Ranger found safe: fell and sprained an ankle, phone battery died. Evacuated to HQ Camp.",
                       at=dms_at + timedelta(minutes=55)),
            AlertEvent(organisation=grtts, alert_id=carcass_id, alert_type="threat", action="acknowledged", actor=manager,
                       note="ZPWMA investigations unit informed.", at=d12 + timedelta(minutes=18)),
        ])
        self._activity = {"patrols": len(patrols), "track_points": len(points), "observations": len(observations),
                          "pings": len(pings)}

    def seed_reports(self, grtts, mazowe, manager, today):
        from dashboard import reports as report_service
        from dashboard.models import Report

        specs = [
            ("grtts-demo-report-patrol-pdf", "patrol_summary", "pdf"),
            ("grtts-demo-report-census-csv", "wildlife_census", "csv"),
        ]
        for key, rtype, fmt in specs:
            rid = demo_uuid(key)
            for old in Report.objects.filter(pk=rid):
                if old.file:
                    old.file.delete(save=False)
                old.delete()
            params = {"type": rtype, "format": fmt, "date_from": (today - timedelta(days=30)).isoformat(),
                      "date_to": today.isoformat(), "area_id": str(mazowe.pk), "sector_id": None, "ranger_id": None,
                      "species_id": None}
            report_service.generate(grtts, manager, params, anonymised=False, report_id=rid)

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
        act = getattr(self, "_activity", None)
        if act:
            w(f"Dashboard demo: {act['patrols']} patrols, {act['track_points']} track points, {act['observations']} "
              f"observations, {act['pings']} position pings, 31 days of risk scores, 2 reports.")
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
