"""
What changing plan would actually do, worked out before anybody commits to it.

A customer pressing Upgrade or Downgrade is agreeing to something, and the
three things they need to know are what it costs, what it changes, and when.
Before this module, the upgrade screen could say the new price and nothing
else -- so a downgrade was a leap: new limits unknown, effective date unknown,
and no answer at all to the question everybody actually asks, which is *what
happens to the contacts I already have that the smaller plan does not allow*.

The answer to that last one is the reason this file is longer than a price
subtraction. **Nothing is deleted.** A workspace that drops to a plan allowing
2,000 contacts while holding 3,400 keeps all 3,400; what it loses is the
ability to add the 3,401st. `preview` says so explicitly, per metric, with the
numbers -- because a customer who finds that out afterwards has been surprised
by their billing, and a customer who finds it out here has made a decision.

Everything here is **read-only and derived**: it writes nothing, and every
number comes from the same `Plan` rows and the same usage query the send gate
enforces. A preview that computed its own version of the limits would
eventually disagree with the thing doing the enforcing, and the disagreement
would surface as a customer being blocked from something the page had just
promised.
"""

from django.utils import timezone

from apps.subscriptions import services as entitlements
from apps.subscriptions.models import Subscription

#: Metric key -> how to say it to somebody who does not read schemas.
METRIC_LABELS = {
    "messages_per_month": "messages a month",
    "contacts": "contacts",
    "devices": "WhatsApp numbers",
    "users": "team members",
}

#: What kind of move this is, from the customer's point of view.
UPGRADE = "UPGRADE"
DOWNGRADE = "DOWNGRADE"
LATERAL = "LATERAL"  # same price, different shape -- e.g. monthly to yearly at parity
SAME = "SAME"


def _direction(current_plan, new_plan) -> str:
    if current_plan is None:
        return UPGRADE
    if current_plan.pk == new_plan.pk:
        return SAME
    # Compared on price rather than on limits, because price is what the
    # customer is deciding about and a plan can raise one limit while lowering
    # another. The per-metric detail below says what actually moves.
    if new_plan.price_cents > current_plan.price_cents:
        return UPGRADE
    if new_plan.price_cents < current_plan.price_cents:
        return DOWNGRADE
    return LATERAL


def _limit_change(metric, current_limit, new_limit, used) -> dict:
    """
    One metric's before, after, and what it means for what is already there.

    0 is unlimited throughout this codebase, and it has to be handled at both
    ends: moving from 0 to 5,000 is a *reduction* even though the number went
    up, and moving to 0 is never a problem however much is in use.
    """
    current_unlimited = current_limit == 0
    new_unlimited = new_limit == 0

    if new_unlimited:
        reduced = False
        over = 0
    elif current_unlimited:
        reduced = True
        over = max(used - new_limit, 0)
    else:
        reduced = new_limit < current_limit
        over = max(used - new_limit, 0)

    return {
        "metric": metric,
        "label": METRIC_LABELS.get(metric, metric),
        "current_limit": current_limit,
        "new_limit": new_limit,
        "current_usage": used,
        "reduced": reduced,
        # How far over the new ceiling this workspace already is. The number
        # that matters on a downgrade, and the one the UI puts in a warning.
        "over_by": over,
        "would_block_new": over > 0,
    }


def preview(*, tenant, new_plan, now=None) -> dict:
    """
    A complete, honest answer to "what happens if I move to this plan?".

    Returns the current plan, the new one, the price difference, every limit
    that moves, what that means for usage already in place, and when it takes
    effect. Nothing is written.
    """
    now = now or timezone.now()
    tenant_id = getattr(tenant, "pk", tenant)

    subscription = entitlements.get_active_subscription(tenant_id)
    current_plan = subscription.plan if subscription is not None else None
    usage = entitlements.usage(tenant_id, subscription)

    direction = _direction(current_plan, new_plan)

    current_limits = current_plan.limits if current_plan is not None else dict.fromkeys(METRIC_LABELS, 0)
    new_limits = new_plan.limits

    limits = [
        _limit_change(
            metric,
            int(current_limits.get(metric, 0)),
            int(new_limits.get(metric, 0)),
            int(usage.get(metric, 0)),
        )
        for metric in METRIC_LABELS
    ]

    features = _feature_changes(current_plan, new_plan)
    period_end = subscription.current_period()[1] if subscription is not None else None

    return {
        "direction": direction,
        "current": _plan_block(current_plan),
        "new": _plan_block(new_plan),
        "price_difference_minor": (
            int(new_plan.price_cents) - int(current_plan.price_cents if current_plan else 0)
        ),
        "currency": (new_plan.currency or "").upper(),
        "limits": limits,
        "features_gained": [f for f in features if f["gained"]],
        "features_lost": [f for f in features if f["lost"]],
        "effective": _effective(direction, subscription, now),
        "usage_impact": _usage_impact(direction, limits),
        "warnings": _warnings(direction, limits, features, subscription),
    }


def _plan_block(plan) -> dict | None:
    if plan is None:
        return None
    return {
        "id": plan.pk,
        "name": plan.name,
        "price_minor": int(plan.price_cents),
        "currency": (plan.currency or "").upper(),
        "interval": plan.interval,
        "limits": plan.limits,
    }


def _feature_changes(current_plan, new_plan) -> list:
    from apps.subscriptions.models import Plan

    out = []
    for key in Plan.FEATURES:
        # A workspace with no plan is treated as having nothing, so every
        # feature on the new plan reads as gained rather than as unchanged.
        had = bool(current_plan.allows(key)) if current_plan is not None else False
        has = bool(new_plan.allows(key))
        out.append({
            "key": key,
            "label": entitlements.feature_label(key),
            "gained": has and not had,
            "lost": had and not has,
        })
    return out


