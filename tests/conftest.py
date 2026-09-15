from __future__ import annotations

import itertools
import uuid
from datetime import timedelta

import pyotp
import pytest
from django.utils import timezone
from pyproj import Transformer
from rest_framework.test import APIClient
from shapely.geometry import Polygon

import geo
from accounts.models import MODULE_CHOICES, AuthToken, Licence, Organisation, User
from areas import services as area_services
from areas.models import ApuBase, Area

_seq = itertools.count(1)


@pytest.fixture(autouse=True)
def _media_root(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")


def make_org(code=None, modules=None, **licence):
    code = code or f"ORG{next(_seq)}"
    org = Organisation.objects.create(name=f"Org {code}", code=code, country="Zimbabwe")
    defaults = dict(plan="standard", max_rangers=20, max_managers=5, max_areas=5,
                    modules=list(MODULE_CHOICES) if modules is None else modules,
                    starts_at=timezone.now() - timedelta(days=30), expires_at=timezone.now() + timedelta(days=365),
                    grace_days=14)
    defaults.update(licence)
    Licence.objects.create(organisation=org, **defaults)
    return org


def make_user(org, role="ranger", password="patrol123", **fields):
    n = next(_seq)
    if role == "ranger":
        fields.setdefault("employee_id", f"RGR-{n:04d}")
    else:
        fields.setdefault("email", f"user{n}@example.org")
    fields.setdefault("full_name", f"{role.title()} {n}")
    fields.setdefault("phone", f"+26377{n:07d}")
    u = User(organisation=org, role=role, **fields)
    u.set_password(password)
    if role in ("manager", "org_admin", "platform_admin"):
        u.totp_secret = pyotp.random_base32()
    u.save()
    return u


def client_for(user=None) -> APIClient:
    c = APIClient()
    if user is not None:
        token = AuthToken.objects.create(user=user, device_id="test")
        c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        c.token = token
    return c


def utm_square(lon=30.95, lat=-17.50, width_km=5, height_km=4) -> Polygon:
    """WGS84 polygon whose corners are whole kilometres in the local UTM zone (grid-aligned)."""
    crs = geo.utm_crs_for(lon, lat)
    fwd = Transformer.from_crs(4326, crs, always_xy=True)
    back = Transformer.from_crs(crs, 4326, always_xy=True)
    x, y = fwd.transform(lon, lat)
    x0, y0 = round(x / 1000) * 1000, round(y / 1000) * 1000
    corners = [(x0, y0), (x0 + width_km * 1000, y0), (x0 + width_km * 1000, y0 + height_km * 1000),
               (x0, y0 + height_km * 1000)]
    return Polygon([back.transform(cx, cy) for cx, cy in corners])


def make_area(org, boundary=None, bases=None, grid=True, status="active", name=None):
    area = Area.objects.create(organisation=org, name=name or f"Area {next(_seq)}", client_name="Client",
                               area_type="conservancy")
    boundary = boundary if boundary is not None else utm_square()
    area_services.set_boundary(area, geo.normalise_boundary(boundary), "drawn")
    if bases is None:
        c = boundary.centroid
        bases = [(c.x, c.y)]
    for i, (lon, lat) in enumerate(bases, start=1):
        ApuBase.objects.create(organisation=org, area=area, name=f"Base {i}", code=f"APU-{i}", call_sign=f"CS{i}",
                               location={"type": "Point", "coordinates": [lon, lat]})
    if grid:
        area_services.generate_grid(area, 1000, seed="test")
    area.status = status
    area.save()
    return area


def totp_now(user) -> str:
    return pyotp.TOTP(user.totp_secret).now()


def new_uuid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def org_a(db):
    return make_org("AAA")


@pytest.fixture
def org_b(db):
    return make_org("BBB")
