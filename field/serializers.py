from __future__ import annotations

from rest_framework import serializers

from core.validation import SanitizedCharField, StrictSerializer

from .models import Media, Observation, Patrol, PositionPing, SafetyAlert, Species, TrackPoint

LAT = dict(min_value=-90, max_value=90)
LON = dict(min_value=-180, max_value=180)


# --- push item schemas (input) -----------------------------------------------------------------

class PatrolInSerializer(StrictSerializer):
    client_uuid = serializers.UUIDField()
    ranger_id = serializers.UUIDField(required=False, allow_null=True)
    team_id = serializers.UUIDField(required=False, allow_null=True)
    area_id = serializers.UUIDField()
    apu_base_id = serializers.UUIDField(required=False, allow_null=True)
    patrol_type = serializers.ChoiceField(choices=[c for c, _ in Patrol.TYPES], default="foot")
    started_at = serializers.DateTimeField()
    ended_at = serializers.DateTimeField(required=False, allow_null=True)
    status = serializers.ChoiceField(choices=[c for c, _ in Patrol.STATUSES], default="active")
    distance_m = serializers.FloatField(required=False, allow_null=True, min_value=0, max_value=10_000_000)
    duration_s = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100_000_000)
    notes = SanitizedCharField(max_length=1000, required=False, allow_null=True)
    debrief_audio = serializers.CharField(max_length=500, required=False, allow_null=True, allow_blank=True)

    def validate(self, attrs):
        if attrs.get("ended_at") and attrs["ended_at"] < attrs["started_at"]:
            raise serializers.ValidationError({"ended_at": ["Must not be before started_at."]})
        return attrs


class TrackPointInSerializer(StrictSerializer):
    patrol_client_uuid = serializers.UUIDField()
    recorded_at = serializers.DateTimeField()
    lat = serializers.FloatField(**LAT)
    lon = serializers.FloatField(**LON)
    accuracy_m = serializers.FloatField(required=False, allow_null=True, min_value=0)
    speed_mps = serializers.FloatField(required=False, allow_null=True, min_value=0)


class ObservationInSerializer(StrictSerializer):
    client_uuid = serializers.UUIDField()
    patrol_client_uuid = serializers.UUIDField(required=False, allow_null=True)
    area_id = serializers.UUIDField()
    observer_id = serializers.UUIDField(required=False, allow_null=True)
    category = serializers.ChoiceField(choices=[c for c, _ in Observation.CATEGORIES])
    subtype = SanitizedCharField(max_length=64, required=False, allow_null=True)
    species_id = serializers.UUIDField(required=False, allow_null=True)
    species_name = SanitizedCharField(max_length=128, required=False, allow_null=True)
    count = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100000)
    sex = serializers.ChoiceField(choices=[c for c, _ in Observation.SEXES], required=False, allow_null=True)
    male_count = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100000)
    female_count = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100000)
    age_class = serializers.ChoiceField(choices=[c for c, _ in Observation.AGE_CLASSES], required=False, allow_null=True)
    behaviour = SanitizedCharField(max_length=255, required=False, allow_null=True)
    severity = serializers.ChoiceField(choices=[c for c, _ in Observation.SEVERITIES], required=False, allow_null=True)
    direction_of_travel = SanitizedCharField(max_length=32, required=False, allow_null=True)
    alert_manager = serializers.BooleanField(default=False)
    notes = SanitizedCharField(max_length=1000, required=False, allow_null=True)
    lat = serializers.FloatField(**LAT)
    lon = serializers.FloatField(**LON)
    accuracy_m = serializers.FloatField(required=False, allow_null=True, min_value=0)
    cell_id = serializers.UUIDField(required=False, allow_null=True)  # ignored: always derived server-side
    ai_species_confidence = serializers.FloatField(required=False, allow_null=True, min_value=0, max_value=1)
    recorded_at = serializers.DateTimeField()
    voice_transcript = SanitizedCharField(max_length=5000, required=False, allow_null=True)

    def validate(self, attrs):
        errors = {}
        if attrs["category"] == "wildlife":
            if not attrs.get("sex"):
                attrs["sex"] = "unknown"  # spec §4: required for wildlife, default unknown
        male, female, count = attrs.get("male_count"), attrs.get("female_count"), attrs.get("count")
        if male is not None or female is not None:
            if attrs.get("sex") != "mixed":
                errors["sex"] = ["male_count/female_count are only allowed when sex is 'mixed'."]
            elif count is None:
                errors["count"] = ["count is required when male_count/female_count are given."]
            else:
                for name, value in (("male_count", male), ("female_count", female)):
                    if value is not None and value > count:
                        errors[name] = [f"Must not exceed count ({count})."]
                if not errors and (male or 0) + (female or 0) > count:
                    errors["male_count"] = [f"male_count + female_count must not exceed count ({count})."]
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class SafetyAlertInSerializer(serializers.Serializer):
    """
    Deliberately lenient (safety first): unknown keys are ignored rather than rejected and only
    ``client_uuid`` is required — an SOS without a GPS fix is still an SOS.
    """

    client_uuid = serializers.UUIDField()
    ranger_id = serializers.UUIDField(required=False, allow_null=True)
    kind = serializers.ChoiceField(choices=[c for c, _ in SafetyAlert.KINDS], default="panic")
    status = serializers.CharField(required=False, allow_null=True)  # server-controlled; ignored
    area_id = serializers.UUIDField(required=False, allow_null=True)
    # Never validated here: HWC details are cleaned leniently by field.hwc.clean_details so that a
    # malformed details block can never make the safety call fail.
    details = serializers.JSONField(required=False, allow_null=True)
    lat = serializers.FloatField(required=False, allow_null=True, **LAT)
    lon = serializers.FloatField(required=False, allow_null=True, **LON)
    accuracy_m = serializers.FloatField(required=False, allow_null=True, min_value=0)
    battery_pct = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100)
    signal_level = serializers.IntegerField(required=False, allow_null=True)
    started_at = serializers.DateTimeField(required=False, allow_null=True)
    resolved_at = serializers.DateTimeField(required=False, allow_null=True)
    resolution_note = SanitizedCharField(max_length=1000, required=False, allow_null=True)


