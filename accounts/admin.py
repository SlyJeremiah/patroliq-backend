from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import AuthToken, Licence, LoginLockout, Organisation, User


class LicenceInline(admin.StackedInline):
    model = Licence
    extra = 0


@admin.register(Organisation)
class OrganisationAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "country", "status", "deployment", "created_at")
    search_fields = ("name", "code")
    inlines = [LicenceInline]


@admin.register(Licence)
class LicenceAdmin(admin.ModelAdmin):
    list_display = ("organisation", "plan", "max_rangers", "max_managers", "max_areas", "expires_at", "status")


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("organisation__code", "full_name")
    list_display = ("full_name", "organisation", "role", "employee_id", "email", "is_active", "last_sync_at")
    list_filter = ("role", "is_active", "organisation")
    search_fields = ("full_name", "employee_id", "email")
    readonly_fields = ("last_login", "last_sync_at", "created_at", "updated_at", "totp_last_step")
    fieldsets = (
        (None, {"fields": ("organisation", "role", "full_name", "employee_id", "email", "password")}),
        ("Profile", {"fields": ("phone", "language", "areas", "apu_base", "team")}),
        ("Personal details", {"classes": ("collapse",), "fields": (
            "first_name", "surname", "national_id", "date_of_birth", "home_address", "next_of_kin_name",
            "next_of_kin_relationship", "next_of_kin_phone", "next_of_kin_address", "date_joined_org",
            "rank", "post", "certificates")}),
        ("Security", {"fields": ("is_active", "is_staff", "is_superuser", "must_change_password", "totp_secret",
                                 "totp_last_step", "last_login", "last_sync_at")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("organisation", "role", "full_name", "employee_id",
                                                               "email", "password1", "password2")}),)
    filter_horizontal = ("areas",)


@admin.register(AuthToken)
class AuthTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "device_id", "created_at", "last_used_at")
    exclude = ("key",)


@admin.register(LoginLockout)
class LoginLockoutAdmin(admin.ModelAdmin):
    list_display = ("identifier", "failures", "locked_until", "last_failure_at")
