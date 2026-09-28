"""
What a tenant is entitled to, what they have used, and whether the next thing
is allowed.

**This module is the only place that answers those questions.** Views, Celery
tasks, signal receivers and service functions all come here; none of them
reads `Plan` or `Subscription` for themselves. That is the whole point -- an
entitlement enforced in the viewset and nowhere else is enforced nowhere,
because the same work can arrive from a workflow, an import, a webhook or a
worker that never touches a view.

Usage is counted from the underlying rows every time rather than kept in a
tally, for the same reason agent performance is: a counter that disagrees with
the message list is worse than no counter. The one exception is the monthly
message allowance, which *is* stamped on the message it was spent on
(`Message.plan_counted_at`) -- see `claim_message_allowance` for why counting
alone cannot be made race-free.

Two rules run through everything here:

* **Fail closed.** If the plan cannot be read, nothing may be spent against
  it. A billing table that is down is a reason to stop, not a reason to hand
  out the product. Read-only *display* is the exception and says so.
* **Zero means unlimited**, the same convention team capacity and reply caps
  already use.
"""

import logging
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.db import transaction

from .entitlements import EntitlementUnavailable, FeatureNotAvailable, QuotaExceeded
from .models import Plan, Subscription

logger = logging.getLogger("sendstack")

UNLIMITED = 0

#: The canonical metric names, which are `Plan.limits`' keys.
METRICS = ("messages_per_month", "contacts", "devices", "users")

#: The column names read the other way round. Callers say either; a plan field
#: is never renamed to suit a caller.
_ALIASES = {
    "max_messages_per_month": "messages_per_month",
    "messages": "messages_per_month",
    "max_contacts": "contacts",
    "max_devices": "devices",
    "max_users": "users",
}

#: Tenant kinds that operate the platform rather than buy from it. A reseller
#: with no subscription of their own is running the shop, not shopping in it,
#: so the *fallback* below never applies to them. A reseller a superadmin has
#: deliberately put on a plan is governed by that plan like anybody else.
_OPERATOR_KINDS = ("PLATFORM", "RESELLER")


def metric_name(name: str) -> str:
    return _ALIASES.get(name, name)


def _tenant_id(tenant):
    """Accept a Tenant, a tenant id, or anything with `.tenant_id`."""
    if tenant is None:
        return None
    if isinstance(tenant, int):
        return tenant
    return getattr(tenant, "pk", None) or getattr(tenant, "tenant_id", None)


# ---------------------------------------------------------------------------
# The plan behind a tenant
# ---------------------------------------------------------------------------

def get_active_subscription(tenant):
    """
    The tenant's subscription, or None if nobody has put them on a plan.

    "Active" here means *the one in force*, whatever its status -- a cancelled
    subscription is still the subscription that decides what happens next, and
    hiding it would silently promote the tenant to unlimited.
    """
    tenant_id = _tenant_id(tenant)
    if not tenant_id:
        return None
    return (
        Subscription.objects.filter(tenant_id=tenant_id).select_related("plan", "tenant").first()
    )


#: Kept: `subscription_for` is what the rest of the codebase already calls.
subscription_for = get_active_subscription


def default_plan():
    """
    The plan a customer with no subscription falls back to, if one is named.

    ``BILLING_DEFAULT_PLAN`` holds a platform-owned plan's name. Unset -- the
    default -- means no fallback and an unsubscribed customer is unlimited,
    which is how this product has always worked and what every tenant created
    before Phase 2 depends on. A deployment that wants unsubscribed tenants
    restricted names a plan here (and `manage.py assign_default_plan` writes
    real subscriptions for the tenants that have none).

    Never invents a plan and never picks one by guessing: a name that matches
    nothing falls back to unlimited rather than to whatever sorted first,
    because accidentally handing every customer the Enterprise plan is a worse
    failure than handing them today's behaviour.
    """
    name = (getattr(settings, "BILLING_DEFAULT_PLAN", "") or "").strip()
    if not name:
        return None
    return Plan.objects.filter(owner__isnull=True, name=name, is_active=True).first()


def get_current_plan(tenant):
    """The plan whose limits and features apply to this tenant, or None for unlimited."""
    subscription = get_active_subscription(tenant)
    if subscription is not None:
        return subscription.plan

    tenant_obj = tenant if hasattr(tenant, "kind") else None
    if tenant_obj is None:
        from apps.tenants.models import Tenant

        tenant_obj = Tenant.objects.filter(pk=_tenant_id(tenant)).first()
    if tenant_obj is not None and tenant_obj.kind in _OPERATOR_KINDS:
        return None

    return default_plan()


