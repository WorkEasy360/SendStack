"""
The numbers whoever runs this platform needs, defined precisely enough to act on.

Every metric in this module has a written definition, and the definition is
returned alongside the number. That is not documentation politeness -- it is
the difference between a dashboard somebody can run a business on and a
dashboard somebody argues about. "Churn was 4% last month" is unusable until
everyone agrees whether a trial that ended counts, and the fastest way to make
them agree is to put the answer next to the number.

The definitions this module commits to, and the reasoning behind each:

**MRR** -- the sum of the recurring monthly price of every subscription
currently entitled to service. A yearly plan contributes one twelfth of its
price. **Trials contribute nothing**, because a trial is not revenue and
counting it is the single most common way a SaaS dashboard flatters itself.
Cancelled subscriptions still inside a paid period *do* contribute: the money
is taken and the service is being delivered. This is a snapshot of a run rate,
not a sum of payments received -- see `revenue_collected` for that, which is a
different number and is labelled as one.

**Churn** -- subscriptions that *ended*: cancelled or expired inside the
window. Deliberately **not** inactivity. A customer who has not logged in for
a month has not churned, they have gone quiet; conflating the two produces a
churn figure that moves when nothing commercial has happened. Quiet accounts
are reported separately, as a health signal, which is what they are.

**Activation** -- the workspace connected a number, had a contact it was
allowed to message, and got a message accepted by the provider. Owned by
`apps.onboarding.services.ACTIVATION_REQUIREMENTS`, read from there rather
than restated, so the funnel and the checklist cannot drift apart.

**Retention** -- reported as counts, never as a single percentage. Different
cohorts have different denominators, and one number labelled "retention" hides
which one was used.

What is deliberately absent: CAC and LTV. This deployment records no
acquisition spend, so a CAC would be an invented number wearing a currency
symbol, and an LTV computed from a few months of data is an extrapolation
presented as a measurement. Both are listed in the metrics dictionary as
"not available and why" rather than estimated. See docs/METRICS.md.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.billing.models import Checkout, Payment
from apps.onboarding.models import FunnelEvent, OnboardingState
from apps.subscriptions.models import Plan, Subscription
from apps.tenants.models import Tenant

logger = logging.getLogger("sendstack")

#: Days of no sending after which a workspace is called quiet. Not churn --
#: see the module docstring -- and reported as its own signal.
QUIET_DAYS = 30

#: The metric definitions, returned with the numbers. Kept beside the code that
#: computes them so a definition cannot drift from its implementation without
#: somebody editing the two lines next to each other.
DEFINITIONS = {
    "mrr": (
        "Sum of the monthly-equivalent price of every subscription currently entitled to service. "
        "Yearly plans contribute one twelfth. Trials contribute nothing. Cancelled subscriptions "
        "still inside a paid period are included, because the service is still being delivered."
    ),
    "arr": "MRR multiplied by twelve. A run rate, not a forecast and not booked revenue.",
    "arpu": "MRR divided by the number of paying subscriptions. Trials are in neither figure.",
    "paying_customers": "Subscriptions that are not trials and are entitled to service right now.",
    "trialing_customers": "Subscriptions in a trial that has not yet ended.",
    "new_subscriptions": "Subscriptions that first became paid inside the window.",
    "churned": (
        "Subscriptions that were cancelled or expired inside the window. Inactivity is not churn "
        "and is reported separately."
    ),
    "churn_rate": (
        "Churned in the window divided by the number of paying subscriptions at the start of it. "
        "Reported as null when that starting number is zero, rather than as 0%."
    ),
    "revenue_collected": (
        "Payments actually captured inside the window. A cash figure, not MRR: a yearly payment "
        "appears here in full, in the month it was taken."
    ),
    "failed_payments": "Payment attempts recorded as failed inside the window.",
    "activation_rate": (
        "Workspaces created in the window that reached activation, divided by workspaces created "
        "in the window. Activation is defined by apps.onboarding.services.ACTIVATION_REQUIREMENTS."
    ),
    "quiet_workspaces": (
        f"Workspaces with no message sent in the last {QUIET_DAYS} days. A health signal, not churn."
    ),
}


# ---------------------------------------------------------------------------
# Revenue
# ---------------------------------------------------------------------------

def _monthly_equivalent_minor(plan) -> Decimal:
    """
    One plan's contribution to MRR, in minor units.

    Yearly divided by twelve rather than by 365/30, because MRR is a monthly
    figure and twelve months make a year in every finance conversation this
    number will appear in.

    Returned as `Decimal` and only rounded at the end: dividing a yearly price
    by twelve in integers and then summing a hundred of them loses up to a
    hundred minor units, which is small, wrong, and exactly the kind of thing
    somebody spends an afternoon reconciling.
    """
    price = Decimal(int(plan.price_cents))
    if plan.interval == Plan.Interval.YEARLY:
        return price / Decimal(12)
    return price


def mrr(*, now=None) -> dict:
    """
    The current monthly run rate, and what it is made of.

    Computed by walking live subscriptions rather than by summing payments,
    because those are different questions: payments answer "what came in last
    month", MRR answers "what is currently contracted". A dashboard that
    labels the first as the second reports a spike every time an annual plan
    is sold.
    """
    now = now or timezone.now()

    subscriptions = (
        Subscription.objects.select_related("plan")
        # TRIALING is excluded here rather than filtered later: a trial has no
        # price and including it would require remembering to zero it.
        .exclude(status=Subscription.Status.TRIALING)
        .exclude(status=Subscription.Status.EXPIRED)
    )

    total = Decimal(0)
    by_plan = {}
    paying = 0
    currency = ""

    for subscription in subscriptions:
        # `access_state` rather than `status`: a PAST_DUE subscription inside
        # its grace window is being served and is still revenue; one past the
        # window is not. Asking the model keeps this in step with the send gate.
        if subscription.access_state(now) == Subscription.EXPIRED:
            continue
        contribution = _monthly_equivalent_minor(subscription.plan)
        if contribution <= 0:
            # A free plan is a real thing and contributes nothing. Counted as
            # a customer below only if it is being paid for, which it is not.
            continue
        total += contribution
        paying += 1
        currency = currency or (subscription.plan.currency or "").upper()
        row = by_plan.setdefault(
            subscription.plan.name, {"plan": subscription.plan.name, "subscriptions": 0, "mrr_minor": Decimal(0)},
        )
        row["subscriptions"] += 1
        row["mrr_minor"] += contribution

    mrr_minor = int(total.quantize(Decimal(1)))
    return {
        "mrr_minor": mrr_minor,
        "arr_minor": mrr_minor * 12,
        "currency": currency,
        "paying_customers": paying,
        "arpu_minor": int(mrr_minor / paying) if paying else 0,
        "by_plan": sorted(
            (
                {**row, "mrr_minor": int(row["mrr_minor"].quantize(Decimal(1)))}
                for row in by_plan.values()
            ),
            key=lambda row: -row["mrr_minor"],
        ),
        "trialing_customers": Subscription.objects.filter(
            status=Subscription.Status.TRIALING, trial_ends_at__gt=now,
        ).count(),
    }


def revenue_movements(*, since, until=None) -> dict:
    """
    What moved in the window: new, churned, and cash collected.

    Cash and MRR are reported side by side and labelled, because they answer
    different questions and putting one under the other's heading is the most
    common way a revenue dashboard misleads its own author.
    """
    until = until or timezone.now()

    new = Subscription.objects.filter(
        created_at__gte=since, created_at__lt=until,
    ).exclude(status=Subscription.Status.TRIALING).count()

    cancelled = Subscription.objects.filter(
        cancelled_at__gte=since, cancelled_at__lt=until,
    ).count()

    # An expiry has no dedicated timestamp -- the status moves and `updated_at`
    # records when. Approximate, and said so in the definition rather than
    # presented as exact.
    expired = Subscription.objects.filter(
        status=Subscription.Status.EXPIRED, updated_at__gte=since, updated_at__lt=until,
    ).count()

    captured = Payment.objects.filter(
        status=Payment.Status.CAPTURED, captured_at__gte=since, captured_at__lt=until,
    ).aggregate(total=Sum("amount_minor"), count=Count("id"))

    failed = Payment.objects.filter(
        status=Payment.Status.FAILED, created_at__gte=since, created_at__lt=until,
    ).count()

    # The denominator for churn: paying subscriptions that existed before the
    # window opened. Anything created inside it could not have churned out of
    # a population it was not in.
    base = Subscription.objects.filter(created_at__lt=since).exclude(
        status=Subscription.Status.TRIALING,
    ).count()
    churned = cancelled + expired

    return {
        "window_start": since,
        "window_end": until,
        "new_subscriptions": new,
        "cancellations": cancelled,
        "expirations": expired,
        "churned": churned,
        # None rather than 0 when there is nothing to divide by. A churn rate
        # of "0%" on a platform with no customers is a false reassurance.
        "churn_rate": round(churned / base * 100, 2) if base else None,
        "churn_base": base,
        "revenue_collected_minor": int(captured["total"] or 0),
        "payments_captured": int(captured["count"] or 0),
        "failed_payments": failed,
    }


def reconciliation(*, since, until=None, limit=50) -> dict:
    """
    Where this system's records and the payment provider's disagree.

    Three specific mismatches, each of which is a real bug or a real loss:

    * a **captured payment with no active subscription** -- somebody paid and
      did not get what they paid for, which is the one on this list a customer
      will notice;
    * a **checkout marked PAID with no captured payment** -- an activation that
      happened without money behind it;
    * a **checkout left pending past its expiry with a payment against it** --
      a settlement that never completed, usually a lost webhook.

    Reported for review rather than fixed automatically. Every one of these has
    more than one possible cause, and a job that guessed would eventually guess
    a refund into an activation.
    """
    until = until or timezone.now()

    paid_no_subscription = []
    payments = (
        Payment.objects.filter(
            status=Payment.Status.CAPTURED, captured_at__gte=since, captured_at__lt=until,
        )
        .select_related("tenant", "checkout__plan")[:500]
    )
    for payment in payments:
        subscription = Subscription.objects.filter(tenant_id=payment.tenant_id).first()
        if subscription is None or subscription.access_state(until) == Subscription.EXPIRED:
            paid_no_subscription.append({
                "tenant": payment.tenant_id,
                "payment": payment.pk,
                "provider_payment_id": payment.provider_payment_id,
                "amount_minor": payment.amount_minor,
                "captured_at": payment.captured_at,
            })
        if len(paid_no_subscription) >= limit:
            break

    paid_without_payment = list(
        Checkout.objects.filter(
            status=Checkout.Status.PAID, updated_at__gte=since, updated_at__lt=until,
        )
        .exclude(payments__status=Payment.Status.CAPTURED)
        .values("id", "tenant_id", "amount_minor", "currency", "updated_at")[:limit]
    )

    stuck = list(
        Checkout.objects.filter(
            status__in=(Checkout.Status.CREATED, Checkout.Status.PENDING),
            expires_at__lt=until,
            payments__isnull=False,
        )
        .distinct()
        .values("id", "tenant_id", "amount_minor", "currency", "expires_at")[:limit]
    )

    return {
        "window_start": since,
        "window_end": until,
        "captured_without_active_subscription": paid_no_subscription,
        "marked_paid_without_captured_payment": paid_without_payment,
        "pending_past_expiry_with_payment": stuck,
        "clean": not (paid_no_subscription or paid_without_payment or stuck),
    }


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

def activation(*, since, until=None) -> dict:
    """
    How many workspaces created in the window went on to actually use the product.

    Cohorted by **creation date**, not by activation date. Cohorting by
    activation would produce a rate that rises whenever an old customer
    finally gets going, which flatters the number and answers nothing about
    whether onboarding works.
    """
    until = until or timezone.now()

    created = Tenant.objects.filter(
        kind=Tenant.Kind.CUSTOMER, created_at__gte=since, created_at__lt=until,
    )
    signups = created.count()

    states = OnboardingState.objects.filter(tenant__in=created)
    return {
        "window_start": since,
        "window_end": until,
        "signups": signups,
        "connected_whatsapp": states.filter(whatsapp_connected_at__isnull=False).count(),
        "added_contacts": states.filter(contacts_added_at__isnull=False).count(),
        "sent_first_message": states.filter(test_message_sent_at__isnull=False).count(),
        "created_first_campaign": states.filter(first_campaign_created_at__isnull=False).count(),
        "sent_first_campaign": states.filter(first_campaign_sent_at__isnull=False).count(),
        "activated": states.filter(activated_at__isnull=False).count(),
        "activation_rate": (
            round(states.filter(activated_at__isnull=False).count() / signups * 100, 2)
            if signups else None
        ),
    }


def retention(*, since, until=None) -> dict:
    """
    New, active, returning and churned, as counts with stated definitions.

    "Active" here means *sent something* in the window. Logging in is not
    activity in a product whose value is delivered by sending: a workspace
    whose owner opens the dashboard daily and sends nothing is not retained,
    it is about to leave.
    """
    from apps.messaging.models import Message

    until = until or timezone.now()
    window = until - since

    def senders(start, end):
        return set(
            Message.objects.filter(queued_at__gte=start, queued_at__lt=end)
            .values_list("tenant_id", flat=True)
            .distinct()
        )

    current = senders(since, until)
    previous = senders(since - window, since)

    new_tenants = set(
        Tenant.objects.filter(
            kind=Tenant.Kind.CUSTOMER, created_at__gte=since, created_at__lt=until,
        ).values_list("id", flat=True)
    )

    return {
        "window_start": since,
        "window_end": until,
        "new_customers": len(new_tenants),
        "active_customers": len(current),
        # Sent in this window and the one before it. The number that says the
        # product is habitual rather than tried once.
        "returning_customers": len(current & previous),
        # Sent in the previous window and not in this one. Named "lapsed"
        # rather than "churned" on purpose: they may still be paying, and
        # calling this churn is exactly the conflation this module refuses.
        "lapsed_customers": len(previous - current),
        "previous_active": len(previous),
        "retention_rate": (
            round(len(current & previous) / len(previous) * 100, 2) if previous else None
        ),
    }


def health_signals(*, limit=50, now=None) -> dict:
    """
    Workspaces worth a human looking at, and why.

    Five signals, each chosen because it is **actionable and observable** --
    something has gone wrong that somebody could help with, and the product
    already knows about it. Deliberately not a behavioural profile: there is
    nothing here about how often somebody logs in, how long they stay, or what
    they look at, because that is surveillance rather than support and it does
    not tell anybody what to do next.
    """
    now = now or timezone.now()
    quiet_since = now - timedelta(days=QUIET_DAYS)

    from apps.messaging.models import Message
    from apps.whatsapp.models import WhatsAppAccount

    signals = {}

    # Signed up, never sent. The most fixable failure in the product.
    never_sent = (
        OnboardingState.objects.filter(
            activated_at__isnull=True, created_at__lt=now - timedelta(days=3),
        )
        .select_related("tenant")
        .order_by("-created_at")[:limit]
    )
    signals["never_activated"] = [
        {
            "tenant": state.tenant_id,
            "name": state.tenant.name,
            "created_at": state.created_at,
            "next_step": state.next_step(),
        }
        for state in never_sent
    ]

    # Was sending, stopped. Different from never started, and needs a
    # different conversation.
    active_tenants = set(
        Message.objects.filter(queued_at__gte=quiet_since).values_list("tenant_id", flat=True).distinct()
    )
    ever_sent = set(
        Message.objects.filter(queued_at__lt=quiet_since).values_list("tenant_id", flat=True).distinct()
    )
    quiet_ids = list(ever_sent - active_tenants)[:limit]
    signals["gone_quiet"] = [
        {"tenant": tenant.pk, "name": tenant.name, "quiet_since_days": QUIET_DAYS}
        for tenant in Tenant.objects.filter(pk__in=quiet_ids)
    ]

    # `WhatsAppAccount.tenant` is nullable -- it is backfilled from `user` and
    # a row can predate that. Filtered rather than guarded per row: a device
    # with no workspace cannot be actioned by anybody, so it does not belong on
    # a list whose entire purpose is telling somebody who to contact.
    signals["device_disconnected"] = [
        {"tenant": account.tenant_id, "name": account.tenant.name, "device": account.name}
        for account in WhatsAppAccount.objects.filter(
            status=WhatsAppAccount.Status.DISCONNECTED, tenant__isnull=False,
        ).select_related("tenant")[:limit]
    ]

    ending = now + timedelta(days=3)
    signals["trial_ending"] = [
        {
            "tenant": subscription.tenant_id,
            "name": subscription.tenant.name,
            "ends_at": subscription.trial_ends_at,
            "days_remaining": subscription.trial_days_remaining(),
        }
        for subscription in Subscription.objects.filter(
            status=Subscription.Status.TRIALING,
            trial_ends_at__gt=now,
            trial_ends_at__lte=ending,
        ).select_related("tenant")[:limit]
    ]

    failing = (
        Message.objects.filter(status=Message.Status.FAILED, queued_at__gte=now - timedelta(days=7))
        .values("tenant_id")
        .annotate(failures=Count("id"))
        .filter(failures__gte=20)
        .order_by("-failures")[:limit]
    )
    names = dict(
        Tenant.objects.filter(pk__in=[row["tenant_id"] for row in failing]).values_list("id", "name")
    )
    signals["repeated_failures"] = [
        {"tenant": row["tenant_id"], "name": names.get(row["tenant_id"], ""), "failures_7d": row["failures"]}
        for row in failing
    ]

    return {
        "generated_at": now,
        "signals": signals,
        "counts": {key: len(value) for key, value in signals.items()},
    }


# ---------------------------------------------------------------------------
# The one payload the executive dashboard renders
# ---------------------------------------------------------------------------

def executive(*, days=30, now=None) -> dict:
    """
    Everything the business dashboard shows, in one response.

    One endpoint rather than nine, because a dashboard that fires nine requests
    to paint one screen is nine chances to be slow and nine places for a
    partial render. The queries here are aggregates over tables that are
    indexed for them; the expensive one is `mrr`, which walks live
    subscriptions and is bounded by the number of paying customers rather than
    by the size of the message table.
    """
    now = now or timezone.now()
    since = now - timedelta(days=days)

    return {
        "window_days": days,
        "generated_at": now,
        "revenue": {**mrr(now=now), **revenue_movements(since=since, until=now)},
        "activation": activation(since=since, until=now),
        "retention": retention(since=since, until=now),
        "volume": _volume(since=since, until=now),
        "funnel": _funnel(since=since),
        # Returned with the numbers, not linked from a wiki. See the module
        # docstring for why.
        "definitions": DEFINITIONS,
    }


def _volume(*, since, until) -> dict:
    """Campaign and message volume, with the failure rate that qualifies it."""
    from apps.campaigns.models import Campaign
    from apps.messaging.models import Message

    messages = Message.objects.filter(queued_at__gte=since, queued_at__lt=until)
    counts = messages.aggregate(
        total=Count("id"),
        sent=Count("id", filter=Q(status__in=(
            Message.Status.SENT, Message.Status.DELIVERED, Message.Status.READ,
        ))),
        failed=Count("id", filter=Q(status=Message.Status.FAILED)),
        skipped=Count("id", filter=Q(status=Message.Status.SKIPPED)),
    )
    total = counts["total"] or 0
    return {
        "campaigns": Campaign.objects.filter(created_at__gte=since, created_at__lt=until).count(),
        "messages": total,
        "sent": counts["sent"] or 0,
        "failed": counts["failed"] or 0,
        "skipped": counts["skipped"] or 0,
        # None rather than 0 on an empty window: a 0% failure rate over no
        # messages reads as healthy and means nothing.
        "failure_rate": round((counts["failed"] or 0) / total * 100, 2) if total else None,
    }


def _funnel(*, since) -> list:
    """
    The acquisition funnel, as workspace counts per stage.

    Counts distinct workspaces rather than events, so one workspace's three
    upgrade attempts do not read as three customers. Delegates to the
    onboarding service, which owns the event log.
    """
    from apps.onboarding import services as onboarding

    counts = onboarding.funnel_counts(since=since)
    stages = (
        FunnelEvent.Name.SIGNUP_COMPLETED,
        FunnelEvent.Name.WHATSAPP_CONNECTED,
        FunnelEvent.Name.CONTACT_ADDED,
        FunnelEvent.Name.TEST_MESSAGE_SENT,
        FunnelEvent.Name.FIRST_CAMPAIGN_SENT,
        FunnelEvent.Name.ACTIVATED,
        FunnelEvent.Name.PAYMENT_COMPLETED,
    )
    labels = dict(FunnelEvent.Name.choices)
    return [
        {
            "name": stage,
            "label": labels.get(stage, stage),
            "workspaces": counts.get(stage, {}).get("workspaces", 0),
        }
        for stage in stages
    ]
