import csv

from django.core.cache import cache
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.campaigns.models import Campaign
from apps.messaging.models import Message
from core.csv_utils import EchoBuffer

from . import services

# Numbers here can lag real send/delivery activity by up to this long. Short
# enough that nobody watching a live campaign notices, long enough to absorb
# repeat dashboard loads (refreshes, multiple tabs) without re-running the
# aggregate queries every time.
DASHBOARD_CACHE_TTL_SECONDS = 45


class DashboardReportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Keyed on the raw range params, not resolved start/end -- "today"
        # and "last_30_days" resolve `end` to `now()` on every call, which
        # would make a resolved-timestamp cache key miss every single time.
        cache_key = "reports:dashboard:{}:{}:{}:{}".format(
            request.user.tenant_id,
            request.query_params.get("range") or "",
            request.query_params.get("start_date") or "",
            request.query_params.get("end_date") or "",
        )
        metrics = cache.get(cache_key)
        if metrics is None:
            start, end = services.resolve_date_range(request.query_params)
            metrics = services.dashboard_metrics(request.user.tenant, start, end)
            cache.set(cache_key, metrics, DASHBOARD_CACHE_TTL_SECONDS)
        return Response(metrics)


class DailyMessageReportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        start, end = services.resolve_date_range(request.query_params)
        rows = services.daily_message_breakdown(request.user.tenant, start, end)
        return Response([{**row, "date": row["date"].isoformat()} for row in rows])


class CampaignReportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, campaign_id):
        campaign = get_object_or_404(Campaign, pk=campaign_id, tenant=request.user.tenant)
        return Response(services.campaign_report(campaign))


class CampaignReportExportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, campaign_id):
        campaign = get_object_or_404(Campaign, pk=campaign_id, tenant=request.user.tenant)
        messages = (
            Message.objects.filter(campaign=campaign)
            .select_related("contact", "whatsapp_account")
            .order_by("queued_at")
        )

        def rows():
            buffer = EchoBuffer()
            writer = csv.writer(buffer)
            # New columns are appended, never inserted: anything already
            # parsing this export by column position keeps working.
            yield writer.writerow([
                "contact_name", "phone", "status", "sent_at", "delivered_at", "read_at", "error_message",
                "whatsapp_account", "whatsapp_number", "delay_seconds",
            ])
            for message in messages.iterator(chunk_size=200):
                yield writer.writerow([
                    message.contact.name if message.contact else "",
                    message.to_phone,
                    message.status,
                    message.sent_at or "",
                    message.delivered_at or "",
                    message.read_at or "",
                    message.error_message,
                    message.whatsapp_account.name if message.whatsapp_account_id else "",
                    message.whatsapp_account.phone_number or "" if message.whatsapp_account_id else "",
                    "" if message.assigned_delay_seconds is None else f"{message.assigned_delay_seconds:.3f}",
                ])

        response = StreamingHttpResponse(rows(), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="campaign-{campaign.pk}-report.csv"'
        return response
