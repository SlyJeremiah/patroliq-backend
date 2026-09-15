"""
zrGISsolutions platform endpoints (spec §5 "Platform"). Platform admins provision organisations
and licences and see *aggregate usage only* — never patrols, observations or positions.
"""
from __future__ import annotations

import secrets

from django.db import transaction
from django.db.models import Max
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.licensing import effective_status, seat_usage
from accounts.models import Licence, Organisation, User
from accounts.security import new_totp_secret, totp_uri
from accounts.serializers import LicenceSerializer, LicenceWriteSerializer, OrganisationSerializer, UserSerializer
from areas.models import Area
from audit.utils import audit
from core.db import tenant_context
from core.exceptions import ApiError
from core.permissions import ORG_ADMIN, IsPlatformAdmin
from core.validation import SanitizedCharField, StrictSerializer


class OrgAdminInSerializer(StrictSerializer):
    full_name = SanitizedCharField(max_length=200, allow_blank=False)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    employee_id = serializers.CharField(max_length=64, required=False, allow_blank=True)


class OrganisationCreateSerializer(StrictSerializer):
    name = SanitizedCharField(max_length=200, allow_blank=False)
    code = serializers.RegexField(r"^[A-Za-z0-9_-]{2,32}$", max_length=32)
    country = SanitizedCharField(max_length=64, required=False)
    deployment = serializers.ChoiceField(choices=["shared", "dedicated"], required=False)
    licence = LicenceWriteSerializer()
    admin = OrgAdminInSerializer()

    def validate_code(self, value):
        if Organisation.objects.filter(code__iexact=value).exists():
            raise serializers.ValidationError("Organisation code already in use.")
        return value.upper()

    def validate(self, attrs):
        if User.objects.filter(email__iexact=attrs["admin"]["email"]).exists():
            raise serializers.ValidationError({"admin": {"email": ["Already in use."]}})
        return attrs


class OrganisationUpdateSerializer(StrictSerializer):
    name = SanitizedCharField(max_length=200, allow_blank=False, required=False)
    status = serializers.ChoiceField(choices=["active", "grace", "suspended"], required=False)
    country = SanitizedCharField(max_length=64, required=False)
    deployment = serializers.ChoiceField(choices=["shared", "dedicated"], required=False)


def _org_body(org):
    licence = getattr(org, "licence", None)
    return {**OrganisationSerializer(org).data, "deployment": org.deployment,
            "licence": LicenceSerializer(licence).data if licence else None}


def _get_org(pk) -> Organisation:
    org = Organisation.objects.select_related("licence").filter(pk=pk).first()
    if org is None:
        raise ApiError(404, "not_found", "Organisation not found.")
    return org


class OrganisationListCreateView(APIView):
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        return Response([_org_body(o) for o in Organisation.objects.select_related("licence")])

    def post(self, request):
        s = OrganisationCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        temp_password = secrets.token_urlsafe(9)
        with transaction.atomic():
            org = Organisation.objects.create(name=d["name"], code=d["code"], country=d.get("country", ""),
                                              deployment=d.get("deployment", "shared"))
            licence = Licence.objects.create(organisation=org, **d["licence"])
            admin_in = d["admin"]
            admin = User(organisation=org, role=ORG_ADMIN, full_name=admin_in["full_name"], email=admin_in["email"],
                         phone=admin_in.get("phone", ""), employee_id=admin_in.get("employee_id") or None,
                         must_change_password=True, totp_secret=new_totp_secret())
            admin.set_password(temp_password)
            admin.save()
        audit(request, "platform.organisation_create", target=org, organisation_id=org.pk,
              detail={"code": org.code, "plan": licence.plan})
        return Response({
            "organisation": _org_body(org),
            "admin_user": UserSerializer(admin).data,
            "temporary_password": temp_password,
            "totp_secret": admin.totp_secret,
            "totp_uri": totp_uri(admin),
        }, status=status.HTTP_201_CREATED)


class OrganisationDetailView(APIView):
    permission_classes = [IsPlatformAdmin]

    def get(self, request, pk):
        return Response(_org_body(_get_org(pk)))

    def patch(self, request, pk):
        org = _get_org(pk)
        s = OrganisationUpdateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        for k, v in s.validated_data.items():
            setattr(org, k, v)
        org.save()
        audit(request, "platform.organisation_update", target=org, organisation_id=org.pk,
              detail={k: str(v) for k, v in s.validated_data.items()})
        return Response(_org_body(org))


class LicenceView(APIView):
    permission_classes = [IsPlatformAdmin]

    def get(self, request, pk):
        org = _get_org(pk)
        if not hasattr(org, "licence"):
            raise ApiError(404, "not_found", "Organisation has no licence.")
        return Response(LicenceSerializer(org.licence).data)

    def put(self, request, pk):
        org = _get_org(pk)
        instance = getattr(org, "licence", None)
        s = LicenceWriteSerializer(instance, data=request.data, partial=instance is not None)
        s.is_valid(raise_exception=True)
        licence = s.save(organisation=org) if instance is None else s.save()
        audit(request, "platform.licence_update", target=licence, organisation_id=org.pk,
              detail={k: str(v) for k, v in s.validated_data.items()})
        org.refresh_from_db()
        return Response(LicenceSerializer(licence).data)


class UsageView(APIView):
    """Seat/area counts and last sync — aggregates only, no operational rows."""

    permission_classes = [IsPlatformAdmin]

    def get(self, request, pk):
        org = _get_org(pk)
        licence = getattr(org, "licence", None)
        seats = seat_usage(org)
        with tenant_context(org.pk):
            areas = Area.objects.for_org(org)
            area_counts = {"total": areas.count(), "active": areas.filter(status="active").count(),
                           "archived": areas.filter(status="archived").count()}
        last_sync = User.objects.filter(organisation=org).aggregate(v=Max("last_sync_at"))["v"]
        return Response({
            "organisation_id": str(org.pk),
            "status": effective_status(org),
            "seats": {"rangers_used": seats["rangers"], "max_rangers": licence.max_rangers if licence else 0,
                      "managers_used": seats["managers"], "max_managers": licence.max_managers if licence else 0},
            "areas": {**area_counts, "max_areas": licence.max_areas if licence else 0},
            "last_sync_at": serializers.DateTimeField().to_representation(last_sync) if last_sync else None,
        })
