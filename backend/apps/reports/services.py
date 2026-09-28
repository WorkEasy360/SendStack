from datetime import datetime, timedelta

from django.db.models import Avg, Count, Exists, OuterRef, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.automation.models import AutomationRule, FollowUpRule, Workflow
from apps.campaigns.models import Campaign
from apps.contacts.models import Contact
from apps.messaging.models import Message
from apps.whatsapp.models import WhatsAppAccount


def _parse_date(value):
    if not value:
        return None
    parsed = datetime.strptime(value, "%Y-%m-%d")
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def resolve_date_range(params) -> tuple:
    """Query-param driven date window: range=today|yesterday|last_7_days|last_30_days|custom (+ start_date/end_date)."""
    range_key = params.get("range")
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if range_key == "today":
        return today_start, now
    if range_key == "yesterday":
        yesterday_start = today_start - timedelta(days=1)
        return yesterday_start, today_start
    if range_key == "last_7_days":
        return today_start - timedelta(days=7), now
    if range_key == "last_30_days":
        return today_start - timedelta(days=30), now
    if range_key == "custom":
        start = _parse_date(params.get("start_date"))
        end = _parse_date(params.get("end_date"))
        if end is not None:
            end = end + timedelta(days=1)  # make end_date inclusive of the whole day
        return start, end
    return None, None  # all-time


def _message_queryset(tenant, start, end):
    queryset = Message.objects.filter(tenant=tenant)
    if start:
        queryset = queryset.filter(queued_at__gte=start)
    if end:
        queryset = queryset.filter(queued_at__lt=end)
    return queryset


def dashboard_metrics(tenant, start, end) -> dict:
    # Every count below is one DB round trip; the dashboard loads right after
    # login, so each is folded into one aggregate() per table (conditional
    # Count(..., filter=...)) instead of a separate query per metric.
    messages = _message_queryset(tenant, start, end)
    message_stats = messages.aggregate(
        sent=Count("id", filter=Q(direction=Message.Direction.OUTBOUND, status__in=[
            Message.Status.SENT, Message.Status.DELIVERED, Message.Status.READ,
        ])),
        delivered=Count("id", filter=Q(direction=Message.Direction.OUTBOUND, status__in=[
            Message.Status.DELIVERED, Message.Status.READ,
        ])),
        read=Count("id", filter=Q(direction=Message.Direction.OUTBOUND, status=Message.Status.READ)),
        failed=Count("id", filter=Q(direction=Message.Direction.OUTBOUND, status=Message.Status.FAILED)),
        messages_total=Count("id", filter=Q(direction=Message.Direction.OUTBOUND)),
        replies=Count("id", filter=Q(direction=Message.Direction.INBOUND)),
    )
    sent = message_stats["sent"]
    delivered = message_stats["delivered"]
    read = message_stats["read"]

    campaign_stats = Campaign.objects.filter(tenant=tenant).aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(status__in=[Campaign.Status.QUEUED, Campaign.Status.RUNNING, Campaign.Status.SCHEDULED])),
    )
    whatsapp_stats = WhatsAppAccount.objects.filter(tenant=tenant).aggregate(
        total=Count("id"),
        connected=Count("id", filter=Q(status=WhatsAppAccount.Status.CONNECTED)),
    )

    return {
        "total_contacts": Contact.objects.filter(tenant=tenant).count(),
        "total_campaigns": campaign_stats["total"],
        "active_campaigns": campaign_stats["active"],
        "whatsapp_accounts": whatsapp_stats["total"],
        "connected_whatsapp_accounts": whatsapp_stats["connected"],
        "messages_total": message_stats["messages_total"],
        "sent": sent,
        "delivered": delivered,
        "read": read,
        "failed": message_stats["failed"],
        "replies": message_stats["replies"],
        "delivery_rate": round(delivered / sent * 100, 1) if sent else 0.0,
        "read_rate": round(read / sent * 100, 1) if sent else 0.0,
        "active_automation_rules": AutomationRule.objects.filter(tenant=tenant, is_active=True).count(),
        "active_follow_up_rules": FollowUpRule.objects.filter(tenant=tenant, is_active=True).count(),
        "active_workflows": Workflow.objects.filter(tenant=tenant, is_active=True).count(),
    }


_DAILY_BUCKET_FIELDS = ("queued", "sending", "sent", "delivered", "read", "failed", "cancelled")


