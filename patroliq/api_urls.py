"""/api/v1/ routes — paths match PATROLIQ Platform Spec v1.2 §5 exactly."""
from django.urls import include, path
from rest_framework.routers import SimpleRouter

from accounts.views import LoginView, LogoutView, MeView, PasswordChangeView, UserViewSet
from areas.views import ApuBaseViewSet, AreaViewSet, AssignmentViewSet, TeamViewSet
from audit.views import AuditLogListView
from dashboard import views as dashboard_views
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
    # Manager dashboard (spec §7)
    path("alerts/<uuid:alert_id>/", field_views.AlertDetailView.as_view(), name="alert-detail"),
    path("alerts/<uuid:alert_id>/dispatch/", field_views.AlertDispatchView.as_view(), name="alert-dispatch"),
    path("dashboard/summary/", dashboard_views.SummaryView.as_view(), name="dashboard-summary"),
    path("rangers/", dashboard_views.RangerListView.as_view(), name="rangers"),
    path("rangers/<uuid:pk>/", dashboard_views.RangerDetailView.as_view(), name="ranger-detail"),
    path("rangers/<uuid:pk>/message/", dashboard_views.RangerMessageView.as_view(), name="ranger-message"),
    path("patrols/<uuid:client_uuid>/track/", field_views.PatrolTrackView.as_view(), name="patrol-track"),
    path("positions/history/", field_views.PositionHistoryView.as_view(), name="positions-history"),
    path("areas/<uuid:pk>/risk/", dashboard_views.AreaRiskView.as_view(), name="area-risk"),
    path("areas/<uuid:pk>/risk/trend/", dashboard_views.AreaRiskTrendView.as_view(), name="area-risk-trend"),
    path("areas/<uuid:pk>/heatmap/", dashboard_views.AreaHeatmapView.as_view(), name="area-heatmap"),
    path("areas/<uuid:pk>/coverage/", dashboard_views.AreaCoverageView.as_view(), name="area-coverage"),
    path("areas/<uuid:pk>/coverage/export/", dashboard_views.AreaCoverageExportView.as_view(), name="area-coverage-export"),
    path("reports/", dashboard_views.ReportListCreateView.as_view(), name="reports"),
    path("reports/shared/<str:token>/", dashboard_views.SharedReportView.as_view(), name="report-shared"),
    path("reports/<uuid:pk>/", dashboard_views.ReportDetailView.as_view(), name="report-detail"),
    path("reports/<uuid:pk>/download/", dashboard_views.ReportDownloadView.as_view(), name="report-download"),
    path("reports/<uuid:pk>/share/", dashboard_views.ReportShareView.as_view(), name="report-share"),
    path("audit-log/", AuditLogListView.as_view(), name="audit-log"),
    path("species/", field_views.SpeciesListView.as_view(), name="species"),
    # Platform
    path("platform/organisations/", platform_views.OrganisationListCreateView.as_view(), name="platform-orgs"),
    path("platform/organisations/<uuid:pk>/", platform_views.OrganisationDetailView.as_view(), name="platform-org"),
    path("platform/organisations/<uuid:pk>/licence/", platform_views.LicenceView.as_view(), name="platform-licence"),
    path("platform/organisations/<uuid:pk>/usage/", platform_views.UsageView.as_view(), name="platform-usage"),
    path("", include(router.urls)),
]