def _effective(direction, subscription, now) -> dict:
    """
    When the change lands, and why then.

    An upgrade is immediate: somebody paying more gets what they paid for the
    moment the payment clears, and starting a fresh period is what
    `services.activate_subscription` already does.

    A downgrade in this deployment is also immediate, because payments here are
    one-off orders rather than a provider-managed recurring subscription --
    there is no scheduled renewal to attach a pending change to. Saying that
    plainly is better than implying a deferral the code does not implement.
    """
    if direction == UPGRADE:
        return {
            "when": "IMMEDIATE",
            "at": now,
            "detail": (
                "Your new plan starts as soon as the payment completes, and a fresh billing "
                "period begins from that moment."
            ),
        }
    if direction == DOWNGRADE:
        return {
            "when": "IMMEDIATE",
            "at": now,
            "period_end": subscription.current_period()[1] if subscription is not None else None,
            "detail": (
                "The new plan and its limits apply as soon as the change is made. Nothing you "
                "have created is removed."
            ),
        }
    return {"when": "IMMEDIATE", "at": now, "detail": "Your limits stay as they are."}


def _usage_impact(direction, limits) -> dict:
    """
    The plain-language answer to "what happens to what I already have?".

    Stated even when nothing is over the line, because "nothing changes" is
    itself the reassurance somebody clicked Preview to get.
    """
    over = [item for item in limits if item["over_by"] > 0]
    if not over:
        return {
            "affected": False,
            "detail": "Everything you have already created stays exactly as it is.",
            "metrics": [],
        }
    return {
        "affected": True,
        "detail": (
            "Nothing is deleted. You keep everything you have already created; you will not be "
            "able to add more until you are back under the new limit."
        ),
        "metrics": [
            {
                "label": item["label"],
                "current_usage": item["current_usage"],
                "new_limit": item["new_limit"],
                "over_by": item["over_by"],
            }
            for item in over
        ],
    }


def _warnings(direction, limits, features, subscription) -> list:
    """
    The specific things somebody should read before confirming.

    Sentences, not codes: this list is rendered as-is, and a UI that had to
    map codes to copy would be a second place for the wording to drift.
    """
    warnings = []

    for item in limits:
        if item["over_by"] > 0:
            warnings.append(
                f"You currently have {item['current_usage']:,} {item['label']} and this plan "
                f"allows {item['new_limit']:,}. Nothing is removed, but you will not be able to "
                f"add more until you are under the limit."
            )
        elif item["reduced"] and item["new_limit"] > 0:
            warnings.append(
                f"Your {item['label']} limit goes down to {item['new_limit']:,}."
            )

    for feature in features:
        if feature["lost"]:
            warnings.append(
                f"{feature['label']} is not included in this plan and will stop being available."
            )

    if direction == DOWNGRADE and subscription is not None and subscription.is_trial:
        warnings.append("Changing plan now ends your free trial.")

    return warnings


def resolve_target(tenant, plan_id):
    """
    The plan a preview is about, filtered through what this workspace may buy.

    Uses the same resolver as checkout, so a preview cannot be obtained for a
    plan the customer could never purchase -- which would otherwise be a
    pleasant way to enumerate every reseller's private pricing.
    """
    from .services import resolve_plan

    return resolve_plan(tenant, plan_id)


def apply_downgrade(*, tenant, new_plan, actor=None, now=None) -> Subscription:
    """
    Move a workspace to a cheaper plan, without taking anything away from it.

    Deliberately **not** a payment path: a downgrade costs nothing, so routing
    it through checkout would mean creating a zero-value order at a provider
    that will refuse it. It changes the plan on the existing subscription row
    and leaves every record the workspace owns exactly where it is.

    The period is *not* restarted. Restarting it on a downgrade would hand the
    customer a fresh monthly message allowance for moving to a smaller plan,
    which is the wrong way round; they keep the window they are in and its
    consumption, and the new, lower ceiling applies to it immediately.
    """
    from apps.audit.services import log_event

    now = now or timezone.now()
    tenant_id = getattr(tenant, "pk", tenant)

    subscription = Subscription.objects.select_for_update().filter(tenant_id=tenant_id).first()
    if subscription is None:
        raise ValueError("This workspace has no subscription to change.")

    previous = subscription.plan
    if previous.pk == new_plan.pk:
        return subscription
    if int(new_plan.price_cents) > int(previous.price_cents):
        # Upgrades are paid for. Routing one through here would move a
        # workspace onto a more expensive plan for free.
        raise ValueError("That plan costs more than your current one. Use checkout to upgrade.")

    subscription.plan = new_plan
    # A downgrade from a trial ends the trial: the customer has chosen a plan,
    # and leaving TRIALING would keep `access_state` deriving from a trial end
    # date that no longer describes anything.
    if subscription.status == Subscription.Status.TRIALING:
        subscription.status = Subscription.Status.ACTIVE
        subscription.trial_ends_at = None
    subscription.save(update_fields=["plan", "status", "trial_ends_at", "updated_at"])

    log_event(
        request=None, action="subscription.downgraded", tenant=subscription.tenant,
        object_type="subscription", object_id=subscription.pk,
        metadata={"from": previous.name, "to": new_plan.name, "by": getattr(actor, "pk", None)},
    )
    return subscription