def daily_message_breakdown(tenant, start, end) -> list:
    queryset = _message_queryset(tenant, start, end).filter(direction=Message.Direction.OUTBOUND)
    # No .order_by(): it is what made this expensive, and it was never used.
    #
    # A trailing ORDER BY on the grouping key forces PostgreSQL to choose
    # GroupAggregate, which means sorting every matching message row first --
    # measured on a tenant with 189,000 messages that was a 6.3 MB external
    # merge *on disk* per dashboard load. Without it the planner picks
    # HashAggregate, whose memory is proportional to the number of distinct
    # (day, status) pairs -- a few hundred rows, 217 kB -- rather than to the
    # number of messages. Measured on the same data: 109ms -> 78ms, and the
    # gap widens with every message a tenant sends.
    #
    # Nothing is lost, because the rows were never consumed in SQL order
    # anyway: they are folded into a dict below and sorted in Python at the
    # end, where the input is one row per day rather than one per message.
    rows = (
        queryset.annotate(day=TruncDate("queued_at"))
        .values("day", "status")
        .annotate(total=Count("id"))
    )

    by_day = {}
    for row in rows:
        bucket = by_day.setdefault(row["day"], {"date": row["day"], **{field: 0 for field in _DAILY_BUCKET_FIELDS}})
        key = row["status"].lower()
        if key in bucket:
            bucket[key] += row["total"]

    return sorted(by_day.values(), key=lambda r: r["date"])


def campaign_report(campaign) -> dict:
    campaign_messages = Message.objects.filter(campaign=campaign, direction=Message.Direction.OUTBOUND, sent_at__isnull=False)
    replied_subquery = Message.objects.filter(
        whatsapp_account_id=OuterRef("whatsapp_account_id"), contact_id=OuterRef("contact_id"),
        direction=Message.Direction.INBOUND, queued_at__gt=OuterRef("sent_at"),
    )
    reply_count = campaign_messages.annotate(replied=Exists(replied_subquery)).filter(replied=True).count()

    sent = campaign.sent_count
    clicks = campaign_click_totals(campaign)
    return {
        "campaign_id": campaign.pk,
        "name": campaign.name,
        "status": campaign.status,
        "total_recipients": campaign.total_recipients,
        "sent": campaign.sent_count,
        "delivered": campaign.delivered_count,
        "read": campaign.read_count,
        "failed": campaign.failed_count,
        "skipped": campaign.skipped_count,
        "replies": reply_count,
        "delivery_rate": round(campaign.delivered_count / sent * 100, 1) if sent else 0.0,
        "read_rate": round(campaign.read_count / sent * 100, 1) if sent else 0.0,
        "reply_rate": round(reply_count / sent * 100, 1) if sent else 0.0,
        "clicks": clicks["clicks"],
        "contacts_clicked": clicks["contacts_clicked"],
        # Measured against messages actually sent rather than against links
        # created, so a campaign that failed halfway is not flattered by the
        # recipients it never reached.
        "click_rate": round(clicks["contacts_clicked"] / sent * 100, 1) if sent else 0.0,
        "account_assignment_mode": campaign.account_assignment_mode,
        "min_delay_seconds": campaign.delay_bounds()[0],
        "max_delay_seconds": campaign.delay_bounds()[1],
        "accounts": campaign_account_breakdown(campaign),
    }


def campaign_click_totals(campaign) -> dict:
    """
    Clicks on this campaign's links.

    `contacts_clicked` is the number that belongs next to "delivered" and
    "read": those count people, and a raw click total counts taps, which one
    enthusiastic recipient can inflate on their own.
    """
    from apps.tracking.models import TrackedLink

    row = TrackedLink.objects.filter(campaign=campaign).aggregate(
        clicks=Sum("click_count"),
        contacts_clicked=Count("contact", filter=Q(click_count__gt=0), distinct=True),
    )
    return {"clicks": row["clicks"] or 0, "contacts_clicked": row["contacts_clicked"] or 0}


def campaign_account_breakdown(campaign) -> list:
    """
    Per-WhatsApp-account totals for one campaign.

    The point of this is auditing an automatic assignment: it's how you see
    at a glance that recipients really were spread across the connected
    devices rather than all piled onto one, and which device a run of
    failures belongs to. One grouped query, not one per account.
    """
    rows = (
        Message.objects.filter(campaign=campaign, direction=Message.Direction.OUTBOUND)
        .values("whatsapp_account_id", "whatsapp_account__name", "whatsapp_account__phone_number")
        .annotate(
            total=Count("id"),
            sent=Count("id", filter=Q(status__in=[Message.Status.SENT, Message.Status.DELIVERED, Message.Status.READ])),
            delivered=Count("id", filter=Q(status__in=[Message.Status.DELIVERED, Message.Status.READ])),
            read=Count("id", filter=Q(status=Message.Status.READ)),
            failed=Count("id", filter=Q(status=Message.Status.FAILED)),
            skipped=Count("id", filter=Q(status=Message.Status.SKIPPED)),
            avg_delay_seconds=Avg("assigned_delay_seconds"),
        )
        .order_by("-total")
    )

    return [
        {
            "account_id": row["whatsapp_account_id"],
            "name": row["whatsapp_account__name"],
            "phone_number": row["whatsapp_account__phone_number"] or "",
            "total": row["total"],
            "sent": row["sent"],
            "delivered": row["delivered"],
            "read": row["read"],
            "failed": row["failed"],
            "skipped": row["skipped"],
            "avg_delay_seconds": round(row["avg_delay_seconds"], 3) if row["avg_delay_seconds"] is not None else None,
        }
        for row in rows
    ]
