from __future__ import annotations

import zoneinfo

from rest_framework import serializers

from accounts.models import User
from core.permissions import ORG_ROLES
from core.tenancy import TenantPKField
from core.validation import PointField, SanitizedCharField, StrictModelSerializer, StrictSerializer

from .models import ApuBase, Area, Assignment, GrtsCell, RiskScore, Sector, Team


class AreaSerializer(serializers.ModelSerializer):
    organisation_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Area
        fields = ["id", "organisation_id", "name", "client_name", "area_type", "boundary", "boundary_source",
                  "area_km2", "timezone", "grid_cell_size_m", "status", "updated_at"]


class AreaWriteSerializer(StrictModelSerializer):
    # Status: only draft/archived via PATCH; ``active`` goes through areas/{id}/activate/ checks.
    status = serializers.ChoiceField(choices=["draft", "archived"], required=False)
    name = SanitizedCharField(max_length=200, allow_blank=False)
    client_name = SanitizedCharField(max_length=200, required=False)
    grid_cell_size_m = serializers.IntegerField(min_value=100, max_value=20000, required=False)

    # Tolerated read-only echoes from clients sending a full object back.
    id = serializers.UUIDField(read_only=True)
    organisation_id = serializers.UUIDField(read_only=True)
    boundary = serializers.JSONField(read_only=True)
    boundary_source = serializers.CharField(read_only=True)
    area_km2 = serializers.FloatField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    class Meta:
        model = Area
        fields = ["id", "organisation_id", "name", "client_name", "area_type", "boundary", "boundary_source",
                  "area_km2", "timezone", "grid_cell_size_m", "status", "updated_at"]

    def validate_timezone(self, value):
        try:
            zoneinfo.ZoneInfo(value)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            raise serializers.ValidationError("Unknown IANA timezone.")
        return value


class BoundarySerializer(StrictSerializer):
    boundary = serializers.JSONField()


class GridGenerateSerializer(StrictSerializer):
    cell_size_m = serializers.IntegerField(min_value=100, max_value=20000, required=False)
    force = serializers.BooleanField(required=False, default=False)
    seed = serializers.CharField(required=False, max_length=64)


class ApuBaseSerializer(StrictModelSerializer):
    area_id = TenantPKField(model=Area, source="area")
    location = PointField()
    name = SanitizedCharField(max_length=200, allow_blank=False)
    code = SanitizedCharField(max_length=32, allow_blank=False)
    call_sign = SanitizedCharField(max_length=64, required=False)
    id = serializers.UUIDField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    class Meta:
        model = ApuBase
        fields = ["id", "area_id", "name", "code", "call_sign", "location", "updated_at"]

    def validate(self, attrs):
        area = attrs.get("area", getattr(self.instance, "area", None))
        code = attrs.get("code", getattr(self.instance, "code", None))
        clash = ApuBase.objects.filter(area=area, code__iexact=code)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError({"code": ["A base with this code already exists in the area."]})
        return attrs


class SectorSerializer(serializers.ModelSerializer):
    area_id = serializers.UUIDField(read_only=True)
    apu_base_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Sector
        fields = ["id", "area_id", "name", "apu_base_id", "updated_at"]


class GrtsCellSerializer(serializers.ModelSerializer):
    area_id = serializers.UUIDField(read_only=True)
    sector_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = GrtsCell
        fields = ["id", "area_id", "sector_id", "label", "grts_order", "geometry", "centroid", "updated_at"]


class TeamSerializer(StrictModelSerializer):
    id = serializers.UUIDField(read_only=True)
    organisation_id = serializers.UUIDField(read_only=True)
    area_id = TenantPKField(model=Area, source="area")
    apu_base_id = TenantPKField(model=ApuBase, source="apu_base", required=False, allow_null=True)
    leader_id = TenantPKField(model=User, source="leader", required=False, allow_null=True)
    member_ids = TenantPKField(model=User, many=True, required=False, source="members")
    name = SanitizedCharField(max_length=200, allow_blank=False)
    updated_at = serializers.DateTimeField(read_only=True)

    class Meta:
        model = Team
        fields = ["id", "organisation_id", "area_id", "apu_base_id", "name", "leader_id", "member_ids", "updated_at"]

    def validate(self, attrs):
        area = attrs.get("area", getattr(self.instance, "area", None))
        base = attrs.get("apu_base", getattr(self.instance, "apu_base", None))
        if base is not None and area is not None and base.area_id != area.pk:
            raise serializers.ValidationError({"apu_base_id": ["Base does not belong to this area."]})
        for m in attrs.get("members", []):
            if m.role not in ORG_ROLES or not m.is_active:
                raise serializers.ValidationError({"member_ids": [f"{m.pk} is not an active organisation user."]})
        return attrs

    def _save_members(self, team, members):
        if members is None:
            return
        User.objects.filter(team=team).exclude(pk__in=[m.pk for m in members]).update(team=None)
        User.objects.filter(pk__in=[m.pk for m in members]).update(team=team, apu_base=team.apu_base)

    def create(self, validated_data):
        members = validated_data.pop("members", None)
        team = Team.objects.create(**validated_data)
        self._save_members(team, members)
        return team

    def update(self, instance, validated_data):
        members = validated_data.pop("members", None)
        for k, v in validated_data.items():
            setattr(instance, k, v)
        instance.save()
        self._save_members(instance, members)
        return instance


class AssignmentSerializer(StrictModelSerializer):
    id = serializers.UUIDField(read_only=True)
    team_id = TenantPKField(model=Team, source="team")
    area_id = TenantPKField(model=Area, source="area")
    cell_ids = TenantPKField(model=GrtsCell, many=True, source="cells", required=False)
    notes = SanitizedCharField(max_length=1000, required=False)
    visit_target = serializers.IntegerField(min_value=1, max_value=1000, required=False)
    updated_at = serializers.DateTimeField(read_only=True)

    class Meta:
        model = Assignment
        fields = ["id", "team_id", "area_id", "date", "cell_ids", "visit_target", "notes", "updated_at"]

    def validate(self, attrs):
        area = attrs.get("area", getattr(self.instance, "area", None))
        for cell in attrs.get("cells", []):
            if cell.area_id != area.pk:
                raise serializers.ValidationError({"cell_ids": [f"Cell {cell.label} is not in this area."]})
        return attrs


class RiskScoreSerializer(serializers.ModelSerializer):
    cell_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = RiskScore
        fields = ["cell_id", "date", "score", "level", "factors"]
