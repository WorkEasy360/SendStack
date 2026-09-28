"""
Aggregate delivery numbers, for a dashboard somebody is building elsewhere.

Thin on purpose. `apps.reports.services` already computes every one of these
and is what the SPA's own reporting screen reads, so this endpoint translates
the query parameters and the field names and does no arithmetic of its own --
a second implementation would eventually disagree with the first, and the
customer would have two different answers to "how many did we send?".

Only the workspace's own numbers are exposed. The platform-staff reports in
`apps.reports.views` (revenue, reconciliation, retention, customer health) have
no public equivalent and never should: they are about the business running
SendStack, not about the workspace holding the key.
"""

from apps.reports import services as report_services

from .. import scopes
from ..errors import INVALID_REQUEST, PublicAPIError
from .base import PublicAPIView

#: The windows `apps.reports.services.resolve_date_range` understands. Listed
#: here so an unknown value is a 400 naming the alternatives, rather than
#: silently falling through to all-time -- a caller asking for last_7_days and
#: getting the workspace's entire history would not notice until the numbers
#: looked wrong.
RANGES = ("today", "yesterday", "last_7_days", "last_30_days", "custom")


class ReportSummaryView(PublicAPIView):
    """``GET /reports/summary`` -- headline delivery numbers for a window."""

    required_scopes = [scopes.REPORTS_READ]

    def get(self, request):
        range_key = request.query_params.get("range")
        if range_key and range_key not in RANGES:
            raise PublicAPIError(
                INVALID_REQUEST,
                f"Unknown range '{range_key}'. Use one of: {', '.join(RANGES)}, or omit it for all time.",
            )

        start, end = report_services.resolve_date_range(request.query_params)
        metrics = report_services.dashboard_metrics(request.auth.tenant, start, end)

        return self.ok({
            "range": range_key or "all_time",
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            # Re-keyed rather than passed through: `dashboard_metrics` returns
            # the SPA's field names, and those are free to change when that
            # screen changes. What is published here is not.
            "messages": {
                "total": metrics["messages_total"],
                "sent": metrics["sent"],
                "delivered": metrics["delivered"],
                "read": metrics["read"],
                "failed": metrics["failed"],
                "received": metrics["replies"],
            },
            "rates": {
                "delivery": metrics["delivery_rate"],
                "read": metrics["read_rate"],
            },
            "campaigns": {
                "total": metrics["total_campaigns"],
                "active": metrics["active_campaigns"],
            },
            "accounts": {
                "total": metrics["whatsapp_accounts"],
                "connected": metrics["connected_whatsapp_accounts"],
            },
            "contacts": {"total": metrics["total_contacts"]},
        })


class ReportDailyView(PublicAPIView):
    """``GET /reports/daily`` -- one row per day, for charting."""

    required_scopes = [scopes.REPORTS_READ]

    def get(self, request):
        range_key = request.query_params.get("range")
        if range_key and range_key not in RANGES:
            raise PublicAPIError(
                INVALID_REQUEST,
                f"Unknown range '{range_key}'. Use one of: {', '.join(RANGES)}, or omit it for all time.",
            )

        start, end = report_services.resolve_date_range(request.query_params)
        rows = report_services.daily_message_breakdown(request.auth.tenant, start, end)

        return self.ok({"range": range_key or "all_time", "days": rows})
