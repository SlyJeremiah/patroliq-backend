from django.conf import settings
from django.contrib import admin
from django.db import connection
from django.http import JsonResponse
from django.urls import include, path


def healthz(request):
    """Unauthenticated liveness probe (TechStack §3.5). Reveals nothing but DB reachability."""
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    return JsonResponse({"status": "ok" if db_ok else "degraded", "database": db_ok}, status=200 if db_ok else 503)


urlpatterns = [
    path("healthz/", healthz),
    path("api/v1/", include("patroliq.api_urls")),
]

if settings.ADMIN_ENABLED:
    urlpatterns.append(path("ops-admin/", admin.site.urls))

handler404 = "core.exceptions.json_404"
handler500 = "core.exceptions.json_500"
