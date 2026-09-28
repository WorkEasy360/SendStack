import csv
from datetime import timedelta

from django.core.cache import cache
from django.http import Http404, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.campaigns.models import Campaign
from apps.messaging.models import Message
from core.csv_utils import EchoBuffer, csv_safe_row

from . import business, services

# Numbers here can lag real send/delivery activity by up to this long. Short
# enough that nobody watching a live campaign notices, long enough to absorb
# repeat dashboard loads (refreshes, multiple tabs) without re-running the
# aggregate queries every time.
DASHBOARD_CACHE_TTL_SECONDS = 45


def _range_cache_key(prefix, request):
    """
    A cache key for one tenant's view of one date range.

    Keyed on the raw range params, not on the resolved start/end -- "today"
    and "last_30_days" resolve `end` to `now()` on every call, which would
    make a resolved-timestamp key miss every single time.

    The tenant id is part of the key and not an afterthought: these responses
    are one workspace's message volumes, and a key that omitted it would serve
    one customer's numbers to another.
    """
    return "reports:{}:{}:{}:{}:{}".format(
        prefix,
        request.user.tenant_id,
        request.query_params.get("range") or "",
        request.query_params.get("start_date") or "",
        request.query_params.get("end_date") or "",
    )


class DashboardReportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        cache_key = _range_cache_key("dashboard", request)
        metrics = cache.get(cache_key)
        if metrics is None:
            start, end = services.resolve_date_range(request.query_params)
            metrics = services.dashboard_metrics(request.user.tenant, start, end)
            cache.set(cache_key, metrics, DASHBOARD_CACHE_TTL_SECONDS)
        return Response(metrics)


class DailyMessageReportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Cached on the same terms as the dashboard above, because it is the
        # same screen: the reports page requests both together, so one of them
        # being cached and the other re-aggregating the tenant's whole message
        # history on every refresh made the caching largely pointless.
        #
        # This one benefits more, not less. The dashboard's counts are
        # conditional aggregates over an index; this groups every matching
        # message row by day, which is the most expensive read in the product.
        cache_key = _range_cache_key("daily", request)
        payload = cache.get(cache_key)
        if payload is None:
            start, end = services.resolve_date_range(request.query_params)
            rows = services.daily_message_breakdown(request.user.tenant, start, end)
            payload = [{**row, "date": row["date"].isoformat()} for row in rows]
            cache.set(cache_key, payload, DASHBOARD_CACHE_TTL_SECONDS)
        return Response(payload)


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
                # The contact name and the provider's error text both originate
                # outside this business, and both land in a file somebody opens
                # in Excel. See core.csv_utils.
                yield writer.writerow(csv_safe_row([
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
                ]))

        response = StreamingHttpResponse(rows(), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="campaign-{campaign.pk}-report.csv"'
        return response


class PlatformStaffView(APIView):
    """
    Base for the business reports: superadmin only, and 404 to everybody else.

    404 rather than 403 on purpose, and it is the same choice the operations
    dashboard already makes. A 403 confirms the endpoint exists, which tells
    somebody probing that there is a platform-wide revenue report at this path
    and that they merely have the wrong role. A 404 tells them nothing.

    A reseller is **not** platform staff here. These numbers are the whole
    deployment's -- every reseller's revenue, every reseller's customers -- and
    that is precisely the thing one reseller must never see about another.
    """

    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        user = request.user
        if user.role != user.Role.SUPERADMIN:
            raise Http404

    @staticmethod
    def window_days(request, default=30, maximum=365) -> int:
        try:
            days = int(request.query_params.get("days", default))
        except (TypeError, ValueError):
            return default
        return max(min(days, maximum), 1)


class ExecutiveDashboardView(PlatformStaffView):
    """
    Revenue, activation, retention and volume, in one response.

    One request rather than nine. A dashboard split across nine endpoints is
    nine chances to be slow and nine ways to paint half a screen, and these
    numbers are all read from the same few tables in the same window anyway.

    Every metric arrives with its definition attached -- see
    apps.reports.business.DEFINITIONS for why that is not documentation
    politeness.
    """

    def get(self, request):
        return Response(business.executive(days=self.window_days(request)))


class RevenueReportView(PlatformStaffView):
    """
    MRR and what moved it, on its own for whoever only wants the money.

    MRR and collected revenue are both here and are labelled as different
    things, because they are: one is the contracted run rate, the other is cash
    that arrived. An annual plan sold today moves the second by twelve months
    and the first by one.
    """

    def get(self, request):
        days = self.window_days(request)
        now = timezone.now()
        since = now - timedelta(days=days)
        return Response({
            "window_days": days,
            "current": business.mrr(now=now),
            "movements": business.revenue_movements(since=since, until=now),
            "definitions": business.DEFINITIONS,
        })


class ReconciliationView(PlatformStaffView):
    """
    Where our records and the payment provider's disagree.

    Reported, never repaired. Every mismatch here has more than one possible
    cause -- a lost webhook, a refund, a manual activation, a test event from
    another environment -- and a job that guessed would eventually turn a
    refund into an activation. A person decides.
    """

    def get(self, request):
        days = self.window_days(request, default=30)
        now = timezone.now()
        return Response(business.reconciliation(since=now - timedelta(days=days), until=now))


class CustomerHealthView(PlatformStaffView):
    """
    Workspaces worth a human looking at, and the reason for each.

    Five signals, all of them something that has gone wrong and that somebody
    could help with. Deliberately not a behavioural profile: nothing here
    records how often somebody signs in or what they looked at, because that
    would be surveillance rather than support and it would not say what to do
    next.

    Read-only, and it sends nothing. Nobody is emailed because they appear on
    this list -- see docs/METRICS.md on why reactivation is a decision rather
    than an automation.
    """

    def get(self, request):
        return Response(business.health_signals())


class RetentionReportView(PlatformStaffView):
    """
    New, active, returning and lapsed workspaces over a window.

    "Active" means sent something, not signed in. In a product whose value is
    delivered by sending, a workspace whose owner opens the dashboard daily and
    sends nothing is not retained -- and counting them as retained is how a
    retention chart stays flat right up until the renewals do not.
    """

    def get(self, request):
        days = self.window_days(request)
        now = timezone.now()
        return Response(business.retention(since=now - timedelta(days=days), until=now))


class ActivationReportView(PlatformStaffView):
    """
    How many workspaces created in the window went on to use the product.

    Cohorted by creation date. Cohorting by activation date would make the rate
    rise whenever an old customer finally got going, which flatters the number
    and answers nothing about whether onboarding works.
    """

    def get(self, request):
        days = self.window_days(request)
        now = timezone.now()
        return Response(business.activation(since=now - timedelta(days=days), until=now))
