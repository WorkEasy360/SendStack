"""
Report routes.

Two groups, and the split is who may read them. Everything under the bare
paths is **one workspace's own numbers**, scoped to the caller's tenant.
Everything under `business/` is the **whole deployment's** -- every reseller's
revenue and every reseller's customers -- and is superadmin only, 404 to
anybody else. Keeping them in separate prefixes means the boundary is visible
in the URL map rather than only in a permission class.
"""

from django.urls import path

from .views import (
    ActivationReportView,
    CampaignReportExportView,
    CampaignReportView,
    CustomerHealthView,
    DailyMessageReportView,
    DashboardReportView,
    ExecutiveDashboardView,
    ReconciliationView,
    RetentionReportView,
    RevenueReportView,
)

urlpatterns = [
    # One workspace's own numbers.
    path("dashboard/", DashboardReportView.as_view(), name="report-dashboard"),
    path("messages/daily/", DailyMessageReportView.as_view(), name="report-messages-daily"),
    path("campaigns/<int:campaign_id>/", CampaignReportView.as_view(), name="report-campaign"),
    path("campaigns/<int:campaign_id>/export/", CampaignReportExportView.as_view(), name="report-campaign-export"),

    # The whole deployment's. Superadmin only -- see PlatformStaffView.
    path("business/", ExecutiveDashboardView.as_view(), name="report-business"),
    path("business/revenue/", RevenueReportView.as_view(), name="report-business-revenue"),
    path("business/retention/", RetentionReportView.as_view(), name="report-business-retention"),
    path("business/activation/", ActivationReportView.as_view(), name="report-business-activation"),
    path("business/health/", CustomerHealthView.as_view(), name="report-business-health"),
    path("business/reconciliation/", ReconciliationView.as_view(), name="report-business-reconciliation"),
]
