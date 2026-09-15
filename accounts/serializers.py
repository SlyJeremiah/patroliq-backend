from __future__ import annotations

from rest_framework import serializers

from areas.models import ApuBase, Area, Team
from core.permissions import ORG_ROLES, RANGER
from core.tenancy import TenantPKField, request_org
from core.validation import StrictModelSerializer, StrictSerializer

from .licensing import effective_status
from .models import MODULE_CHOICES, Licence, Organisation, User


class OrganisationSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()

    class Meta:
        model = Organisation
        fields = ["id", "name", "code", "country", "status", "created_at"]

    def get_status(self, obj):
        return effective_status(obj)


class LicenceSerializer(serializers.ModelSerializer):
    organisation_id = serializers.UUIDField(read_only=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = Licence
        fields = ["organisation_id", "plan", "max_rangers", "max_managers", "max_areas", "modules", "starts_at",
                  "expires_at", "grace_days", "status"]

    def get_status(self, obj):
        return effective_status(obj.organisation)


class LicenceWriteSerializer(StrictModelSerializer):
    modules = serializers.ListField(child=serializers.ChoiceField(choices=MODULE_CHOICES), allow_empty=True)
    status = serializers.ChoiceField(choices=["active", "suspended"], required=False)

    class Meta:
        model = Licence
        fields = ["plan", "max_rangers", "max_managers", "max_areas", "modules", "starts_at", "expires_at",
                  "grace_days", "status"]

    def validate(self, attrs):
        starts = attrs.get("starts_at", getattr(self.instance, "starts_at", None))
        expires = attrs.get("expires_at", getattr(self.instance, "expires_at", None))
        if starts and expires and expires <= starts:
            raise serializers.ValidationError({"expires_at": ["Must be after starts_at."]})
        if "modules" in attrs:
            attrs["modules"] = sorted(set(attrs["modules"]))
        return attrs


class UserSerializer(serializers.ModelSerializer):
    organisation_id = serializers.UUIDField(read_only=True)
    area_ids = serializers.SerializerMethodField()
    apu_base_id = serializers.UUIDField(read_only=True)
    team_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = User
        fields = ["id", "organisation_id", "employee_id", "email", "full_name", "role", "phone", "language",
                  "is_active", "area_ids", "apu_base_id", "team_id"]

    def get_area_ids(self, obj):
        return [str(pk) for pk in obj.areas.values_list("pk", flat=True)]


def auth_payload(user, token_key: str | None = None) -> dict:
    org = user.organisation
    licence = getattr(org, "licence", None) if org else None
    payload = {
        "user": UserSerializer(user).data,
        "organisation": OrganisationSerializer(org).data if org else None,
        "licence": LicenceSerializer(licence).data if licence else None,
    }
    if token_key is not None:
        payload = {"token": token_key, **payload}
    return payload


class LoginSerializer(StrictSerializer):
    organisation_code = serializers.CharField(required=False, max_length=32)
    employee_id = serializers.CharField(required=False, max_length=64)
    email = serializers.EmailField(required=False)
    password = serializers.CharField(max_length=256, trim_whitespace=False)
    device_id = serializers.CharField(required=False, allow_blank=True, max_length=128)
    totp = serializers.CharField(required=False, allow_blank=True, max_length=10)

    def validate(self, attrs):
        if attrs.get("email"):
            if attrs.get("organisation_code") or attrs.get("employee_id"):
                raise serializers.ValidationError("Use either email or organisation_code + employee_id, not both.")
        elif not (attrs.get("organisation_code") and attrs.get("employee_id")):
            raise serializers.ValidationError("Provide organisation_code + employee_id (rangers) or email (web).")
        return attrs


class PasswordChangeSerializer(StrictSerializer):
    current_password = serializers.CharField(trim_whitespace=False)
    new_password = serializers.CharField(trim_whitespace=False, min_length=8, max_length=256)


class UserWriteSerializer(StrictModelSerializer):
    role = serializers.ChoiceField(choices=sorted(ORG_ROLES))
    area_ids = TenantPKField(model=Area, source="areas", many=True, required=False)
    apu_base_id = TenantPKField(model=ApuBase, source="apu_base", required=False, allow_null=True)
    team_id = TenantPKField(model=Team, source="team", required=False, allow_null=True)
    email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)
    employee_id = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=64)

    class Meta:
        model = User
        fields = ["employee_id", "email", "full_name", "role", "phone", "language", "is_active", "area_ids",
                  "apu_base_id", "team_id"]

    def validate(self, attrs):
        org = request_org(self.context)
        inst = self.instance
        role = attrs.get("role", getattr(inst, "role", None))
        employee_id = (attrs.get("employee_id", getattr(inst, "employee_id", None)) or "").strip() or None
        email = (attrs.get("email", getattr(inst, "email", None)) or "").strip().lower() or None
        if "employee_id" in attrs:
            attrs["employee_id"] = employee_id
        if "email" in attrs:
            attrs["email"] = email
        if role == RANGER and not employee_id:
            raise serializers.ValidationError({"employee_id": ["Required for rangers (used at sign-in)."]})
        if role != RANGER and not email:
            raise serializers.ValidationError({"email": ["Required for web users (used at sign-in)."]})
        if employee_id:
            clash = User.objects.filter(organisation=org, employee_id__iexact=employee_id)
            if inst:
                clash = clash.exclude(pk=inst.pk)
            if clash.exists():
                raise serializers.ValidationError({"employee_id": ["Already used in this organisation."]})
        if email:
            clash = User.objects.filter(email__iexact=email)
            if inst:
                clash = clash.exclude(pk=inst.pk)
            if clash.exists():
                raise serializers.ValidationError({"email": ["Already in use."]})
        return attrs
