"""
Field data synced from the ranger app (spec §4).

Offline-created records use the device-generated ``client_uuid`` as primary key: re-pushing the
same record is naturally idempotent. References between offline records (``patrol_client_uuid``)
are *soft* (``db_constraint=False``) so a device may push an observation before its patrol without
being rejected; tenant scoping always applies because every row carries ``organisation``.
"""
import uuid

from django.conf import settings
from django.db import models

from core.models import TenantModel, TimeStampedModel

SPECIES_NAMESPACE = uuid.UUID("5b0a4a53-7f4e-4d1a-9d64-5f0c1b7b8e11")


def media_upload_to(instance, filename):
    """MEDIA_ROOT/uploads/<organisation>/<yyyy>/<mm>/<media id><ext> — never the client's filename."""
    import os

    from django.utils import timezone

    ext = os.path.splitext(filename or "")[1].lower()[:10]
    ext = ext if ext.replace(".", "").isalnum() else ""
    now = timezone.now()
    return f"uploads/{instance.organisation_id}/{now:%Y}/{now:%m}/{instance.id}{ext}"


class Species(TimeStampedModel):
    """Global reference data (not tenant-owned). IDs are uuid5(scientific_name) — identical on every server."""

    IUCN = [(c, c) for c in ["LC", "NT", "VU", "EN", "CR", "EW", "EX", "DD", "NE"]]
    TAXON_GROUPS = [(c, c.title()) for c in ["mammal", "bird", "reptile", "amphibian", "fish", "invertebrate"]]
    id = models.UUIDField(primary_key=True, editable=False)
    common_name = models.CharField(max_length=128)
    scientific_name = models.CharField(max_length=128, unique=True)
    shona_name = models.CharField(max_length=128, blank=True, default="")
    ndebele_name = models.CharField(max_length=128, blank=True, default="")
    iucn_status = models.CharField(max_length=2, choices=IUCN, default="NE")
    taxon_group = models.CharField(max_length=16, choices=TAXON_GROUPS, default="mammal", db_index=True)

    class Meta:
        ordering = ["common_name"]
        verbose_name_plural = "species"

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = uuid.uuid5(SPECIES_NAMESPACE, self.scientific_name.strip().lower())
        super().save(*args, **kwargs)

    def __str__(self):
        return self.common_name


class Patrol(TenantModel, TimeStampedModel):
    TYPES = [("foot", "Foot"), ("vehicle", "Vehicle"), ("horseback", "Horseback"), ("boat", "Boat")]
    STATUSES = [("active", "Active"), ("paused", "Paused"), ("ended", "Ended")]

    client_uuid = models.UUIDField(primary_key=True)
    ranger = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="patrols")
    team = models.ForeignKey("areas.Team", null=True, blank=True, on_delete=models.SET_NULL, related_name="patrols")
    area = models.ForeignKey("areas.Area", on_delete=models.PROTECT, related_name="patrols")
    apu_base = models.ForeignKey("areas.ApuBase", null=True, blank=True, on_delete=models.SET_NULL, related_name="patrols")
    patrol_type = models.CharField(max_length=16, choices=TYPES, default="foot")
    started_at = models.DateTimeField(db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUSES, default="active")
    distance_m = models.FloatField(default=0)
    # Distance over the *sanitised* track (geo.track.clean_track): outliers from the old
    # GPS+network recording dropped. NULL means "not measurable" — the patrol has no track points
    # at all — which is not the same as 0.0 ("walked nowhere"). Kept alongside distance_m so a
    # client-supplied figure is never lost; see Patrol.effective_distance_m.
    distance_clean_m = models.FloatField(null=True, blank=True)
    duration_s = models.PositiveIntegerField(default=0)
    distance_from_client = models.BooleanField(default=False)
    duration_from_client = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default="")
    debrief_audio = models.CharField(max_length=500, null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    @property
    def effective_distance_m(self) -> float:
        """
        The distance to report: the sanitised one when we have a track, else what was stored.

        Aggregates (dashboard KPIs, reports) use this; payloads that mirror the row itself
        (``GET patrols/``, the track endpoint) carry ``distance_m`` and ``distance_clean_m``
        separately so a client can still see both.
        """
        return (self.distance_m or 0.0) if self.distance_clean_m is None else self.distance_clean_m


class TrackPoint(TenantModel):
    id = models.BigAutoField(primary_key=True)
    patrol = models.ForeignKey(Patrol, on_delete=models.DO_NOTHING, db_constraint=False, related_name="track_points")
    recorded_at = models.DateTimeField()
    lat = models.FloatField()
    lon = models.FloatField()
    accuracy_m = models.FloatField(null=True, blank=True)
    speed_mps = models.FloatField(null=True, blank=True)
    cell = models.ForeignKey("areas.GrtsCell", null=True, blank=True, on_delete=models.SET_NULL, related_name="track_points")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["recorded_at"]
        constraints = [models.UniqueConstraint(fields=["patrol", "recorded_at"], name="uniq_trackpoint_time")]


class Observation(TenantModel, TimeStampedModel):
    CATEGORIES = [(c, c.title()) for c in ["wildlife", "threat", "carcass", "habitat", "infrastructure", "other"]]
    SEXES = [(c, c.title()) for c in ["male", "female", "mixed", "unknown"]]
    AGE_CLASSES = [(c, c.title()) for c in ["adult", "juvenile", "mixed", "unknown"]]
    SEVERITIES = [(c, c.title()) for c in ["low", "medium", "high", "critical"]]

    client_uuid = models.UUIDField(primary_key=True)
    patrol = models.ForeignKey(Patrol, null=True, blank=True, on_delete=models.DO_NOTHING, db_constraint=False,
                               related_name="observations")
    area = models.ForeignKey("areas.Area", on_delete=models.PROTECT, related_name="observations")
    observer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="observations")
    category = models.CharField(max_length=16, choices=CATEGORIES, db_index=True)
    subtype = models.CharField(max_length=64, null=True, blank=True)
    species = models.ForeignKey(Species, null=True, blank=True, on_delete=models.SET_NULL)
    species_name = models.CharField(max_length=128, null=True, blank=True)
    count = models.PositiveIntegerField(null=True, blank=True)
    sex = models.CharField(max_length=8, choices=SEXES, null=True, blank=True)
    male_count = models.PositiveIntegerField(null=True, blank=True)
    female_count = models.PositiveIntegerField(null=True, blank=True)
    age_class = models.CharField(max_length=8, choices=AGE_CLASSES, null=True, blank=True)
    behaviour = models.CharField(max_length=255, null=True, blank=True)
    severity = models.CharField(max_length=8, choices=SEVERITIES, null=True, blank=True)
    direction_of_travel = models.CharField(max_length=32, null=True, blank=True)
    alert_manager = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default="")
    lat = models.FloatField()
    lon = models.FloatField()
    accuracy_m = models.FloatField(null=True, blank=True)
    cell = models.ForeignKey("areas.GrtsCell", null=True, blank=True, on_delete=models.SET_NULL, related_name="observations")
    ai_species_confidence = models.FloatField(null=True, blank=True)
    recorded_at = models.DateTimeField(db_index=True)
    voice_transcript = models.TextField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name="+")

    class Meta:
        ordering = ["-recorded_at"]


