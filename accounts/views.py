from __future__ import annotations

from django.conf import settings

import secrets

from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from audit.utils import audit
from core.exceptions import ApiError
from core.permissions import ADMINS, MANAGERS, PLATFORM_ADMIN, TOTP_ROLES, roles_allowed
from core.tenancy import TenantScopedMixin

from .licensing import check_seat_available, effective_status, seat_kind
from .models import AuthToken, Organisation, User
from .security import (
    clear_failures,
    lockout_remaining,
    new_totp_secret,
    ranger_identifier,
    register_failure,
    totp_uri,
    verify_totp,
    web_identifier,
)
from .serializers import (
    LoginSerializer,
    PasswordChangeSerializer,
    UserDetailSerializer,
    UserSerializer,
    UserWriteSerializer,
    auth_payload,
)

_DUMMY_HASH = make_password("timing-equaliser-not-a-password")


def _locked_out(seconds: int) -> ApiError:
    minutes = max(1, round(seconds / 60))
    return ApiError(429, "locked_out", f"Too many failed attempts. Try again in about {minutes} minute(s).",
                    headers={"Retry-After": str(seconds)})


class LoginView(APIView):
    """
    POST auth/login/
      rangers: {organisation_code, employee_id, password, device_id}
      web:     {email, password, totp?}
    Security: generic ``invalid_credentials`` for unknown user *or* wrong password (no enumeration),
    per-identifier lockout (5 failures -> 15 min, ``429 locked_out``) plus a per-IP throttle, TOTP
    mandatory for manager/org_admin/platform_admin whichever path they use.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        s = LoginSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        if data.get("email"):
            identifier = web_identifier(data["email"])
            user = User.objects.select_related("organisation").filter(email__iexact=data["email"].strip()).first()
        else:
            identifier = ranger_identifier(data["organisation_code"], data["employee_id"])
            org = Organisation.objects.filter(code__iexact=data["organisation_code"].strip()).first()
            user = (
                User.objects.select_related("organisation")
                .filter(organisation=org, employee_id__iexact=data["employee_id"].strip())
                .first()
                if org
                else None
            )

        remaining = lockout_remaining(identifier)
        if remaining:
            raise _locked_out(remaining)

        if user is None:
            from django.contrib.auth.hashers import check_password

            check_password(data["password"], _DUMMY_HASH)  # equalise timing
            password_ok = False
        else:
            password_ok = user.check_password(data["password"])

        if not password_ok:
            self._fail(request, identifier, user, "invalid_credentials")
            raise ApiError(401, "invalid_credentials", "Invalid credentials.")

        if not user.is_active:
            raise ApiError(403, "account_disabled", "This account has been deactivated.")

        if settings.WEB_TOTP_REQUIRED and user.role in TOTP_ROLES:
            if not data.get("totp"):
                raise ApiError(401, "totp_required", "A TOTP code from your authenticator app is required.")
            if not user.totp_secret:
                raise ApiError(403, "totp_not_enrolled", "Two-factor authentication is not set up for this account.")
            if not verify_totp(user, data["totp"]):
                self._fail(request, identifier, user, "invalid_totp")
                raise ApiError(401, "invalid_totp", "Invalid or already used TOTP code.")

        if user.role != PLATFORM_ADMIN and effective_status(user.organisation) == "suspended":
            audit(request, "auth.login_refused", target=user, actor=user, detail={"reason": "licence_suspended"})
            raise ApiError(403, "licence_suspended", "The organisation's licence is suspended. Contact your administrator.")

        clear_failures(identifier)
        token = AuthToken.objects.create(user=user, device_id=data.get("device_id", "") or "")
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        audit(request, "auth.login", target=user, actor=user, detail={"device_id": token.device_id,
                                                                     "method": "web" if data.get("email") else "ranger"})
        return Response(auth_payload(user, token.key), status=status.HTTP_200_OK)

    @staticmethod
    def _fail(request, identifier, user, reason):
        locked = register_failure(identifier)
        audit(request, "auth.login_failed", actor=user, organisation_id=getattr(user, "organisation_id", None),
              target_type="login", target_id=identifier[:64], detail={"reason": reason, "locked": bool(locked)})
        if locked:
            raise _locked_out(locked)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if isinstance(request.auth, AuthToken):
            request.auth.delete()
        audit(request, "auth.logout", target=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    """GET me/ — works for suspended organisations too, so the app can show the licence state."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(auth_payload(request.user))


