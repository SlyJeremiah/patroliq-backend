"""
Conservation areas and their spatial set-up (spec §3–4). Geometries are GeoJSON in JSON columns;
all spatial computation goes through the :mod:`geo` package.
"""
from django.conf import settings
from django.db import models

from core.models import TenantModel, TimeStampedModel, UUIDModel


class Area(UUIDModel, TenantModel, TimeStampedModel):
    AREA_TYPES = [
        ("national_park", "National park"),
        ("safari_area", "Safari area"),
        ("conservancy", "Conservancy"),
        ("concession", "Concession"),
        ("community", "Community area"),
        ("other", "Other"),
    ]
    BOUNDARY_SOURCES = [("shapefile", "Shapefile"), ("geojson", "GeoJSON"), ("kml", "KML"), ("drawn", "Drawn")]
    STATUS_CHOICES = [("draft", "Draft"), ("active", "Active"), ("archived", "Archived")]

    name = models.CharField(max_length=200)
    client_name = models.CharField(max_length=200, blank=True, default="")
    area_type = models.CharField(max_length=32, choices=AREA_TYPES, default="other")
    boundary = models.JSONField(null=True, blank=True, help_text="GeoJSON MultiPolygon, WGS84")
    boundary_source = models.CharField(max_length=16, choices=BOUNDARY_SOURCES, null=True, blank=True)
    area_km2 = models.FloatField(default=0.0)
    timezone = models.CharField(max_length=64, default="Africa/Harare")
    grid_cell_size_m = models.PositiveIntegerField(default=1000)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="draft", db_index=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class FeatureLayer(UUIDModel, TenantModel, TimeStampedModel):
    """Optional risk-engine inputs per area (roads, water). Missing layers are simply skipped."""

    KINDS = [("roads", "Roads"), ("water", "Water")]
    area = models.ForeignKey(Area, on_delete=models.CASCADE, related_name="feature_layers")
    kind = models.CharField(max_length=16, choices=KINDS)
    geometry = models.JSONField(help_text="GeoJSON geometry / Feature / FeatureCollection, WGS84")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["area", "kind"], name="uniq_layer_kind_per_area")]


class ApuBase(UUIDModel, TenantModel, TimeStampedModel):
    area = models.ForeignKey(Area, on_delete=models.CASCADE, related_name="apu_bases")
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=32)
    call_sign = models.CharField(max_length=64, blank=True, default="")
    location = models.JSONField(help_text="GeoJSON Point")

    class Meta:
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["area", "code"], name="uniq_base_code_per_area")]

    def __str__(self):
        return f"{self.code} {self.name}"


class Sector(UUIDModel, TenantModel, TimeStampedModel):
    area = models.ForeignKey(Area, on_delete=models.CASCADE, related_name="sectors")
    name = models.CharField(max_length=200)
    apu_base = models.ForeignKey(ApuBase, null=True, blank=True, on_delete=models.SET_NULL, related_name="sectors")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class GrtsCell(UUIDModel, TenantModel, TimeStampedModel):
    area = models.ForeignKey(Area, on_delete=models.CASCADE, related_name="cells")
    sector = models.ForeignKey(Sector, null=True, blank=True, on_delete=models.SET_NULL, related_name="cells")
    label = models.CharField(max_length=16)
    grts_order = models.PositiveIntegerField()
    geometry = models.JSONField(help_text="GeoJSON Polygon")
    centroid = models.JSONField(help_text="GeoJSON Point")
    # Bounding box for cheap pre-filtering before exact point-in-polygon tests.
    min_lon = models.FloatField()
    min_lat = models.FloatField()
    max_lon = models.FloatField()
    max_lat = models.FloatField()

    class Meta:
        ordering = ["grts_order"]
        constraints = [models.UniqueConstraint(fields=["area", "grts_order"], name="uniq_grts_order_per_area")]

    def __str__(self):
        return self.label


class Team(UUIDModel, TenantModel, TimeStampedModel):
    area = models.ForeignKey(Area, on_delete=models.CASCADE, related_name="teams")
    apu_base = models.ForeignKey(ApuBase, null=True, blank=True, on_delete=models.SET_NULL, related_name="teams")
    name = models.CharField(max_length=200)
    leader = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="led_teams")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Assignment(UUIDModel, TenantModel, TimeStampedModel):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="assignments")
    area = models.ForeignKey(Area, on_delete=models.CASCADE, related_name="assignments")
    date = models.DateField(db_index=True)
    cells = models.ManyToManyField(GrtsCell, blank=True, related_name="assignments")
    visit_target = models.PositiveIntegerField(default=1)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-date"]


class RiskScore(UUIDModel, TenantModel, TimeStampedModel):
    LEVELS = [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("critical", "Critical")]
    area = models.ForeignKey(Area, on_delete=models.CASCADE, related_name="risk_scores")
    cell = models.ForeignKey(GrtsCell, on_delete=models.CASCADE, related_name="risk_scores")
    date = models.DateField(db_index=True)
    score = models.FloatField()
    level = models.CharField(max_length=16, choices=LEVELS)
    factors = models.JSONField(default=list)

    class Meta:
        ordering = ["-date", "-score"]
        constraints = [models.UniqueConstraint(fields=["cell", "date"], name="uniq_risk_per_cell_date")]