#: Kept for existing callers.
plan_for = get_current_plan


def access_state(tenant, subscription=None):
    """
    ``FULL`` / ``GRACE`` / ``EXPIRED``, or None when no subscription governs.

    None is not a state a customer is in -- it means the question does not
    apply, which is what an unsubscribed tenant and every operator tenant look
    like.
    """
    subscription = subscription if subscription is not None else get_active_subscription(tenant)
    return subscription.access_state() if subscription is not None else None


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

def usage(tenant, subscription=None) -> dict:
    """
    What this tenant has consumed in the current period.

    Messages are counted over the current billing window; the rest are simply
    what exists now, because a contact or a device is a thing you hold rather
    than a thing you spend.

    Every count is an aggregate in the database -- four `COUNT(*)`s, no rows
    loaded -- because this runs on the send path and on every dashboard load.
    """
    from django.contrib.auth import get_user_model

    from apps.contacts.models import Contact
    from apps.messaging.models import Message
    from apps.whatsapp.models import WhatsAppAccount

    tenant_id = _tenant_id(tenant)
    subscription = subscription if subscription is not None else get_active_subscription(tenant_id)

    return {
        "messages_per_month": messages_used(tenant_id, subscription),
        "contacts": Contact.objects.filter(tenant_id=tenant_id).count(),
        "devices": WhatsAppAccount.objects.filter(tenant_id=tenant_id).count(),
        # Deliberately narrow, and documented in docs/ENTITLEMENTS.md: the
        # users *of this tenant* that can currently sign in. A superadmin, a
        # reseller and another tenant's staff are all somebody else's row.
        "users": get_user_model().objects.filter(
            tenant_id=tenant_id, is_active=True, is_deleted=False,
        ).count(),
    }


#: Kept: `get_usage` is the name Phase 2 asked for, `usage` is the name the
#: codebase already calls. Same function.
get_usage = usage


def calendar_month(now=None):
    """
    This calendar month, as a half-open window.

    The fallback period for a tenant governed by a plan but holding no
    subscription -- there is no anchor date to measure from, and "messages per
    month" with no month at all would mean *every message ever sent*, which
    would block such a tenant permanently the first time they crossed the
    limit rather than for the rest of one month.
    """
    from django.utils import timezone

    now = now or timezone.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


def current_period(tenant, subscription=None, plan=None):
    """
    The window message usage is measured over.

    `(None, None)` when nothing bounds it -- a tenant on no plan at all, whose
    usage is unlimited and whose count is therefore not a limit on anything.
    """
    subscription = subscription if subscription is not None else get_active_subscription(tenant)
    if subscription is not None:
        return subscription.current_period()
    if (plan if plan is not None else get_current_plan(tenant)) is not None:
        return calendar_month()
    return None, None


def messages_used(tenant, subscription=None) -> int:
    """
    Messages charged against the plan in the current period.

    Counts the stamp, not the status: a message counts from the moment its
    allowance was claimed (`claim_message_allowance`) until that claim is
    given back (`release_message_allowance`). Counting statuses instead is
    what made this wrong before -- every message a campaign queued was
    counted the instant it was queued, so a plan allowing 100 messages a month
    let a campaign of 100 send exactly none of them.
    """
    from apps.messaging.models import Message

    tenant_id = _tenant_id(tenant)
    queryset = Message.objects.filter(
        tenant_id=tenant_id,
        direction=Message.Direction.OUTBOUND,
        plan_counted_at__isnull=False,
    )
    start, end = current_period(tenant_id, subscription)
    if start is not None:
        queryset = queryset.filter(plan_counted_at__gte=start, plan_counted_at__lt=end)
    return queryset.count()


# ---------------------------------------------------------------------------
# The questions everything else asks
# ---------------------------------------------------------------------------

def limits_for(tenant, subscription=None, plan=None) -> dict:
    """
    This tenant's ceilings, with EXPIRED collapsing every one of them to nothing.

    Pass both `subscription` and `plan` when you already have them -- the send
    path does -- and nothing is looked up again.
    """
    if plan is None:
        subscription = subscription if subscription is not None else get_active_subscription(tenant)
        plan = subscription.plan if subscription is not None else get_current_plan(tenant)

    if subscription is not None and subscription.access_state() == Subscription.EXPIRED:
        # Not "unlimited by another name": an expired subscription grants no
        # paid entitlements, and 0 means unlimited everywhere else in this
        # product, so the refusal has to come from `check_quota` instead.
        return {key: 0 for key in METRICS}

    return plan.limits if plan is not None else {key: UNLIMITED for key in METRICS}