class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        s = PasswordChangeSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(s.validated_data["current_password"]):
            raise ApiError(400, "invalid_credentials", "Current password is incorrect.")
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError

        try:
            validate_password(s.validated_data["new_password"], user)
        except DjangoValidationError as exc:
            raise ApiError(400, "validation_error", "Password too weak.", fields={"new_password": exc.messages})
        user.set_password(s.validated_data["new_password"])
        user.must_change_password = False
        user.save()
        AuthToken.objects.filter(user=user).exclude(pk=getattr(request.auth, "pk", None)).delete()
        audit(request, "auth.password_changed", target=user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class UserViewSet(TenantScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin,
                  mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """
    users/ — org_admin manages; managers may list. Licence seat limits -> 402.

    This is the only endpoint that returns the personal details of §B (plus the ``profile`` object of
    ``rangers/{id}/``), hence :class:`UserDetailSerializer` rather than the lean
    :class:`UserSerializer` used by auth/me/bootstrap.
    """

    queryset = User.objects.prefetch_related("areas")
    permission_classes = [roles_allowed(read=MANAGERS, write=ADMINS)]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_serializer_class(self):
        return UserDetailSerializer if self.request.method in ("GET", "HEAD", "OPTIONS") else UserWriteSerializer

    def scope_queryset(self, qs):
        p = self.request.query_params
        if p.get("role"):
            qs = qs.filter(role=p["role"])
        if p.get("is_active") in ("true", "false"):
            qs = qs.filter(is_active=p["is_active"] == "true")
        return qs

    def create(self, request, *args, **kwargs):
        s = UserWriteSerializer(data=request.data, context=self.get_serializer_context())
        s.is_valid(raise_exception=True)
        org = request.user.organisation
        with transaction.atomic():
            if s.validated_data.get("is_active", True):
                check_seat_available(org, s.validated_data["role"])
            temp_password = secrets.token_urlsafe(9)
            areas = s.validated_data.pop("areas", [])
            user = User(organisation=org, must_change_password=True, **s.validated_data)
            user.set_password(temp_password)
            if user.role in TOTP_ROLES:
                user.totp_secret = new_totp_secret()
            user.save()
            user.areas.set(areas)
        audit(request, "user.create", target=user, detail={"role": user.role})
        body = {**UserDetailSerializer(user).data, "temporary_password": temp_password}
        if user.totp_secret:
            body.update(totp_secret=user.totp_secret, totp_uri=totp_uri(user))
        return Response(body, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        user = self.get_object()
        s = UserWriteSerializer(user, data=request.data, partial=True, context=self.get_serializer_context())
        s.is_valid(raise_exception=True)
        new_role = s.validated_data.get("role", user.role)
        new_active = s.validated_data.get("is_active", user.is_active)
        if new_active and (not user.is_active or seat_kind(new_role) != seat_kind(user.role)):
            check_seat_available(user.organisation, new_role, exclude_user=user)
        issued_secret = None
        with transaction.atomic():
            areas = s.validated_data.pop("areas", None)
            for k, v in s.validated_data.items():
                setattr(user, k, v)
            if user.role in TOTP_ROLES and not user.totp_secret:
                user.totp_secret = issued_secret = new_totp_secret()
            user.save()
            if areas is not None:
                user.areas.set(areas)
            if not user.is_active:
                AuthToken.objects.filter(user=user).delete()
        audit(request, "user.update", target=user, detail={"fields": sorted(request.data.keys())})
        body = dict(UserDetailSerializer(user).data)
        if issued_secret:
            body.update(totp_secret=issued_secret, totp_uri=totp_uri(user))
        return Response(body)

    def destroy(self, request, *args, **kwargs):
        """Soft delete: deactivate and revoke tokens (history such as patrols must stay attributable)."""
        user = self.get_object()
        if user.pk == request.user.pk:
            raise ApiError(400, "cannot_deactivate_self", "You cannot deactivate your own account.")
        user.is_active = False
        user.save(update_fields=["is_active", "updated_at"])
        AuthToken.objects.filter(user=user).delete()
        audit(request, "user.deactivate", target=user)
        return Response(status=status.HTTP_204_NO_CONTENT)
