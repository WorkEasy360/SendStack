"""
The one place a message can be stopped on its way out.

Consent, quiet hours and per-number send limits are three unrelated rules that
all have to be checked immediately before a send and all have to be checked in
the same order every time. Spreading them across the campaign builder, the
automation services and the task would mean three near-copies that drift, and
a send path added later that quietly obeys none of them. They live here
instead, and apps.messaging.tasks calls this exactly once.

Three answers come back, and the difference between the last two is the whole
point of the module:

* **ALLOW**  -- send it.
* **HOLD**   -- not now, ask again in `retry_after` seconds. The message stays
  QUEUED and nobody loses a recipient. This is what a quiet window or a number
  that has hit its hourly limit produces, because both clear on their own.
* **BLOCK**  -- never. The message is marked SKIPPED with the reason attached.
  Reserved for a decision that will not reverse itself, above all a contact
  who has asked not to be messaged.
"""

import logging
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from .models import Message

logger = logging.getLogger("multisender")

ALLOW = "ALLOW"
HOLD = "HOLD"
BLOCK = "BLOCK"


@dataclass
class GateDecision:
    action: str = ALLOW
    reason: str = ""
    retry_after: float = 0.0

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW


def _max_hold_seconds() -> float:
    return float(getattr(settings, "MESSAGE_MAX_HOLD_SECONDS", 48 * 60 * 60))


def _held_too_long(message, now) -> bool:
    """
    Whether this message has been waiting so long that holding it again is worse
    than admitting it will not be sent.

    Measured from when it was queued, so no counter has to be kept on the row:
    a message that has been rescheduled thirty times and one that has been
    asleep once for a very long time are the same problem, and both end here.
    """
    if not message.queued_at:
        return False
    return (now - message.queued_at).total_seconds() > _max_hold_seconds()


def evaluate(message: Message, account, now=None) -> GateDecision:
    """
    Decide whether this message may go out right now.

    Called immediately before the send and never earlier: every input is
    live -- a contact can unsubscribe, and a number can be paused, in the time
    between a campaign being launched and its last message being dispatched.

    A slot against the number's send limits is *taken* here when the answer is
    ALLOW, so this must not be called speculatively.
    """
    from apps.compliance.models import CompliancePolicy
    from apps.compliance.services import quiet_hours_delay
    from apps.whatsapp import health, limits

    now = now or timezone.now()
    policy = CompliancePolicy.for_tenant(message.tenant_id)

    # -- Consent ------------------------------------------------------------
    # Checked first because it is the only check whose answer is "never", and
    # because it is the one with a person behind it. A contact who unsubscribed
    # after this campaign was launched still has messages sitting in the queue
    # with their name on them; this is where those stop.
    if message.kind != Message.Kind.NOTICE and message.contact_id:
        contact = message.contact
        if contact is not None and not contact.subscribed:
            return GateDecision(
                action=BLOCK,
                reason="This contact has unsubscribed and was not messaged.",
            )

    # -- Quiet hours --------------------------------------------------------
    if message.kind == Message.Kind.MARKETING:
        delay = quiet_hours_delay(policy, message.contact, now=now)
        if delay > 0:
            if _held_too_long(message, now):
                return GateDecision(
                    action=BLOCK,
                    reason="Still outside the allowed sending hours after waiting too long; not sent.",
                )
            return GateDecision(
                action=HOLD,
                reason="Held until the tenant's quiet hours end.",
                retry_after=delay,
            )

    # -- The plan's monthly allowance ---------------------------------------
    # BLOCK rather than HOLD: an exhausted month does not clear inside the
    # hold window (it can be weeks away), so queueing the message would only
    # delay telling somebody their plan is full. A NOTICE is exempt -- cutting
    # off the message that says "your account is over its limit" would be a
    # poor way to deliver that news.
    if message.kind != Message.Kind.NOTICE:
        from apps.subscriptions.services import check_quota

        within_quota, quota_reason = check_quota(message.tenant_id, "messages_per_month")
        if not within_quota:
            return GateDecision(action=BLOCK, reason=f"{quota_reason} Not sent.")

    # -- The number itself --------------------------------------------------
    account_policy = limits.policy_for(account)
    paused, resumes_in = health.pause_state(account, policy=account_policy, now=now)
    if paused:
        # An indefinite pause is not this message's problem to wait out: it
        # needs somebody to look at the number. Reporting it as unavailable
        # sends an automatic campaign to a different number instead, which is
        # the recovery that already exists (see ensure_sendable_account).
        if not resumes_in:
            return GateDecision(
                action=BLOCK,
                reason=account_policy.pause_reason or "Sending from this number is paused.",
            )
        if _held_too_long(message, now):
            return GateDecision(
                action=BLOCK,
                reason=account_policy.pause_reason or "Sending from this number is paused.",
            )
        return GateDecision(action=HOLD, reason=account_policy.pause_reason, retry_after=resumes_in)

    decision = limits.reserve_send_slot(account, policy=account_policy, now=now, day_zone=_day_zone(policy))
    if not decision.allowed:
        health.note_limit_reached(account, decision)
        if _held_too_long(message, now):
            return GateDecision(action=BLOCK, reason=f"{decision.reason} Not sent.")
        return GateDecision(action=HOLD, reason=decision.reason, retry_after=decision.retry_after)

    return GateDecision()


def _day_zone(policy):
    """The timezone a daily send limit's midnight is measured in."""
    from apps.compliance.timezones import load_zone

    return load_zone(policy.timezone)
