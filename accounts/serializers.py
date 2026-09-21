from __future__ import annotations

import re

from django.utils import timezone
from rest_framework import serializers

from areas.models import ApuBase, Area, Team
from core.permissions import ORG_ROLES, RANGER
from core.tenancy import TenantPKField, request_org
from core.validation import SanitizedCharField, StrictModelSerializer, StrictSerializer

from .licensing import effective_status
from .models import MODULE_CHOICES, PERSONAL_FIELDS, Licence, Organisation, User

NATIONAL_ID_RE = re.compile(r"^[A-Z0-9 \-]+$")
MIN_AGE_YEARS = 16


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


class UserDetailSerializer(UserSerializer):
    """
    :class:`UserSerializer` **plus the personal details** (spec v1.5 §B2).

    Personal data. Use it ONLY for ``users/`` responses — never for ``auth/login``, ``me/``,
    ``sync/bootstrap``, ``rangers/`` list, alerts, reports or the platform-admin views, all of which
    keep using the plain :class:`UserSerializer`.
    """

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + PERSONAL_FIELDS


def personal_profile(user: User) -> dict:
    """The ``profile`` object of ``rangers/{id}/`` (spec v1.5 §B2) — dates as ISO strings or null."""
    out = {}
    for name in PERSONAL_FIELDS:
        value = getattr(user, name)
        out[name] = value.isoformat() if hasattr(value, "isoformat") else value
    return out


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


def _optional_text(max_length: int):
    return SanitizedCharField(max_length=max_length, required=False, allow_null=True, allow_blank=True)


class UserWriteSerializer(StrictModelSerializer):
    """
    ``users/`` write payload — org_admin only (enforced by the view's ``roles_allowed(write=ADMINS)``).

    ``full_name`` is optional when ``first_name`` **and** ``surname`` are supplied; the server then
    derives it as ``"{first_name} {surname}"`` (spec v1.5 §B1).
    """

    role = serializers.ChoiceField(choices=sorted(ORG_ROLES))
    area_ids = TenantPKField(model=Area, source="areas", many=True, required=False)
    apu_base_id = TenantPKField(model=ApuBase, source="apu_base", required=False, allow_null=True)
    team_id = TenantPKField(model=Team, source="team", required=False, allow_null=True)
    email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)
    employee_id = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=64)
    full_name = SanitizedCharField(max_length=200, required=False, allow_blank=True)
    first_name = _optional_text(100)
    surname = _optional_text(100)
    national_id = _optional_text(32)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    home_address = _optional_text(300)
    next_of_kin_name = _optional_text(200)
    next_of_kin_relationship = _optional_text(60)
    next_of_kin_phone = _optional_text(32)
    next_of_kin_address = _optional_text(300)
    date_joined_org = serializers.DateField(required=False, allow_null=True)
    rank = _optional_text(60)
    post = _optional_text(100)
    certificates = _optional_text(1000)

    class Meta:
        model = User
        fields = ["employee_id", "email", "full_name", "role", "phone", "language", "is_active", "area_ids",
                  "apu_base_id", "team_id"] + PERSONAL_FIELDS

    def validate_phone(self, value):
        """Stored in E.164 (spec v1.6 §3): ``0771234567`` -> ``+263771234567``; blank clears it."""
        from notify.phones import normalise_phone

        phone = normalise_phone(value)
        if phone is None:
            raise serializers.ValidationError(
                "Enter a valid phone number including the country code, e.g. +263 77 123 4567.")
        return phone

    def validate_national_id(self, value):
        value = (value or "").strip().upper()
        if value and not NATIONAL_ID_RE.match(value):
            raise serializers.ValidationError("Only letters, digits, spaces and '-' are allowed.")
        return value

    def validate_date_of_birth(self, value):
        if value is None:
            return value
        today = timezone.localdate()
        if value >= today:
            raise serializers.ValidationError("Must be in the past.")
        age = today.year - value.year - ((today.month, today.day) < (value.month, value.day))
        if age < MIN_AGE_YEARS:
            raise serializers.ValidationError(f"The person must be at least {MIN_AGE_YEARS} years old.")
        return value

    def validate_date_joined_org(self, value):
        if value is not None and value > timezone.localdate():
            raise serializers.ValidationError("Must not be in the future.")
        return value

    def validate(self, attrs):
        org = request_org(self.context)
        inst = self.instance
        for name in PERSONAL_FIELDS:
            if name in attrs and attrs[name] is None and not name.startswith("date"):
                attrs[name] = ""
        # full_name is derived from first_name + surname whenever the client supplies both names and
        # leaves full_name out of the payload — on create and on update (spec v1.5 §B1).
        supplied_names = "first_name" in attrs and "surname" in attrs
        first = (attrs.get("first_name", getattr(inst, "first_name", "")) or "").strip()
        surname = (attrs.get("surname", getattr(inst, "surname", "")) or "").strip()
        full_name = (attrs.get("full_name") or "").strip()
        if not full_name and first and surname and (supplied_names or inst is None):
            full_name = f"{first} {surname}"
        if not full_name:
            full_name = (getattr(inst, "full_name", "") or "").strip()
        if not full_name:
            raise serializers.ValidationError(
                {"full_name": ["This field is required unless first_name and surname are given."]})
        attrs["full_name"] = full_name
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