def check_quota(tenant, metric: str, requested_amount: int = 1, *, additional: int = None):
    """
    Whether `requested_amount` more of `metric` is within the plan.

    Returns ``(allowed, reason)``; `reason` is a sentence written for the
    person who will read it, naming their plan and their numbers and no
    database detail.

    **Fails closed.** Before Phase 2 every unknown resolved to allowed,
    including an exception, on the reasoning that a billing table should never
    stop a customer's messages. That reasoning stops holding the moment the
    plan is what somebody is paying for: a lookup that raises is a lookup that
    cannot say the resource is free, and handing out the paid resource anyway
    is handing out the product. A tenant genuinely on **no plan** is still
    allowed -- that is a known answer, not an unknown one.
    """
    if additional is not None:  # the pre-Phase-2 keyword
        requested_amount = additional
    metric = metric_name(metric)

    try:
        tenant_id = _tenant_id(tenant)
        subscription = get_active_subscription(tenant_id)
        plan = subscription.plan if subscription is not None else get_current_plan(tenant)

        if plan is None:
            return True, ""

        if subscription is not None:
            state = subscription.access_state()
            if state == Subscription.EXPIRED:
                return False, _lapsed_reason(subscription)

        limit = limits_for(tenant_id, subscription, plan).get(metric, UNLIMITED)
        if limit == UNLIMITED:
            return True, ""

        used = usage(tenant_id, subscription).get(metric, 0)
        if used + max(requested_amount, 0) > limit:
            return False, _quota_reason(plan, metric, limit, used, requested_amount)
        return True, ""
    except Exception:
        logger.exception("Quota check failed for tenant %s; refusing the work.", _tenant_id(tenant))
        return False, EntitlementUnavailable.default_detail


def _lapsed_reason(subscription) -> str:
    if subscription.status == Subscription.Status.CANCELLED:
        return "This workspace's subscription has been cancelled and its billing period has ended."
    if subscription.status == Subscription.Status.PAST_DUE:
        return "This workspace's subscription is past due and its grace period has ended."
    return f"This workspace's subscription is {subscription.get_status_display().lower()}."


#: How each metric reads in a sentence aimed at a person.
_METRIC_NOUNS = {
    "messages_per_month": ("messages this month", "sent"),
    "contacts": ("contacts", "have"),
    "devices": ("WhatsApp devices", "have"),
    "users": ("team members", "have"),
}


def _quota_reason(plan, metric, limit, used, requested) -> str:
    noun, verb = _METRIC_NOUNS.get(metric, (metric.replace("_", " "), "have"))
    remaining = max(limit - used, 0)
    lines = [f"The {plan.name} plan allows {limit:,} {noun}, and you {verb} {used:,}."]
    if requested > 1:
        lines.append(
            f"This would add {requested:,} more; there is room for {remaining:,}."
            if remaining
            else "There is no room left."
        )
    lines.append("Upgrade your plan to raise the limit.")
    return " ".join(lines)


def require_quota(tenant, metric: str, requested_amount: int = 1) -> None:
    """`check_quota`, but raising the DRF error instead of returning a pair."""
    allowed, reason = check_quota(tenant, metric, requested_amount)
    if not allowed:
        if reason == EntitlementUnavailable.default_detail:
            raise EntitlementUnavailable()
        raise QuotaExceeded(metric_name(metric), reason)


def feature_enabled(tenant, feature: str) -> bool:
    """
    Whether a plan opens this part of the product.

    No plan means everything, an expired subscription means nothing, and a
    lookup that fails means nothing either -- the same fail-closed rule as
    `check_quota`, for the same reason.
    """
    try:
        subscription = get_active_subscription(tenant)
        if subscription is not None:
            if subscription.access_state() == Subscription.EXPIRED:
                return False
            return subscription.plan.allows(feature)

        plan = get_current_plan(tenant)
        return plan.allows(feature) if plan is not None else True
    except Exception:
        logger.exception("Feature check failed for tenant %s; refusing %s.", _tenant_id(tenant), feature)
        return False