class Media(TenantModel, TimeStampedModel):
    KINDS = [("photo", "Photo"), ("video", "Video"), ("audio", "Audio")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    observation = models.ForeignKey(Observation, on_delete=models.CASCADE, related_name="media")
    kind = models.CharField(max_length=8, choices=KINDS)
    content_type = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64, db_index=True)
    file = models.FileField(upload_to=media_upload_to, max_length=255)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["created_at"]
        constraints = [models.UniqueConstraint(fields=["observation", "sha256"], name="uniq_media_per_observation")]


class SafetyAlert(TenantModel, TimeStampedModel):
    """
    Panic button, dead man's switch and (v1.5) human–wildlife conflict. HWC alerts travel the same
    always-accepted safety path as SOS and additionally carry a ``details`` log that the ranger can
    fill in afterwards by re-POSTing the same ``client_uuid`` (spec v1.5 §A).
    """

    HWC = "human_wildlife_conflict"
    KINDS = [("panic", "Panic button"), ("dead_mans_switch", "Dead man's switch"),
             (HWC, "Human-wildlife conflict")]
    SOS_KINDS = ["panic", "dead_mans_switch"]
    STATUSES = [(c, c.title()) for c in ["active", "acknowledged", "resolved", "cancelled"]]

    client_uuid = models.UUIDField(primary_key=True)
    ranger = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="safety_alerts")
    kind = models.CharField(max_length=24, choices=KINDS, default="panic")
    status = models.CharField(max_length=16, choices=STATUSES, default="active", db_index=True)
    area = models.ForeignKey("areas.Area", null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="safety_alerts")
    details = models.JSONField(null=True, blank=True, help_text="HWC details log (spec v1.5 §A2)")
    details_updated_at = models.DateTimeField(null=True, blank=True)
    lat = models.FloatField(null=True, blank=True)
    lon = models.FloatField(null=True, blank=True)
    accuracy_m = models.FloatField(null=True, blank=True)
    battery_pct = models.IntegerField(null=True, blank=True)
    signal_level = models.IntegerField(null=True, blank=True)
    started_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name="+")

    class Meta:
        ordering = ["-started_at"]


class AlertEvent(TenantModel):
    """
    Timeline of manager actions on an alert (spec §7 ``alerts/{id}/``). ``alert_id`` is the id used by
    ``alerts/`` — a SafetyAlert client_uuid (``alert_type = safety``) or an Observation client_uuid
    (``threat``); a soft reference because it can point at either table.
    """

    ACTIONS = [(c, c.title()) for c in ["acknowledged", "dispatched", "resolved", "cancelled", "note"]]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    alert_id = models.UUIDField(db_index=True)
    alert_type = models.CharField(max_length=8, choices=[("safety", "Safety"), ("threat", "Threat")])
    action = models.CharField(max_length=16, choices=ACTIONS)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="+")
    note = models.TextField(blank=True, default="")
    responder_ids = models.JSONField(default=list, blank=True)
    at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["at"]


class PositionPing(TenantModel):
    id = models.BigAutoField(primary_key=True)
    ranger = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="position_pings")
    patrol_client_uuid = models.UUIDField(null=True, blank=True)
    recorded_at = models.DateTimeField(db_index=True)
    lat = models.FloatField()
    lon = models.FloatField()
    accuracy_m = models.FloatField(null=True, blank=True)
    battery_pct = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-recorded_at"]
        indexes = [models.Index(fields=["ranger", "-recorded_at"])]
