from django.contrib import admin

from .models import Media, Observation, Patrol, PositionPing, SafetyAlert, Species, TrackPoint


@admin.register(Species)
class SpeciesAdmin(admin.ModelAdmin):
    list_display = ("common_name", "scientific_name", "shona_name", "ndebele_name", "iucn_status")
    search_fields = ("common_name", "scientific_name")


@admin.register(Patrol)
class PatrolAdmin(admin.ModelAdmin):
    list_display = ("client_uuid", "ranger", "area", "patrol_type", "status", "started_at", "distance_m")
    list_filter = ("status", "patrol_type", "organisation")


@admin.register(TrackPoint)
class TrackPointAdmin(admin.ModelAdmin):
    list_display = ("patrol_id", "recorded_at", "lat", "lon", "cell")


@admin.register(Observation)
class ObservationAdmin(admin.ModelAdmin):
    list_display = ("client_uuid", "category", "subtype", "observer", "area", "cell", "recorded_at", "alert_manager")
    list_filter = ("category", "severity", "organisation")


@admin.register(Media)
class MediaAdmin(admin.ModelAdmin):
    list_display = ("id", "observation", "kind", "content_type", "size_bytes")


@admin.register(SafetyAlert)
class SafetyAlertAdmin(admin.ModelAdmin):
    list_display = ("client_uuid", "ranger", "kind", "status", "started_at", "resolved_at")
    list_filter = ("status", "kind", "organisation")


@admin.register(PositionPing)
class PositionPingAdmin(admin.ModelAdmin):
    list_display = ("ranger", "recorded_at", "lat", "lon", "battery_pct")