def require_feature(tenant, feature: str) -> None:
    """`feature_enabled`, but raising the DRF error. Used at every write/execute entry point."""
    if not feature_enabled(tenant, feature):
        raise FeatureNotAvailable(feature, feature_message(tenant, feature))


#: How each switch reads to the person who does not have it.
_FEATURE_LABELS = {
    "ai_agent": "AI Agent",
    "workflows": "Workflows",
    "api_access": "API access",
    "click_tracking": "Click tracking",
    "number_filter": "Number filter",
    "group_grabber": "Group grabber",
}


def feature_label(feature: str) -> str:
    return _FEATURE_LABELS.get(feature, feature.replace("_", " ").title())


def feature_message(tenant, feature: str) -> str:
    plan = None
    try:
        plan = get_current_plan(tenant)
    except Exception:  # noqa: BLE001 - wording must never be the thing that fails
        logger.exception("Could not name the plan for tenant %s.", _tenant_id(tenant))
    label = feature_label(feature)
    if plan is not None:
        return f"{label} is not included in your {plan.name} plan. Upgrade your plan to unlock it."
    return f"{label} is not included in your current plan."


# ---------------------------------------------------------------------------
# Serialising a quota-consuming create against the concurrent one
# ---------------------------------------------------------------------------

def _lock_tenant(tenant_id):
    """
    Take the tenant's row, so two creates for one tenant cannot both see the
    last free slot.

    The tenant row is the lock rather than the subscription row because it is
    the one that always exists -- a tenant with no subscription still has to
    serialise against itself once a default plan is configured -- and because
    it is written approximately never, so nothing else contends for it.
    """
    from apps.tenants.models import Tenant

    return Tenant.objects.select_for_update().filter(pk=tenant_id).first()


@contextmanager
def quota_guard(tenant, metric: str, requested_amount: int = 1):
    """
    Check a limit and hold it until the caller has created the thing.

    ::

        with quota_guard(tenant, "devices"):
            serializer.save(tenant=tenant)

    The check and the create share one transaction and one row lock, which is
    what makes "two requests, one slot left, one succeeds" true rather than
    aspirational. Raises `QuotaExceeded` (403) or `EntitlementUnavailable`
    (503) before the body runs; a body that raises rolls the whole thing back,
    lock included, so a failed create never eats a slot.
    """
    tenant_id = _tenant_id(tenant)
    with transaction.atomic():
        if tenant_id:
            _lock_tenant(tenant_id)
        require_quota(tenant_id, metric, requested_amount)
        yield


# ---------------------------------------------------------------------------
# The monthly message allowance
# ---------------------------------------------------------------------------

def claim_message_allowance(message, now=None):
    """
    Take one message off this tenant's monthly allowance. Returns ``(ok, reason)``.

    Counting sent messages and comparing against a limit is not enough on its
    own: the count and the send are two steps, and between them any number of
    workers can read the same free slot. So the allowance is *claimed* --
    stamped onto the message row -- inside the same transaction as the count,
    under the tenant's lock. A second worker blocks on that lock and, when it
    proceeds, its count already includes the first worker's message.

    Idempotent by construction: a message that already carries a stamp has
    already spent its slot, so a hold, a retry or a redelivered task claims
    nothing further. The stamp is given back by `release_message_allowance`
    wherever the credit reservation is given back, so a send that never
    happened costs a customer neither a credit nor a message.
    """
    from django.utils import timezone

    from apps.messaging.models import Message

    now = now or timezone.now()
    try:
        tenant_id = message.tenant_id
        if message.plan_counted_at is not None:
            return True, ""  # already paid for out of this period's allowance

        subscription = get_active_subscription(tenant_id)
        plan = subscription.plan if subscription is not None else get_current_plan(tenant_id)
        if plan is None:
            return True, ""

        if subscription is not None and subscription.access_state() == Subscription.EXPIRED:
            return False, _lapsed_reason(subscription)

        limit = limits_for(tenant_id, subscription, plan).get("messages_per_month", UNLIMITED)
        if limit == UNLIMITED:
            return True, ""

        with transaction.atomic():
            _lock_tenant(tenant_id)
            used = messages_used(tenant_id, subscription)
            if used + 1 > limit:
                return False, _quota_reason(plan, "messages_per_month", limit, used, 1)
            # Conditional so a concurrent claim on the same row cannot stamp
            # it twice, and so the count above stays the count that decided.
            stamped = Message.objects.filter(pk=message.pk, plan_counted_at__isnull=True).update(
                plan_counted_at=now
            )
            if stamped:
                message.plan_counted_at = now
        return True, ""
    except Exception:
        logger.exception(
            "Could not claim the monthly allowance for message %s; refusing the send.", getattr(message, "pk", "?"),
        )
        return False, EntitlementUnavailable.default_detail


