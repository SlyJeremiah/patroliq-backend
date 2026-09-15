"""Role constants (no framework imports, safe to use from models)."""

PLATFORM_ADMIN = "platform_admin"
ORG_ADMIN = "org_admin"
MANAGER = "manager"
RANGER = "ranger"
RESEARCHER = "researcher"
VIEWER = "viewer"

ROLE_CHOICES = [
    (PLATFORM_ADMIN, "Platform admin"),
    (ORG_ADMIN, "Organisation admin"),
    (MANAGER, "Manager"),
    (RANGER, "Ranger"),
    (RESEARCHER, "Researcher"),
    (VIEWER, "Viewer (NGO/Government)"),
]

ADMINS = {ORG_ADMIN}
MANAGERS = {ORG_ADMIN, MANAGER}
FIELD_ROLES = {ORG_ADMIN, MANAGER, RANGER}
ORG_ROLES = {ORG_ADMIN, MANAGER, RANGER, RESEARCHER, VIEWER}
TOTP_ROLES = {MANAGER, ORG_ADMIN, PLATFORM_ADMIN}
