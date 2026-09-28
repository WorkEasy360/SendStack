"""
The one place a message can be stopped on its way out.

Workspace status, consent, quiet hours, the plan's allowance and per-number
send limits are unrelated rules that all have to be checked immediately before
a send and all have to be checked in the same order every time. Spreading them
across the campaign builder, the automation services and the task would mean
several near-copies that drift, and a send path added later that quietly obeys
none of them. They live here instead, and apps.messaging.tasks calls this
exactly once.

The order, and what each answer means:

1. **Is the workspace active?** A suspended customer sends nothing.
2. **Has the contact opted out?** Absolute, and checked before anything is
   consumed.
3. **Is there marketing consent**, where the tenant requires it? Marketing
   only -- a service reply to somebody who just messaged in never needs one.
4. **Quiet hours** -- marketing only, and a hold rather than a block.
5. **The plan's monthly allowance**, claimed transactionally per message.
6. **The number itself** -- pause state and send limits.

Steps 2 and 3 are not implemented here. The precedence rule --

    explicit opt-out  >  marketing consent  >  campaign eligibility

-- lives in apps.compliance.services.may_receive_marketing, so this gate and
any future caller share one implementation of it. See docs/CONSENT.md.

**Every input is read live, at send time, and never earlier.** That is the
point of doing this here rather than at launch: a campaign scheduled on Monday
and dispatched on Friday carries an authorization decision made on Monday, and
by Friday the workspace may be suspended, the subscription lapsed, the plan
full or the recipient unsubscribed. A scheduled task must never trust the
authorization it was scheduled with.

A premium feature is not an authorization layer. Campaigns, workflows, the AI
agent and voice follow-up all produce a Message and all land here.

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

logger = logging.getLogger("sendstack")

ALLOW = "ALLOW"
HOLD = "HOLD"
BLOCK = "BLOCK"


@dataclass
class GateDecision:
    action: str = ALLOW
    reason: str = ""
    retry_after: float = 0.0
    #: The stable code behind `reason`, from Message.FailureReason. Carried on
    #: a BLOCK so the skip can be counted as well as read; empty on ALLOW and
    #: on a HOLD, which is not a failure and is not recorded as one.
    code: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW


def _allowance_code(reason: str) -> str:
    """
    Which billing refusal this was, from the sentence the allowance check gave.

    Reading a code out of prose is exactly what these codes exist to stop, and
    it is the right thing to do here only because it is *one* place: the
    alternative is threading a code back through the entitlement service, whose
    callers are not all message sends and none of the rest have a use for one.
    Anything unrecognised falls back to the plan ceiling, which is what every
    refusal from that check was before subscriptions could expire.
    """
    lowered = (reason or "").lower()
    if "subscription" in lowered or "expired" in lowered or "trial" in lowered:
        return Message.FailureReason.SUBSCRIPTION_EXPIRED
    return Message.FailureReason.QUOTA_EXCEEDED


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
    from apps.compliance.services import may_receive_marketing, quiet_hours_delay
    from apps.whatsapp import health, limits

    now = now or timezone.now()
    policy = CompliancePolicy.for_tenant(message.tenant_id)

    # -- Is this workspace still allowed to send at all? --------------------
    # Before anything about the recipient, because a suspended customer must
    # not consume credits, provider capacity or a sender's reputation
    # whatever the message is.
    #
    # Checked *here*, at send time, and not where the campaign was launched.
    # That is the whole point: a campaign scheduled on Monday and dispatched on
    # Friday carries an authorization decision made on Monday, and by Friday
    # the account may have been suspended. A scheduled task must never trust
    # the authorization it was scheduled with. Subscription expiry gets the
    # same treatment a few checks below, inside claim_message_allowance.
    if not _tenant_may_send(message):
        return GateDecision(
            action=BLOCK,
            reason="This workspace is suspended, so the message was not sent.",
            code=Message.FailureReason.TENANT_SUSPENDED,
        )

    # -- Consent ------------------------------------------------------------
    # Checked before anything about capacity because it is the only check whose
    # answer is "never", and because it is the one with a person behind it. A
    # contact who unsubscribed after this campaign was launched still has
    # messages sitting in the queue with their name on them; this is where
    # those stop.
    #
    # The precedence rule -- opt-out beats consent beats eligibility -- is not
    # written here. It lives in compliance.services.may_receive_marketing, so
    # that this gate and every future caller share one implementation of it.
    if message.kind != Message.Kind.NOTICE and message.contact_id:
        contact = message.contact
        if contact is not None:
            if not contact.subscribed:
                return GateDecision(
                    action=BLOCK,
                    reason="This contact has unsubscribed and was not messaged.",
                    code=Message.FailureReason.OPTED_OUT,
                )
            # Positive consent is asked for only where the tenant has asked for
            # it, and only of marketing. A service reply to somebody who just
            # messaged in is not marketing and must not need an opt-in on file.
            if message.kind == Message.Kind.MARKETING:
                permitted, consent_reason = may_receive_marketing(contact, policy=policy)
                if not permitted:
                    return GateDecision(
                        action=BLOCK,
                        reason=consent_reason,
                        code=Message.FailureReason.CONSENT_MISSING,
                    )

    # -- Quiet hours --------------------------------------------------------
    if message.kind == Message.Kind.MARKETING:
        delay = quiet_hours_delay(policy, message.contact, now=now)
        if delay > 0:
            if _held_too_long(message, now):
                return GateDecision(
                    action=BLOCK,
                    reason="Still outside the allowed sending hours after waiting too long; not sent.",
                    code=Message.FailureReason.HELD_TOO_LONG,
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
        from apps.subscriptions.services import claim_message_allowance

        # Claimed, not merely checked. The claim is stamped on this message
        # row inside the same transaction that counts the period's usage, so
        # two workers reaching the last free slot cannot both take it; it is
        # idempotent, so a message that comes back after a hold does not pay
        # twice; and it is released wherever the credit reservation is, so a
        # message that never goes out costs nothing. See
        # apps.subscriptions.services.claim_message_allowance.
        within_quota, quota_reason = claim_message_allowance(message, now=now)
        if not within_quota:
            # The allowance check answers two different refusals -- the plan's
            # monthly ceiling and a subscription that is no longer active --
            # and they need different codes because they need different fixes.
            return GateDecision(
                action=BLOCK,
                reason=f"{quota_reason} Not sent.",
                code=_allowance_code(quota_reason),
            )

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
                code=Message.FailureReason.DEVICE_PAUSED,
            )
        if _held_too_long(message, now):
            return GateDecision(
                action=BLOCK,
                reason=account_policy.pause_reason or "Sending from this number is paused.",
                code=Message.FailureReason.HELD_TOO_LONG,
            )
        return GateDecision(action=HOLD, reason=account_policy.pause_reason, retry_after=resumes_in)

    decision = limits.reserve_send_slot(account, policy=account_policy, now=now, day_zone=_day_zone(policy))
    if not decision.allowed:
        health.note_limit_reached(account, decision)
        if _held_too_long(message, now):
            return GateDecision(
                action=BLOCK,
                reason=f"{decision.reason} Not sent.",
                code=Message.FailureReason.HELD_TOO_LONG,
            )
        return GateDecision(action=HOLD, reason=decision.reason, retry_after=decision.retry_after)

    return GateDecision()


def _tenant_may_send(message) -> bool:
    """
    Whether the message's workspace is active.

    Fails **closed** on an unexpected error, unlike most decoration on this
    path: the question being answered is "is this customer suspended", and an
    unreadable answer must not be read as "no". A suspended tenant is a
    deliberate act by an operator, usually for abuse or non-payment, and
    sending anyway would undo it.

    A message with no tenant cannot be evaluated and is left alone -- nothing
    in the application creates one, and inventing a refusal for an impossible
    row would only make a real bug harder to see.
    """
    if not message.tenant_id:
        return True
    try:
        from apps.tenants.models import Tenant

        return Tenant.objects.filter(pk=message.tenant_id, is_active=True).exists()
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception(
            "Could not confirm whether tenant %s is active; refusing the send.", message.tenant_id,
        )
        return False


def _day_zone(policy):
    """The timezone a daily send limit's midnight is measured in."""
    from apps.compliance.timezones import load_zone

    return load_zone(policy.timezone)
