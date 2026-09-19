from __future__ import annotations

import binascii
import os
import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import TimeStampedModel, UUIDModel
from core.roles import PLATFORM_ADMIN, RANGER, ROLE_CHOICES

MODULE_CHOICES = ["ai_risk", "species_id", "voice", "collars", "reports", "grts"]

#: Personal-detail fields (spec v1.5 §B). The ``profile`` object of ``rangers/{id}/`` is exactly
#: these keys, in this order.
PERSONAL_FIELDS = [
    "first_name", "surname", "national_id", "date_of_birth", "home_address", "next_of_kin_name",
    "next_of_kin_relationship", "next_of_kin_phone", "next_of_kin_address", "date_joined_org",
    "rank", "post", "certificates",
]


class Organisation(UUIDModel, TimeStampedModel):
    STATUS_CHOICES = [("active", "Active"), ("grace", "Grace"), ("suspended", "Suspended")]
    DEPLOYMENT_CHOICES = [("shared", "Shared cloud"), ("dedicated", "Dedicated instance")]

    name = models.CharField(max_length=200)
    code = models.CharField(max_length=32, unique=True, help_text="Short upper-case code used at ranger sign-in")
    country = models.CharField(max_length=64, blank=True, default="")
    # Administrative status set by zrGISsolutions. The *effective* status also depends on the
    # licence dates (see accounts.licensing.effective_status) and is what the API returns.
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="active")
    deployment = models.CharField(max_length=16, choices=DEPLOYMENT_CHOICES, default="shared")

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        self.code = (self.code or "").strip().upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.code})"


class Licence(UUIDModel, TimeStampedModel):
    PLAN_CHOICES = [("pilot", "Pilot"), ("standard", "Standard"), ("enterprise", "Enterprise")]
    STATUS_CHOICES = Organisation.STATUS_CHOICES

    organisation = models.OneToOneField(Organisation, on_delete=models.CASCADE, related_name="licence")
    plan = models.CharField(max_length=16, choices=PLAN_CHOICES, default="pilot")
    max_rangers = models.PositiveIntegerField(default=10)
    max_managers = models.PositiveIntegerField(default=3)
    max_areas = models.PositiveIntegerField(default=1)
    modules = models.JSONField(default=list, blank=True)
    starts_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    grace_days = models.PositiveIntegerField(default=14)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="active")

    def __str__(self):
        return f"{self.organisation.code} {self.plan}"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email=None, password=None, **extra):
        if email:
            email = self.normalize_email(email).lower()
        user = self.model(email=email or None, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password, **extra):
        extra.setdefault("role", PLATFORM_ADMIN)
        extra.setdefault("full_name", "Platform admin")
        extra["is_staff"] = True
        extra["is_superuser"] = True
        return self.create_user(email=email, password=password, **extra)


class User(UUIDModel, AbstractBaseUser, PermissionsMixin):
    LANGUAGE_CHOICES = [("en", "English"), ("sn", "Shona"), ("nd", "Ndebele")]

    organisation = models.ForeignKey(
        Organisation, null=True, blank=True, on_delete=models.CASCADE, related_name="users",
        help_text="Null only for zrGISsolutions platform admins",
    )
    employee_id = models.CharField(max_length=64, null=True, blank=True)
    email = models.EmailField(unique=True, null=True, blank=True)
    full_name = models.CharField(max_length=200)
    role = models.CharField(max_length=32, choices=ROLE_CHOICES, default=RANGER)
    phone = models.CharField(max_length=32, blank=True, default="")
    language = models.CharField(max_length=2, choices=LANGUAGE_CHOICES, default="en")
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    areas = models.ManyToManyField("areas.Area", blank=True, related_name="users")
    apu_base = models.ForeignKey("areas.ApuBase", null=True, blank=True, on_delete=models.SET_NULL, related_name="users")
    team = models.ForeignKey("areas.Team", null=True, blank=True, on_delete=models.SET_NULL, related_name="members")

    # Personal details (spec v1.5 §B). Personal data: exposed ONLY through ``users/`` and the
    # ``profile`` object of ``rangers/{id}/`` — never in auth/me/bootstrap/rangers-list/alerts/reports.
    first_name = models.CharField(max_length=100, blank=True, default="")
    surname = models.CharField(max_length=100, blank=True, default="")
    national_id = models.CharField(max_length=32, blank=True, default="")
    date_of_birth = models.DateField(null=True, blank=True)
    home_address = models.CharField(max_length=300, blank=True, default="")
    next_of_kin_name = models.CharField(max_length=200, blank=True, default="")
    next_of_kin_relationship = models.CharField(max_length=60, blank=True, default="")
    next_of_kin_phone = models.CharField(max_length=32, blank=True, default="")
    next_of_kin_address = models.CharField(max_length=300, blank=True, default="")
    date_joined_org = models.DateField(null=True, blank=True)
    rank = models.CharField(max_length=60, blank=True, default="")
    post = models.CharField(max_length=100, blank=True, default="")
    certificates = models.TextField(max_length=1000, blank=True, default="")

    # TOTP (RFC 6238). The secret is shown once at creation/seed; stored server-side only.
    totp_secret = models.CharField(max_length=64, blank=True, default="")
    totp_last_step = models.BigIntegerField(null=True, blank=True, help_text="Replay protection")
    must_change_password = models.BooleanField(default=False)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        ordering = ["full_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organisation", "employee_id"],
                condition=Q(employee_id__isnull=False),
                name="uniq_employee_id_per_org",
            )
        ]

    def save(self, *args, **kwargs):
        if self.email == "":
            self.email = None
        if self.email:
            self.email = self.email.lower()
        if self.employee_id == "":
            self.employee_id = None
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.full_name} ({self.employee_id or self.email})"


def _new_token_key() -> str:
    return binascii.hexlify(os.urandom(20)).decode()


class AuthToken(models.Model):
    """
    Opaque API token (``Authorization: Token <key>``). One row per signed-in device/browser so a
    ranger's phone and a manager's browser sessions are independent; logout deletes only its row.
    """

    key = models.CharField(max_length=40, primary_key=True, default=_new_token_key)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="auth_tokens")
    device_id = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"token for {self.user_id}"


class LoginLockout(models.Model):
    """
    Failed-login counter keyed by the *identifier* the client typed (not by user row), so unknown
    accounts are throttled exactly like real ones (no account enumeration via lockout behaviour).
    """

    identifier = models.CharField(max_length=320, unique=True)
    failures = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.identifier


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()
