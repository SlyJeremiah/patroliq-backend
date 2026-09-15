"""
Role-based permissions (spec §2, PRD 6.2 RBAC matrix) and licence gating (spec §1).
"""
from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission

from .exceptions import ApiError

from .roles import (  # noqa: F401
    ADMINS, FIELD_ROLES, MANAGER, MANAGERS, ORG_ADMIN, ORG_ROLES, PLATFORM_ADMIN, RANGER, RESEARCHER,
    ROLE_CHOICES, TOTP_ROLES, VIEWER,
)


def role_of(request) -> str | None:
    user = getattr(request, "user", None)
    return getattr(user, "role", None) if user is not None and user.is_authenticated else None


class LicenceNotSuspended(BasePermission):
    """
    Default for every endpoint: a suspended organisation (licence expired beyond grace, or
    suspended by zrGISsolutions) gets ``403 licence_suspended``. Safety endpoints opt out
    (rangers are never cut off from SOS).
    """

    def has_permission(self, request, view):
        user = request.user
        if user is None or not user.is_authenticated or user.organisation_id is None:
            return True
        from accounts.licensing import effective_status

        if effective_status(user.organisation) == "suspended":
            raise ApiError(403, "licence_suspended", "The organisation's licence is suspended.")
        return True


class IsOrgMember(BasePermission):
    """Tenant endpoints are for organisation users only; platform admins get 403."""

    message = "This endpoint is only available to organisation users."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.organisation_id and user.role in ORG_ROLES)


def roles_allowed(read: set[str], write: set[str] | None = None, allow_suspended: bool = False):
    """
    Permission class factory: ``read`` roles for safe methods, ``write`` roles otherwise.
    Also enforces the licence (``403 licence_suspended``) unless ``allow_suspended`` — views that set
    ``permission_classes`` explicitly replace DRF's defaults, so the check must live here too.
    """
    write = read if write is None else write

    class _RolePermission(IsOrgMember):
        message = "Your role is not permitted to perform this action."

        def has_permission(self, request, view):
            if not super().has_permission(request, view):
                return False
            if not allow_suspended:
                LicenceNotSuspended().has_permission(request, view)
            allowed = read if request.method in SAFE_METHODS else write
            return request.user.role in allowed

    _RolePermission.__name__ = f"Roles_{'_'.join(sorted(read))}__{'_'.join(sorted(write))}"
    return _RolePermission


class IsPlatformAdmin(BasePermission):
    message = "Platform admin only."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.role == PLATFORM_ADMIN and user.organisation_id is None)


def require_module(organisation, module: str) -> None:
    from accounts.licensing import module_enabled

    if not module_enabled(organisation, module):
        raise ApiError(403, "module_disabled", f"The '{module}' module is not enabled for this organisation.")
