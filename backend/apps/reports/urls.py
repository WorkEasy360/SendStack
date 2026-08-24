from django.urls import path

from .views import CampaignReportExportView, CampaignReportView, DailyMessageReportView, DashboardReportView

urlpatterns = [
    path("dashboard/", DashboardReportView.as_view(), name="report-dashboard"),
    path("messages/daily/", DailyMessageReportView.as_view(), name="report-messages-daily"),
    path("campaigns/<int:campaign_id>/", CampaignReportView.as_view(), name="report-campaign"),
    path("campaigns/<int:campaign_id>/export/", CampaignReportExportView.as_view(), name="report-campaign-export"),
]
