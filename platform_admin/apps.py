from django.apps import AppConfig


class PlatformAdminConfig(AppConfig):
    # Named platform_admin (not "platform") to avoid shadowing Python's stdlib ``platform`` module.
    name = "platform_admin"
    verbose_name = "zrGISsolutions platform"
