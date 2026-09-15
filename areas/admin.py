from django.contrib import admin

from .models import ApuBase, Area, Assignment, FeatureLayer, GrtsCell, RiskScore, Sector, Team


@admin.register(Area)
class AreaAdmin(admin.ModelAdmin):
    list_display = ("name", "organisation", "area_type", "status", "area_km2", "grid_cell_size_m", "updated_at")
    list_filter = ("status", "area_type", "organisation")
    search_fields = ("name", "client_name")


@admin.register(ApuBase)
class ApuBaseAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "area", "call_sign")
    list_filter = ("area",)


@admin.register(Sector)
class SectorAdmin(admin.ModelAdmin):
    list_display = ("name", "area", "apu_base")


@admin.register(GrtsCell)
class GrtsCellAdmin(admin.ModelAdmin):
    list_display = ("label", "area", "sector", "grts_order")
    list_filter = ("area",)
    search_fields = ("label",)


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("name", "area", "apu_base", "leader")


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = ("team", "area", "date", "visit_target")
    list_filter = ("date", "area")
    filter_horizontal = ("cells",)


@admin.register(RiskScore)
class RiskScoreAdmin(admin.ModelAdmin):
    list_display = ("cell", "date", "score", "level")
    list_filter = ("date", "level", "area")


@admin.register(FeatureLayer)
class FeatureLayerAdmin(admin.ModelAdmin):
    list_display = ("area", "kind", "updated_at")