def release_message_allowance(message) -> None:
    """
    Give a claimed slot back, for a message that never went out.

    The mirror of `apps.credits.services.release_credits`, and called from the
    same three places: a blocked send, a send with nowhere to go, and a
    terminal failure. Best-effort -- a slot that fails to come back is a
    customer being charged for a message they did not send, which is worth a
    log line but is never worth failing the surrounding cleanup over.
    """
    from apps.messaging.models import Message

    try:
        if getattr(message, "plan_counted_at", None) is None:
            return
        Message.objects.filter(pk=message.pk).update(plan_counted_at=None)
        message.plan_counted_at = None
    except Exception:
        logger.exception("Could not release the monthly allowance for message %s.", getattr(message, "pk", "?"))


# ---------------------------------------------------------------------------
# What the UI renders
# ---------------------------------------------------------------------------

def _metric_block(limit, used) -> dict:
    unlimited = limit == UNLIMITED
    return {
        "used": used,
        "limit": limit,
        "unlimited": unlimited,
        "remaining": None if unlimited else max(limit - used, 0),
        "exceeded": (not unlimited) and used >= limit,
        "over_limit": (not unlimited) and used > limit,
        "percentage": None if unlimited else min(round(used / limit * 100), 100),
        # Kept under its old name too: `percent` is what quota_state returned
        # before Phase 2 and what any existing caller reads.
        "percent": None if unlimited else min(round(used / limit * 100), 100),
    }


def get_entitlements(tenant) -> dict:
    """
    Plan, subscription, limits, usage and features, in one shape the UI can
    render directly and one query pass.

    This is a **read**, so it does not fail closed: a dashboard that cannot
    count contacts should say so in one panel, not take the page down and
    certainly not stop the product. The security boundary is `check_quota` and
    `feature_enabled`, which do fail closed; nothing here authorises anything.
    """
    tenant_id = _tenant_id(tenant)
    subscription = get_active_subscription(tenant_id)
    plan = subscription.plan if subscription is not None else get_current_plan(tenant_id)
    state = subscription.access_state() if subscription is not None else None

    limits = limits_for(tenant_id, subscription, plan)
    try:
        used = usage(tenant_id, subscription)
        degraded = False
    except Exception:
        logger.exception("Could not read usage for tenant %s; reporting it as unavailable.", tenant_id)
        used = {key: 0 for key in METRICS}
        degraded = True

    metrics = {key: _metric_block(limits.get(key, UNLIMITED), used.get(key, 0)) for key in METRICS}
    period_start, period_end = current_period(tenant_id, subscription, plan)

    features = (
        {feature: False for feature in Plan.FEATURES}
        if state == Subscription.EXPIRED
        else {feature: plan.allows(feature) for feature in Plan.FEATURES}
        if plan is not None
        else {feature: True for feature in Plan.FEATURES}
    )

    return {
        "plan": (
            {
                "id": plan.pk,
                "name": plan.name,
                "description": plan.description,
                "interval": plan.interval,
                "price_cents": plan.price_cents,
                "currency": plan.currency,
                "features": features,
                "limits": plan.limits,
            }
            if plan
            else None
        ),
        "subscription": (
            {
                "status": subscription.status,
                "status_display": subscription.get_status_display(),
                "access": state,
                "current_period_start": period_start,
                "current_period_end": period_end,
                "cancelled_at": subscription.cancelled_at,
            }
            if subscription
            else None
        ),
        # Flat aliases, kept because the pre-Phase-2 payload had them at the
        # top level and the reseller screens read them there.
        "status": subscription.status if subscription else None,
        "access": state,
        "current_period_start": period_start,
        "current_period_end": period_end,
        "usage": {
            "messages": metrics["messages_per_month"],
            "contacts": metrics["contacts"],
            "devices": metrics["devices"],
            "users": metrics["users"],
        },
        "metrics": metrics,
        "features": features,
        "over_limit": sorted(key for key, block in metrics.items() if block["over_limit"]),
        "usage_available": not degraded,
    }


#: Kept: `quota_state` is the pre-Phase-2 name for this payload.
quota_state = get_entitlements
