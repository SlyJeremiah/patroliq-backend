"""/api/v1/ routes — paths match PATROLIQ Platform Spec v1.2 §5 exactly."""
from django.urls import include, path
from rest_framework.routers import SimpleRouter

from accounts.views import LoginView, LogoutView, MeView, PasswordChangeView, UserViewSet
from areas.views import ApuBaseViewSet, AreaViewSet, AssignmentViewSet, TeamViewSet
from audit.views import AuditLogListView
from field import views as field_views
from platform_admin import views as platform_views

router = SimpleRouter()
router.register("areas", AreaViewSet, basename="area")
router.register("apu-bases", ApuBaseViewSet, basename="apu-base")
router.register("teams", TeamViewSet, basename="team")
router.register("assignments", AssignmentViewSet, basename="assignment")
router.register("users", UserViewSet, basename="user")
router.register("observations", field_views.ObservationViewSet, basename="observation")
router.register("patrols", field_views.PatrolViewSet, basename="patrol")

media_detail = field_views.MediaViewSet.as_view({"get": "retrieve"})
media_file = field_views.MediaViewSet.as_view({"get": "file"})

urlpatterns = [
    # Auth
    path("auth/login/", LoginView.as_view(), name="auth-login"),
    path("auth/logout/", LogoutView.as_view(), name="auth-logout"),
    path("auth/password/", PasswordChangeView.as_view(), name="auth-password"),
    path("me/", MeView.as_view(), name="me"),
    # Ranger sync
    path("sync/bootstrap/", field_views.BootstrapView.as_view(), name="sync-bootstrap"),
    path("sync/push/", field_views.PushView.as_view(), name="sync-push"),
    path("media/", field_views.MediaUploadView.as_view(), name="media-upload"),
    path("media/<uuid:pk>/", media_detail, name="media-detail"),
    path("media/<uuid:pk>/file/", media_file, name="media-file"),
    path("positions/", field_views.PositionsView.as_view(), name="positions"),
    path("positions/latest/", field_views.LatestPositionsView.as_view(), name="positions-latest"),
    path("safety/alerts/", field_views.SafetyAlertCreateView.as_view(), name="safety-alerts"),
    path("safety/alerts/<uuid:client_uuid>/cancel/", field_views.SafetyAlertCancelView.as_view(), name="safety-cancel"),
    # Manager
    path("alerts/", field_views.AlertListView.as_view(), name="alerts"),
    path("alerts/<uuid:alert_id>/acknowledge/", field_views.AlertAcknowledgeView.as_view(), name="alert-ack"),
    path("alerts/<uuid:alert_id>/resolve/", field_views.AlertResolveView.as_view(), name="alert-resolve"),
    path("audit-log/", AuditLogListView.as_view(), name="audit-log"),
    path("species/", field_views.SpeciesListView.as_view(), name="species"),
    # Platform
    path("platform/organisations/", platform_views.OrganisationListCreateView.as_view(), name="platform-orgs"),
    path("platform/organisations/<uuid:pk>/", platform_views.OrganisationDetailView.as_view(), name="platform-org"),
    path("platform/organisations/<uuid:pk>/licence/", platform_views.LicenceView.as_view(), name="platform-licence"),
    path("platform/organisations/<uuid:pk>/usage/", platform_views.UsageView.as_view(), name="platform-usage"),
    path("", include(router.urls)),
]
