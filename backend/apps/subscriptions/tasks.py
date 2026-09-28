"""
The subscription lifecycle sweep.

Read what this task is **not** before reading what it is. It is not what stops
an expired trial from sending: `Subscription.access_state` decides that from
the dates, and would decide it identically if this task never ran again. That
separation is the point. A lifecycle job is a thing that can die -- a beat
container that fails to start, a queue that backs up, a deploy that drops a
schedule -- and a system where a dead job means every trialling tenant keeps
premium access for free is a system with a billing hole in it.

What this task actually does is bookkeeping and courtesy: it writes the status
change so the billing page can say "your trial ended on the 14th", and it
sends the two notices a customer should get. Both are safe to skip, safe to
repeat, and safe to run twice at once.
"""

import logging

from django.conf import settings
from django.utils import timezone

from apps.audit.services import log_event
from config.celery import app

from . import trials
from .models import Subscription, SubscriptionNotice

logger = logging.getLogger("sendstack")


def trial_reminder_days() -> int:
    """How many days before the end the reminder goes out. One place, as ever."""
    try:
        return max(int(getattr(settings, "BILLING_TRIAL_REMINDER_DAYS", 3)), 0)
    except (TypeError, ValueError):
        return 3


@app.task(name="apps.subscriptions.tasks.sweep_subscription_lifecycle")
def sweep_subscription_lifecycle():
    """
    Expire trials that have run out, and warn the ones about to.

    Idempotent in both halves: expiry is a conditional UPDATE that a second
    run finds nothing to do, and each notice is claimed through a unique
    constraint before it is sent. Running this twice concurrently produces
    exactly the same result as running it once.
    """
    now = timezone.now()
    expired = warned = 0

    for subscription in trials.due_for_expiry(now=now):
        if not trials.expire_trial(subscription, now=now):
            continue
        expired += 1
        log_event(
            request=None, action="subscription.trial_expired", tenant=subscription.tenant,
            object_type="subscription", object_id=subscription.pk,
            metadata={"plan": subscription.plan.name, "ended": subscription.trial_ends_at.isoformat()},
        )
        if SubscriptionNotice.claim(
            subscription, SubscriptionNotice.Kind.TRIAL_EXPIRED, subscription.trial_ends_at.date(),
        ):
            _notify(subscription, SubscriptionNotice.Kind.TRIAL_EXPIRED)

    reminder_days = trial_reminder_days()
    if reminder_days:
        for subscription in trials.ending_soon(within_days=reminder_days, now=now):
            if SubscriptionNotice.claim(
                subscription, SubscriptionNotice.Kind.TRIAL_ENDING, subscription.trial_ends_at.date(),
            ):
                _notify(subscription, SubscriptionNotice.Kind.TRIAL_ENDING)
                warned += 1

    if expired or warned:
        logger.info("Subscription sweep: %s trial(s) expired, %s reminder(s) sent.", expired, warned)
    return {"expired": expired, "warned": warned}


def _notify(subscription, kind) -> None:
    """
    Tell the workspace. Never raises -- a mail server outage must not roll back
    an expiry that has already been written and is already in force.
    """
    try:
        from .notices import send_billing_notice

        send_billing_notice(subscription, kind)
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception(
            "Could not deliver the %s notice for subscription %s.", kind, subscription.pk,
        )
