from __future__ import annotations

from django.db import transaction
from django.db.models import Count, IntegerField, OuterRef, ProtectedError, Q, Subquery
from django.db.models.functions import Coalesce
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

import geo
from accounts.licensing import check_area_available
from audit.utils import audit
from core.exceptions import ApiError
from core.models import Tombstone
from core.permissions import ADMINS, MANAGERS, ORG_ROLES, RANGER, require_module, roles_allowed
from core.tenancy import TenantScopedMixin
from core.utils import query_date, query_uuid
from core.validation import reject_unexpected

from . import services
from .models import ApuBase, Area, Assignment, FeatureLayer, GrtsCell, Sector, Team
from .serializers import (
    ApuBaseSerializer,
    AreaSetupSerializer as AreaSerializer,
    AreaSetupSerializer,
    AreaWriteSerializer,
    AssignmentSerializer,
    BoundarySerializer,
    GridGenerateSerializer,
    GrtsCellSerializer,
    SectorSerializer,
    TeamSerializer,
)

MAX_BOUNDARY_UPLOAD = 50 * 1024 * 1024


def _count_subquery(model):
    return Coalesce(Subquery(
        model.objects.filter(area_id=OuterRef("pk")).order_by().values("area_id").annotate(n=Count("pk")).values("n")[:1],
        output_field=IntegerField()), 0)


def ranger_area_filter(user) -> Q:
    q = Q(users=user)
    if user.team_id:
        q |= Q(teams__members=user)
    return q


class AreaViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    queryset = Area.objects.all()
    permission_classes = [roles_allowed(read=ORG_ROLES, write=ADMINS)]
    http_method_names = ["get", "post", "patch", "put", "delete", "head", "options"]

    def get_serializer_class(self):
        return AreaSetupSerializer if self.request.method in ("GET", "HEAD", "OPTIONS") else AreaWriteSerializer

    def scope_queryset(self, qs):
        if self.request.user.role == RANGER:
            qs = qs.filter(pk__in=Area.objects.filter(ranger_area_filter(self.request.user)).values("pk"))
        if self.request.query_params.get("status"):
            qs = qs.filter(status=self.request.query_params["status"])
        if self.request.method in ("GET", "HEAD") and getattr(self, "action", None) in ("list", "retrieve"):
            qs = qs.annotate(**{f"n_{name}": _count_subquery(model) for name, model in (
                ("apu_bases", ApuBase), ("cells", GrtsCell), ("teams", Team), ("sectors", Sector))})
        return qs

    def update(self, request, *args, **kwargs):
        if request.method == "PUT":
            raise ApiError(405, "method_not_allowed", "Use PATCH to update an area.")
        return super().update(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        s = AreaWriteSerializer(data=request.data, context=self.get_serializer_context())
        s.is_valid(raise_exception=True)
        with transaction.atomic():
            check_area_available(request.user.organisation)
            area = s.save(organisation=request.user.organisation)
        audit(request, "area.create", target=area)
        return Response(AreaSerializer(area).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        area = self.get_object()
        s = AreaWriteSerializer(area, data=request.data, partial=True, context=self.get_serializer_context())
        s.is_valid(raise_exception=True)
        if s.validated_data.get("status") == "draft" and area.status == "archived":
            check_area_available(request.user.organisation)
        area = s.save()
        audit(request, "area.update", target=area, detail={"fields": sorted(request.data.keys())})
        return Response(AreaSerializer(area).data)

    def destroy(self, request, *args, **kwargs):
        area = self.get_object()
        area_id = area.pk
        try:
            with transaction.atomic():
                area.delete()
        except ProtectedError:
            raise ApiError(409, "area_in_use", "The area has patrol data; archive it instead (PATCH status=archived).")
        Tombstone.record(request.user.organisation_id, "areas", [area_id], area_id)
        audit(request, "area.delete", organisation_id=request.user.organisation_id, target_type="areas.area",
              target_id=area_id)
        return Response(status=status.HTTP_204_NO_CONTENT)

    # --- boundary ------------------------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="boundary/import")
    def boundary_import(self, request, pk=None):
        area = self.get_object()
        reject_unexpected(request.data, {"file", "feature_index", "dissolve"})
        upload = request.FILES.get("file")
        if upload is None:
            raise ApiError(400, "validation_error", "A boundary file is required.", fields={"file": ["This field is required."]})
        if upload.size > MAX_BOUNDARY_UPLOAD:
            raise ApiError(413, "file_too_large", "Boundary file exceeds 50 MB.")
        feature_index = request.data.get("feature_index")
        if feature_index not in (None, ""):
            try:
                feature_index = int(feature_index)
            except (TypeError, ValueError):
                raise ApiError(400, "validation_error", "feature_index must be an integer.",
                               fields={"feature_index": ["A valid integer is required."]})
        else:
            feature_index = None
        dissolve = str(request.data.get("dissolve", "")).lower() in {"true", "1", "yes"}
        try:
            result = geo.read_boundary_file(upload.name, upload.read(), feature_index=feature_index, dissolve=dissolve)
        except geo.GeoError as exc:
            raise services.geo_api_error(exc)
        services.set_boundary(area, result.geometry, result.source)
        audit(request, "area.boundary_import", target=area, detail={
            "source": result.source, "features_found": result.features_found, "crs_detected": result.crs_detected,
            "feature_index": feature_index, "dissolve": dissolve, "area_km2": area.area_km2})
        return Response({"area": AreaSerializer(area).data, "features_found": result.features_found,
                         "crs_detected": result.crs_detected})

    @action(detail=True, methods=["put"], url_path="boundary")
    def boundary(self, request, pk=None):
        area = self.get_object()
        s = BoundarySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        raw = s.validated_data["boundary"]
        if not isinstance(raw, dict) or raw.get("type") not in {"Polygon", "MultiPolygon"}:
            raise ApiError(400, "validation_error", "boundary must be a GeoJSON Polygon or MultiPolygon.",
                           fields={"boundary": ["Expected a GeoJSON Polygon or MultiPolygon."]})
        try:
            geom = geo.normalise_boundary(geo.shape_from_geojson(raw))
        except geo.GeoError as exc:
            raise services.geo_api_error(exc)
        services.set_boundary(area, geom, "drawn")
        audit(request, "area.boundary_drawn", target=area, detail={"area_km2": area.area_km2})
        return Response(AreaSerializer(area).data)

    # --- grid / activation ---------------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="grid/generate")
    def grid_generate(self, request, pk=None):
        area = self.get_object()
        require_module(request.user.organisation, "grts")
        s = GridGenerateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        if s.validated_data["dry_run"]:
            return Response(services.preview_grid(area, s.validated_data.get("cell_size_m"), s.validated_data.get("seed")))
        result = services.generate_grid(area, s.validated_data.get("cell_size_m"), s.validated_data["force"],
                                        s.validated_data.get("seed"))
        audit(request, "area.grid_generate", target=area, detail={**result, "cell_size_m": area.grid_cell_size_m})
        return Response(result)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        area = self.get_object()
        reject_unexpected(request.data, set())
        problems = services.activation_problems(area)
        if problems:
            raise ApiError(400, "area_not_ready", "The area needs a boundary, at least one APU base and a grid.",
                           fields=problems)
        area.status = "active"
        area.save(update_fields=["status", "updated_at"])
        audit(request, "area.activate", target=area)
        return Response(AreaSerializer(area).data)

    @action(detail=True, methods=["get"], permission_classes=[roles_allowed(read=ORG_ROLES)])
    def cells(self, request, pk=None):
        """GeoJSON FeatureCollection of the area's cells (spec §7; properties id, label, grts_order, sector_id)."""
        area = self.get_object()
        return Response(services.cells_feature_collection(GrtsCell.objects.filter(area=area).order_by("grts_order")))

    @action(detail=True, methods=["get"], permission_classes=[roles_allowed(read=ORG_ROLES)])
    def sectors(self, request, pk=None):
        area = self.get_object()
        return Response(SectorSerializer(Sector.objects.filter(area=area), many=True).data)

    @action(detail=True, methods=["put", "delete"], url_path=r"layers/(?P<kind>roads|water)")
    def layers(self, request, pk=None, kind=None):
        """Optional risk-engine layers: PUT {geometry: GeoJSON} / DELETE."""
        area = self.get_object()
        if request.method == "DELETE":
            FeatureLayer.objects.filter(area=area, kind=kind).delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        reject_unexpected(request.data, {"geometry"})
        raw = request.data.get("geometry")
        try:
            geo.shape_from_geojson(raw)
        except geo.GeoError as exc:
            raise services.geo_api_error(exc)
        FeatureLayer.objects.update_or_create(area=area, kind=kind, defaults={"organisation_id": area.organisation_id,
                                                                              "geometry": raw})
        audit(request, "area.layer_set", target=area, detail={"kind": kind})
        return Response({"area_id": str(area.pk), "kind": kind}, status=status.HTTP_200_OK)


class ApuBaseViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    queryset = ApuBase.objects.select_related("area")
    serializer_class = ApuBaseSerializer
    permission_classes = [roles_allowed(read=ORG_ROLES, write=ADMINS)]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def scope_queryset(self, qs):
        if area_id := query_uuid(self.request, "area_id"):
            qs = qs.filter(area_id=area_id)
        return qs

    def perform_create(self, serializer):
        services.ensure_point_in_area(serializer.validated_data["area"], serializer.validated_data["location"])
        base = serializer.save(organisation=self.request.user.organisation)
        audit(self.request, "apu_base.create", target=base)

    def perform_update(self, serializer):
        area = serializer.validated_data.get("area", serializer.instance.area)
        location = serializer.validated_data.get("location", serializer.instance.location)
        services.ensure_point_in_area(area, location)
        base = serializer.save()
        audit(self.request, "apu_base.update", target=base)

    def perform_destroy(self, instance):
        Tombstone.record(instance.organisation_id, "apu_bases", [instance.pk], instance.area_id)
        audit(self.request, "apu_base.delete", target=instance)
        instance.delete()


class TeamViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    queryset = Team.objects.prefetch_related("members")
    serializer_class = TeamSerializer
    permission_classes = [roles_allowed(read=ORG_ROLES, write=ADMINS)]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def scope_queryset(self, qs):
        if area_id := query_uuid(self.request, "area_id"):
            qs = qs.filter(area_id=area_id)
        return qs

    def perform_create(self, serializer):
        team = serializer.save(organisation=self.request.user.organisation)
        audit(self.request, "team.create", target=team)

    def perform_update(self, serializer):
        audit(self.request, "team.update", target=serializer.save())

    def perform_destroy(self, instance):
        audit(self.request, "team.delete", target=instance)
        instance.delete()


class AssignmentViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    queryset = Assignment.objects.prefetch_related("cells")
    serializer_class = AssignmentSerializer
    permission_classes = [roles_allowed(read=ORG_ROLES, write=MANAGERS)]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def scope_queryset(self, qs):
        user = self.request.user
        if user.role == RANGER:
            qs = qs.filter(team__members=user)
        if area_id := query_uuid(self.request, "area_id"):
            qs = qs.filter(area_id=area_id)
        if team_id := query_uuid(self.request, "team_id"):
            qs = qs.filter(team_id=team_id)
        if d := query_date(self.request, "date"):
            qs = qs.filter(date=d)
        return qs

    def perform_create(self, serializer):
        a = serializer.save(organisation=self.request.user.organisation)
        audit(self.request, "assignment.create", target=a, detail={"date": str(a.date), "cells": a.cells.count()})

    def perform_update(self, serializer):
        audit(self.request, "assignment.update", target=serializer.save())

    def perform_destroy(self, instance):
        Tombstone.record(instance.organisation_id, "assignments", [instance.pk], instance.area_id)
        audit(self.request, "assignment.delete", target=instance)
        instance.delete()