class SafetyCancelSerializer(StrictSerializer):
    pin_verified = serializers.BooleanField()
    note = SanitizedCharField(max_length=1000, required=False, allow_null=True)


class PositionPingInSerializer(StrictSerializer):
    ranger_id = serializers.UUIDField(required=False, allow_null=True)
    patrol_client_uuid = serializers.UUIDField(required=False, allow_null=True)
    recorded_at = serializers.DateTimeField()
    lat = serializers.FloatField(**LAT)
    lon = serializers.FloatField(**LON)
    accuracy_m = serializers.FloatField(required=False, allow_null=True, min_value=0)
    battery_pct = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100)


# --- output ------------------------------------------------------------------------------------

class SpeciesSerializer(serializers.ModelSerializer):
    class Meta:
        model = Species
        fields = ["id", "common_name", "scientific_name", "shona_name", "ndebele_name", "iucn_status", "taxon_group"]


class RoundedInt(serializers.Field):
    def to_representation(self, value):
        return int(round(value or 0))


class PatrolSerializer(serializers.ModelSerializer):
    ranger_id = serializers.UUIDField(read_only=True)
    team_id = serializers.UUIDField(read_only=True)
    area_id = serializers.UUIDField(read_only=True)
    apu_base_id = serializers.UUIDField(read_only=True)
    distance_m = RoundedInt(read_only=True)

    class Meta:
        model = Patrol
        fields = ["client_uuid", "ranger_id", "team_id", "area_id", "apu_base_id", "patrol_type", "started_at",
                  "ended_at", "status", "distance_m", "duration_s", "notes", "debrief_audio"]


class TrackPointSerializer(serializers.ModelSerializer):
    patrol_client_uuid = serializers.UUIDField(source="patrol_id", read_only=True)

    class Meta:
        model = TrackPoint
        fields = ["patrol_client_uuid", "recorded_at", "lat", "lon", "accuracy_m", "speed_mps"]


class MediaSerializer(serializers.ModelSerializer):
    observation_client_uuid = serializers.UUIDField(source="observation_id", read_only=True)
    url = serializers.SerializerMethodField()

    class Meta:
        model = Media
        fields = ["id", "observation_client_uuid", "kind", "content_type", "size_bytes", "sha256", "url"]

    def get_url(self, obj):
        path = f"/api/v1/media/{obj.pk}/file/"
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path


class ObservationSerializer(serializers.ModelSerializer):
    patrol_client_uuid = serializers.UUIDField(source="patrol_id", read_only=True)
    area_id = serializers.UUIDField(read_only=True)
    observer_id = serializers.UUIDField(read_only=True)
    species_id = serializers.UUIDField(read_only=True)
    cell_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Observation
        fields = ["client_uuid", "patrol_client_uuid", "area_id", "observer_id", "category", "subtype", "species_id",
                  "species_name", "count", "sex", "male_count", "female_count", "age_class", "behaviour", "severity",
                  "direction_of_travel", "alert_manager", "notes", "lat", "lon", "accuracy_m", "cell_id",
                  "ai_species_confidence", "recorded_at", "voice_transcript"]


class ObservationListSerializer(ObservationSerializer):
    media = MediaSerializer(many=True, read_only=True)

    class Meta(ObservationSerializer.Meta):
        fields = ObservationSerializer.Meta.fields + ["media"]


class SafetyAlertSerializer(serializers.ModelSerializer):
    ranger_id = serializers.UUIDField(read_only=True)
    area_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = SafetyAlert
        fields = ["client_uuid", "ranger_id", "kind", "status", "lat", "lon", "accuracy_m", "battery_pct",
                  "signal_level", "started_at", "resolved_at", "resolution_note", "area_id", "details",
                  "details_updated_at"]


class PositionPingSerializer(serializers.ModelSerializer):
    ranger_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = PositionPing
        fields = ["ranger_id", "patrol_client_uuid", "recorded_at", "lat", "lon", "accuracy_m", "battery_pct"]
