from django.contrib.admin.apps import AdminConfig


class PatrolIQAdminConfig(AdminConfig):
    """Django admin whose login form also requires a TOTP code (PRD 7.3: 2FA cannot be bypassed)."""

    default_site = "accounts.admin_site.PatrolIQAdminSite"
