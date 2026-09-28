"""
Free trials: where they come from, how long they last, and how they end.

**One source of truth for the duration**, and it is `trial_days()` below. The
frontend does not know it, the signup view does not know it, the sweeper does
not know it -- they all ask. A trial length written into a React component and
also into a Celery task is a trial length that will one day be two different
numbers, and the customer will be the one who finds out.

Trials flow through Phase 2 like everything else. A trial is a `Subscription`
in `TRIALING` on a real `Plan`; the entitlement service reads it exactly as it
reads a paid one, and there is no "trial" branch anywhere in quota or feature
enforcement. That is deliberate: a second entitlement path for trials is a
second path to get wrong, and it would be the one nobody tests.

**What expiry means here.** When a trial ends the customer keeps every row
they created -- contacts, campaigns, messages, templates, all of it -- and
loses the ability to spend the plan's allowances. Nothing is deleted, ever, by
expiry. There is no grace period on a trial: it ends on the day it says it
ends, which is what the signup page promised. See docs/BILLING.md.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Plan, Subscription

logger = logging.getLogger("sendstack")

#: What a deployment that has configured nothing gets. Long enough to connect
#: WhatsApp, import contacts and run a campaign; short enough to be a trial.
DEFAULT_TRIAL_DAYS = 14


def trials_enabled() -> bool:
    """
    Whether self-service trials may be started at all.

    False when the operator has switched them off *or* when no trial plan is
    configured. The second half matters: an unconfigured deployment must
    refuse to start a trial and say so, never fall through to "no plan", which
    in this codebase means unlimited (see services.get_current_plan).
    """
    if not getattr(settings, "TRIALS_ENABLED", True):
        return False
    return trial_plan() is not None


def trial_days() -> int:
    """How long a trial runs. The only place this number is decided."""
    try:
        days = int(getattr(settings, "BILLING_TRIAL_DAYS", DEFAULT_TRIAL_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_TRIAL_DAYS
    return days if days > 0 else DEFAULT_TRIAL_DAYS


def trial_plan():
    """
    The platform plan a new trial starts on, or None if none is configured.

    Named by ``BILLING_TRIAL_PLAN``, resolved the same careful way
    `services.default_plan` resolves its own: platform-owned, active, matched
    by name and never guessed. A name that matches nothing returns None and
    signup refuses the trial -- which is the safe direction. Picking
    "whatever sorted first" could hand every self-service signup the
    Enterprise plan.
    """
    name = (getattr(settings, "BILLING_TRIAL_PLAN", "") or "").strip()
    if not name:
        return None
    return Plan.objects.filter(owner__isnull=True, name=name, is_active=True).first()


class TrialUnavailable(Exception):
    """A trial was asked for and this deployment cannot start one."""


@transaction.atomic
def start_trial(tenant, *, plan=None, now=None) -> Subscription:
    """
    Put a tenant on a trial. Idempotent per tenant, because `Subscription` is
    a OneToOne -- a second call returns what is already there rather than
    raising, so a retried signup cannot leave a customer with no subscription
    or with two.

    Never silently extends: a tenant that already has a subscription of any
    kind keeps it. Extending an expired trial by calling signup again would be
    a free renewal for anyone who noticed.
    """
    now = now or timezone.now()
    tenant_id = getattr(tenant, "pk", tenant)

    existing = Subscription.objects.select_for_update().filter(tenant_id=tenant_id).first()
    if existing is not None:
        logger.info("Tenant %s already has a subscription; not starting a trial.", tenant_id)
        return existing

    plan = plan or trial_plan()
    if plan is None:
        raise TrialUnavailable(
            "No trial plan is configured for this deployment (BILLING_TRIAL_PLAN)."
        )

    ends_at = now + timedelta(days=trial_days())
    subscription = Subscription.objects.create(
        tenant_id=tenant_id,
        plan=plan,
        status=Subscription.Status.TRIALING,
        current_period_start=now,
        # The trial *is* the period. Giving it the plan's full month while the
        # trial runs fourteen days would mean the monthly message allowance
        # never reset and never mattered -- the trial would end first.
        current_period_end=ends_at,
        trial_started_at=now,
        trial_ends_at=ends_at,
    )
    logger.info("Started a %s-day trial for tenant %s on plan '%s'.", trial_days(), tenant_id, plan.name)
    return subscription


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

def due_for_expiry(now=None):
    """Trials whose end date has passed and whose status has not caught up yet."""
    now = now or timezone.now()
    return Subscription.objects.filter(
        status=Subscription.Status.TRIALING, trial_ends_at__lte=now,
    ).select_related("plan", "tenant")


def expire_trial(subscription, *, now=None) -> bool:
    """
    Move one run-out trial to EXPIRED. Returns whether anything changed.

    **Idempotent, and it has to be**: this runs from a periodic task that may
    overlap itself, be retried, or be run by hand by an operator at the same
    moment. The status filter is the guard -- a second call sees EXPIRED and
    does nothing -- so double-running the sweep cannot double-notify or
    double-anything.

    Deletes nothing. Not the tenant, not their contacts, not their campaigns.
    The customer loses the plan's allowances and keeps every row they made,
    which is the difference between a trial ending and an account ending.
    """
    now = now or timezone.now()
    updated = Subscription.objects.filter(
        pk=subscription.pk, status=Subscription.Status.TRIALING,
    ).update(status=Subscription.Status.EXPIRED, updated_at=now)
    if not updated:
        return False

    subscription.status = Subscription.Status.EXPIRED
    logger.info("Trial expired for tenant %s.", subscription.tenant_id)
    return True


def ending_soon(*, within_days: int, now=None):
    """
    Trials ending inside the next ``within_days`` days and not yet expired.

    Used for the one reminder this product sends. A window rather than an
    exact day so a sweep that misses a run -- a redeploy, an outage -- still
    warns the customer rather than skipping them silently; the send itself is
    de-duplicated by the notice record, not by this query.
    """
    now = now or timezone.now()
    return Subscription.objects.filter(
        status=Subscription.Status.TRIALING,
        trial_ends_at__gt=now,
        trial_ends_at__lte=now + timedelta(days=max(within_days, 0)),
    ).select_related("plan", "tenant")
