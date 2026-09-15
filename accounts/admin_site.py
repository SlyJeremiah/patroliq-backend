from django import forms
from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm

from .security import clear_failures, lockout_remaining, register_failure, verify_totp, web_identifier


class TOTPAdminAuthenticationForm(AdminAuthenticationForm):
    """Email + password + TOTP, with the same lockout as the API. Only superusers may enter."""

    totp = forms.CharField(label="Authenticator code", max_length=10, required=True)

    def clean(self):
        identifier = web_identifier(self.data.get("username", ""))
        if lockout_remaining(identifier):
            raise forms.ValidationError("Too many failed attempts. Try again later.", code="locked_out")
        try:
            super().clean()
        except forms.ValidationError:
            register_failure(identifier)
            raise
        user = self.get_user()
        if not user.is_superuser or not verify_totp(user, self.cleaned_data.get("totp", "")):
            register_failure(identifier)
            raise forms.ValidationError("Invalid authenticator code.", code="invalid_totp")
        clear_failures(identifier)
        return self.cleaned_data


class PatrolIQAdminSite(admin.AdminSite):
    site_header = "PATROLIQ operations"
    site_title = "PATROLIQ ops"
    index_title = "Operations"
    login_form = TOTPAdminAuthenticationForm
    login_template = "admin/patroliq_login.html"

    def has_permission(self, request):
        return request.user.is_active and request.user.is_superuser
