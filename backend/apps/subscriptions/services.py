"""
What a tenant is entitled to, what they have used, and whether the next thing
is allowed.

Usage is counted from the underlying rows every time rather than kept in a
tally, for the same reason agent performance is: a counter that disagrees with
the message list is worse than no counter, and there is no cheap way to keep
one honest across retries, deletions and a period that rolls.
"""

import logging

from .models import Plan, Subscription

logger = logging.getLogger("multisender")

UNLIMITED = 0


def subscription_for(tenant_id):
    """The tenant's subscription, or None if nobody has put them on a plan."""
    if not tenant_id:
        return None
    return (
        Subscription.objects.filter(tenant_id=tenant_id).select_related("plan", "tenant").first()
    )


def plan_for(tenant_id):
    subscription = subscription_for(tenant_id)
    return subscription.plan if subscription else None


def usage(tenant_id, subscription=None) -> dict:
    """
    What this tenant has consumed in the current period.

    Messages are counted from the period start; the rest are simply what
    exists now, because a contact or a device is a thing you hold rather than
    a thing you spend.
    """
    from apps.contacts.models import Contact
    from apps.messaging.models import Message
    from apps.whatsapp.models import WhatsAppAccount
    from django.contrib.auth import get_user_model

    subscription = subscription or subscription_for(tenant_id)

    messages = Message.objects.filter(tenant_id=tenant_id, direction=Message.Direction.OUTBOUND)
    if subscription is not None:
        messages = messages.filter(queued_at__gte=subscription.current_period_start)

    return {
        "messages_per_month": messages.count(),
        "contacts": Contact.objects.filter(tenant_id=tenant_id).count(),
        "devices": WhatsAppAccount.objects.filter(tenant_id=tenant_id).count(),
        "users": get_user_model().objects.filter(tenant_id=tenant_id, is_active=True).count(),
    }


def quota_state(tenant_id) -> dict:
    """
    Limits, usage and what remains, in one shape the UI can render directly.

    A tenant on no plan is reported as unlimited rather than as blocked: the
    product has always worked without subscriptions, and switching them on for
    one customer must not silently stop everyone else.
    """
    subscription = subscription_for(tenant_id)
    plan = subscription.plan if subscription else None
    limits = plan.limits if plan else {key: UNLIMITED for key in ("messages_per_month", "contacts", "devices", "users")}
    used = usage(tenant_id, subscription)

    metrics = {}
    for key, limit in limits.items():
        consumed = used.get(key, 0)
        metrics[key] = {
            "limit": limit,
            "used": consumed,
            "remaining": None if limit == UNLIMITED else max(limit - consumed, 0),
            "exceeded": limit != UNLIMITED and consumed >= limit,
            "percent": None if limit == UNLIMITED else min(round(consumed / limit * 100), 100) if limit else None,
        }

    return {
        "plan": (
            {
                "id": plan.pk,
                "name": plan.name,
                "interval": plan.interval,
                "price_cents": plan.price_cents,
                "currency": plan.currency,
                "features": {feature: plan.allows(feature) for feature in Plan.FEATURES},
            }
            if plan
            else None
        ),
        "status": subscription.status if subscription else None,
        "current_period_start": subscription.current_period_start if subscription else None,
        "current_period_end": subscription.current_period_end if subscription else None,
        "metrics": metrics,
    }


def check_quota(tenant_id, metric: str, *, additional: int = 1):
    """
    Whether one more of `metric` is within the plan.

    Returns `(allowed, reason)`. Everything unknown resolves to allowed: a
    tenant with no subscription, a metric no plan constrains, or a lookup that
    fails are all reasons to let the work through rather than to stop a
    customer's messages over a billing table.
    """
    try:
        subscription = subscription_for(tenant_id)
        if subscription is None:
            return True, ""

        if not subscription.is_live:
            return False, f"This workspace's subscription is {subscription.get_status_display().lower()}."

        limit = subscription.plan.limits.get(metric, UNLIMITED)
        if limit == UNLIMITED:
            return True, ""

        used = usage(tenant_id, subscription).get(metric, 0)
        if used + additional > limit:
            return False, f"The {subscription.plan.name} plan allows {limit:,} {metric.replace('_', ' ')}."
        return True, ""
    except Exception:
        logger.exception("Quota check failed for tenant %s; allowing the work through.", tenant_id)
        return True, ""


def feature_enabled(tenant_id, feature: str) -> bool:
    """Whether a plan opens this part of the product. No plan means everything."""
    plan = plan_for(tenant_id)
    return plan.allows(feature) if plan else True
