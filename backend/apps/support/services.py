"""
Capturing what was true about a workspace when somebody asked for help.

The whole value of this module is in what it refuses to collect. A support
context is read by a person who is not the customer, is quoted into replies,
and ends up in screenshots — so it holds **counts, statuses and identifiers,
and nothing that identifies one of the customer's own contacts**. No phone
number, no contact name, no message body, no email address, no token, no
provider credential.

That constraint is not a nicety. The support view is the one screen in the
product where somebody has a legitimate reason to look at another company's
workspace, which makes it exactly the screen where "just show a bit more"
accumulates until it is a window onto every conversation a customer has ever
had. Drawing the line at aggregates keeps the view genuinely useful — it
answers every question support actually asks — while making that accumulation
impossible rather than merely discouraged.

Everything here is best-effort. A context that cannot be built must never stop
a customer submitting a request: somebody asking for help while the product is
broken is the case this exists for, and it is precisely when a lookup is most
likely to fail.
"""

import logging

from django.utils import timezone

logger = logging.getLogger("sendstack")

#: How far back "recent" reaches for the failure and campaign counts. Long
#: enough to cover "it stopped working yesterday", short enough that the counts
#: describe now rather than the year.
RECENT_DAYS = 7


def capture_context(tenant, *, campaign_id=None) -> dict:
    """
    A safe snapshot of this workspace, for whoever answers.

    Never raises. Each block is gathered independently, so one broken subsystem
    degrades that block to an error marker instead of losing the whole context
    — and a support request raised *because* something is broken is the one
    that most needs the rest of the picture.
    """
    context = {"captured_at": timezone.now().isoformat()}

    for key, gather in (
        ("subscription", _subscription),
        ("usage", _usage),
        ("devices", _devices),
        ("campaigns", _campaigns),
        ("failures", _recent_failures),
    ):
        try:
            context[key] = gather(tenant)
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.exception("Could not gather the %s block of a support context.", key)
            context[key] = {"error": "unavailable"}

    if campaign_id:
        try:
            context["campaign"] = _campaign_detail(tenant, campaign_id)
        except Exception:  # noqa: BLE001
            logger.exception("Could not gather the campaign block of a support context.")
            context["campaign"] = {"error": "unavailable"}

    return context


def _subscription(tenant) -> dict:
    from apps.subscriptions import services as entitlements

    subscription = entitlements.get_active_subscription(getattr(tenant, "pk", tenant))
    if subscription is None:
        return {"plan": None, "status": None, "access_state": "NONE"}
    return {
        "plan": subscription.plan.name,
        "status": subscription.status,
        "access_state": subscription.access_state(),
        "is_trial": subscription.is_trial,
        "trial_days_remaining": subscription.trial_days_remaining(),
        "period_end": subscription.current_period()[1].isoformat(),
    }


def _usage(tenant) -> dict:
    from apps.subscriptions import services as entitlements

    tenant_id = getattr(tenant, "pk", tenant)
    used = entitlements.usage(tenant_id)
    limits = entitlements.limits_for(tenant_id)
    return {
        "used": used,
        "limits": limits,
        "credits": _credit_balance(tenant_id),
    }


def _credit_balance(tenant_id):
    """
    The workspace's credit balance, or None if credits are not in play.

    Read through the same helper the send gate uses where one exists, so this
    cannot report a balance the gate would disagree with.
    """
    from django.contrib.auth import get_user_model

    from django.db.models import Sum

    total = (
        get_user_model()
        .objects.filter(tenant_id=tenant_id, is_active=True)
        .aggregate(total=Sum("credits"))
        .get("total")
    )
    return int(total) if total is not None else None


def _devices(tenant) -> list:
    """
    The workspace's WhatsApp numbers, by state.

    **The phone number is not included.** "Which of my numbers?" is answered by
    the name the customer gave it, which they chose and will recognise; the
    number itself is their customers' route to them and is not needed to answer
    a support question.
    """
    from apps.whatsapp.models import WhatsAppAccount

    return [
        {
            "id": account.pk,
            "name": account.name,
            "status": account.status,
            "provider": getattr(account, "provider", "") or "",
        }
        for account in WhatsAppAccount.objects.filter(tenant=tenant).only(
            "id", "name", "status", "provider",
        )[:20]
    ]


def _campaigns(tenant) -> dict:
    from datetime import timedelta

    from django.db.models import Count

    from apps.campaigns.models import Campaign

    since = timezone.now() - timedelta(days=RECENT_DAYS)
    rows = (
        Campaign.objects.filter(tenant=tenant, created_at__gte=since)
        .values("status")
        .annotate(count=Count("id"))
    )
    return {
        "window_days": RECENT_DAYS,
        "by_status": {row["status"]: row["count"] for row in rows},
        "total": Campaign.objects.filter(tenant=tenant).count(),
    }


def _recent_failures(tenant) -> dict:
    """
    Why messages failed lately, grouped.

    Grouped by the failure *reason* rather than listed per message: the
    question support is answering is "what is going wrong", and a list of
    individual failures would be a list of the customer's contacts.
    """
    from datetime import timedelta

    from django.db.models import Count

    from apps.messaging.models import Message

    since = timezone.now() - timedelta(days=RECENT_DAYS)
    labels = dict(Message.FailureReason.choices)
    rows = (
        Message.objects.filter(
            tenant=tenant, status=Message.Status.FAILED, queued_at__gte=since,
        )
        # `failure_reason`, not `error_message`. The code is stable and the
        # sentence is not -- see the long note on Message.FailureReason for why
        # grouping on prose silently changes the answer.
        .values("failure_reason")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )
    return {
        "window_days": RECENT_DAYS,
        "by_reason": [
            {
                "reason": row["failure_reason"] or "unknown",
                "label": labels.get(row["failure_reason"], "Reason not recorded"),
                "count": row["count"],
            }
            for row in rows
        ],
    }


def _campaign_detail(tenant, campaign_id) -> dict:
    """
    One campaign's counts, scoped to the workspace that asked.

    Tenant-scoped like every other lookup in this codebase: a customer naming
    somebody else's campaign id in a support form gets "not found", not that
    campaign's numbers.
    """
    from django.db.models import Count

    from apps.campaigns.models import Campaign
    from apps.messaging.models import Message

    campaign = Campaign.objects.filter(pk=campaign_id, tenant=tenant).first()
    if campaign is None:
        return {"found": False}

    rows = (
        Message.objects.filter(tenant=tenant, campaign=campaign)
        .values("status")
        .annotate(count=Count("id"))
    )
    return {
        "found": True,
        "id": campaign.pk,
        "name": campaign.name,
        "status": campaign.status,
        "created_at": campaign.created_at.isoformat(),
        "messages_by_status": {row["status"]: row["count"] for row in rows},
    }
