import logging
import random

from django.conf import settings
from django.db.models import F

from apps.credits.services import consume_credit, release_credits
from apps.subscriptions.services import release_message_allowance
from config.celery import app
from core.backoff import retry_delay

from . import gating
from .models import Message
from .services import (
    attempt_send,
    ensure_sendable_account,
    finalize_failure,
    mark_skipped,
    refresh_campaign_counters,
    release_for_retry,
)
from .signals import message_status_changed, outbound_message_sent

logger = logging.getLogger("sendstack")

MAX_SEND_RETRIES = 3

#: A send task that runs longer than this has almost certainly hit a provider
#: call that hung. The soft limit raises SoftTimeLimitExceeded inside the task
#: (caught in attempt_send -> the message becomes RECOVERY_REQUIRED, because a
#: transmitted-but-unanswered send is unknown, not failed); the hard limit is
#: the backstop that actually kills the worker if the soft one is ignored.
#: Both comfortably exceed the QR bridge's own 45s HTTP timeout plus a
#: multi-attachment message's handful of calls.
SEND_SOFT_TIME_LIMIT = int(getattr(settings, "SEND_SOFT_TIME_LIMIT_SECONDS", 90))
SEND_HARD_TIME_LIMIT = int(getattr(settings, "SEND_HARD_TIME_LIMIT_SECONDS", 120))

#: Base wait before a retry when the workspace has expressed no preference.
#: The value `retry_delay` has always been called with, kept as the default so
#: a workspace that never opens the settings screen retries exactly as before.
DEFAULT_RETRY_BASE_SECONDS = 30


def _retry_policy(message):
    """
    How many times to retry this message and how long to wait first.

    Read from the workspace's own settings (apps.tenants.TenantSettings) and
    bounded by this task's own ceiling: `max_retries` can be lowered, or
    turned off entirely, but it can never exceed MAX_SEND_RETRIES -- a
    workspace must not be able to have a worker hammer a broken number
    indefinitely.

    Neither knob can loosen the rule that matters: an *ambiguous* failure is
    never retried whatever this returns, because the message may already be on
    the recipient's phone. That check is in the caller and does not consult
    this at all.
    """
    from apps.tenants.models import TenantSettings

    pacing = TenantSettings.for_tenant(message.tenant_id)
    if not pacing.retry_enabled:
        return 0, DEFAULT_RETRY_BASE_SECONDS
    return (
        min(int(pacing.max_retries or 0), MAX_SEND_RETRIES),
        max(int(pacing.retry_delay_seconds or 0), 1),
    )

#: Spread added to a held message's wake-up time. Without it every message a
#: campaign parked behind one quiet window or one daily limit comes back at
#: the same instant, and the burst that arrives the moment the window opens is
#: exactly the traffic shape the limits exist to avoid.
_HOLD_JITTER_SECONDS = 30

#: The provider's own error code -> the code a report groups by. Providers
#: classify their failures already (see ProviderError.code); this is the one
#: place that vocabulary is translated into this application's, so a new
#: provider only has to emit codes rather than know about Message.
#:
#: Anything not listed becomes PROVIDER_ERROR -- "the provider said no and we
#: have not characterised why", which is honest and still countable, rather
#: than being silently dropped into one of the specific buckets.
_PROVIDER_FAILURE_CODES = {
    "network_error": Message.FailureReason.PROVIDER_UNAVAILABLE,
    "timeout": Message.FailureReason.PROVIDER_UNAVAILABLE,
    "connection_error": Message.FailureReason.PROVIDER_UNAVAILABLE,
    "rate_limited": Message.FailureReason.RATE_LIMITED,
    "invalid_recipient": Message.FailureReason.INVALID_RECIPIENT,
    "no_session": Message.FailureReason.DEVICE_DISCONNECTED,
    "missing_credentials": Message.FailureReason.DEVICE_DISCONNECTED,
    "authentication_error": Message.FailureReason.DEVICE_DISCONNECTED,
    "configuration_error": Message.FailureReason.DEVICE_DISCONNECTED,
}


def _failure_reason(result) -> str:
    """Which Message.FailureReason this send result is."""
    if result.ambiguous:
        return Message.FailureReason.DELIVERY_UNCONFIRMED
    return _PROVIDER_FAILURE_CODES.get(
        (result.code or "").strip().lower(), Message.FailureReason.PROVIDER_ERROR
    )


@app.task(
    bind=True,
    max_retries=MAX_SEND_RETRIES,
    soft_time_limit=SEND_SOFT_TIME_LIMIT,
    time_limit=SEND_HARD_TIME_LIMIT,
)
def send_message_task(self, message_id):
    try:
        message = Message.objects.select_related("whatsapp_account", "campaign").get(pk=message_id)
    except Message.DoesNotExist:
        logger.warning("Message %s no longer exists.", message_id)
        return

    if message.status != Message.Status.QUEUED:
        return

    if message.campaign_id:
        campaign_status = message.campaign.status
        if campaign_status == "PAUSED":
            return  # left QUEUED; the dispatcher re-enqueues remaining messages on resume
        if campaign_status == "CANCELLED":
            Message.objects.filter(pk=message.pk, status=Message.Status.QUEUED).update(status=Message.Status.CANCELLED)
            refresh_campaign_counters(message.campaign_id)
            return

    # Account health check, immediately before the send: the account this
    # message was assigned at launch may have disconnected since, in which
    # case an automatic campaign moves it onto another connected account
    # rather than burning the recipient.
    account = ensure_sendable_account(message)
    if account is None:
        # Nothing eligible right now. Spend the normal retry budget first --
        # a device that dropped may well be back before the last attempt --
        # and only give up on the recipient once it's exhausted. The message
        # stays QUEUED in the meantime, so it is never lost.
        allowed_retries, retry_base = _retry_policy(message)
        if self.request.retries < allowed_retries:
            Message.objects.filter(pk=message.pk).update(retry_count=F("retry_count") + 1)
            raise self.retry(
                exc=RuntimeError("No connected WhatsApp account is available."),
                countdown=retry_delay(self.request.retries, base=retry_base),
            )
        if mark_skipped(
            message,
            "No connected WhatsApp account was available to send this message.",
            code=Message.FailureReason.DEVICE_DISCONNECTED,
        ):
            # The plan's allowance goes back alongside the credit: a message
            # with nowhere to go was never sent, and charging a month's
            # quota for it would be charging twice for one recipient once the
            # campaign is retried.
            release_message_allowance(message)
            if message.billed_to_id:
                release_credits(
                    user_id=message.billed_to_id, reference=f"message:{message.pk}",
                    operation_id=f"msg-release:{message.pk}",
                )
            refresh_campaign_counters(message.campaign_id)
        return

    # Consent, quiet hours and this number's send limits, in that order and in
    # one place -- see apps/messaging/gating.py. A slot against the number's
    # limits is taken here when the answer is ALLOW, so nothing between this
    # call and attempt_send may return early.
    gate = gating.evaluate(message, account)
    if gate.action == gating.HOLD:
        countdown = gate.retry_after + random.uniform(0, _HOLD_JITTER_SECONDS)
        logger.info(
            "Campaign %s | recipient %s | account: %s (id=%s) | result: HELD for %.0fs | reason: %s",
            message.campaign_id, message.to_phone, account.name, account.pk, countdown, gate.reason,
        )
        # A fresh task rather than self.retry: being held is not a failed
        # attempt, and spending the retry budget on it would mean a message
        # waiting out a quiet night had none left for a real send failure.
        send_message_task.apply_async(args=[message.pk], countdown=countdown)
        return

    if gate.action == gating.BLOCK:
        if mark_skipped(message, gate.reason, code=gate.code):
            release_message_allowance(message)
            if message.billed_to_id:
                release_credits(
                    user_id=message.billed_to_id, reference=f"message:{message.pk}",
                    operation_id=f"msg-release:{message.pk}",
                )
            refresh_campaign_counters(message.campaign_id)
        return

    outcome = attempt_send(message, account, task_id=self.request.id or "")

    if outcome is None or outcome.disposition == "stale":
        # Another worker already claimed this message, or the recovery sweep
        # reclaimed our lease while we were in the provider call. Either way the
        # outcome is not ours to write.
        return

    if outcome.disposition == "sent":
        logger.info(
            "Campaign %s | recipient %s | account: %s (id=%s) | delay: %s | result: SENT",
            message.campaign_id, message.to_phone, account.name, account.pk,
            f"{message.assigned_delay_seconds:.3f}s" if message.assigned_delay_seconds is not None else "n/a",
        )
        if message.billed_to_id:
            # Keyed on the message, not the attempt: a message is charged
            # exactly once however many attempts or redeliveries it took.
            consume_credit(
                user_id=message.billed_to_id, reference=f"message:{message.pk}",
                operation_id=f"msg-consume:{message.pk}",
            )
        if message.direction == Message.Direction.OUTBOUND and message.contact_id:
            # Announced for every outbound send, automated or not: "when did
            # we last speak to this person" is true of an auto-reply as much
            # as a hand-typed one, and the CRM's no-reply segments are wrong
            # without it.
            outbound_message_sent.send(sender=Message, message=message)
        if message.direction == Message.Direction.OUTBOUND and message.contact_id and not message.is_automated:
            from apps.automation.services import schedule_follow_up_checks

            schedule_follow_up_checks(message)
        refresh_campaign_counters(message.campaign_id)
        return

    if outcome.disposition == "recovery_required":
        # The worker was reached but never confirmed, or died in the ambiguous
        # window, or the send timed out. The message may or may not be on the
        # recipient's phone. It is NOT retried (that risks a duplicate) and the
        # credit stays reserved -- neither charged nor refunded until a human
        # or the reconciliation sweep settles it. attempt_send already set the
        # row to RECOVERY_REQUIRED.
        message_status_changed.send(
            sender=Message, message=message, status=Message.Status.RECOVERY_REQUIRED,
        )
        logger.error(
            "Campaign %s | recipient %s | account: %s (id=%s) | result: RECOVERY_REQUIRED | %s",
            message.campaign_id, message.to_phone, account.name, account.pk,
            (outcome.result.error if outcome.result else "worker lost mid-send"),
        )
        refresh_campaign_counters(message.campaign_id)
        return

    # disposition == "provider_rejected": the provider was reached and
    # definitively refused. The message is still SENDING under our lease, so we
    # own the retry-vs-terminal decision. This is the ONLY path that resets a
    # message back to QUEUED, and only for a proven rejection.
    result = outcome.result
    allowed_retries, retry_base = _retry_policy(message)
    if self.request.retries < allowed_retries:
        if release_for_retry(message, outcome.attempt):
            Message.objects.filter(pk=message.pk).update(retry_count=F("retry_count") + 1)
            raise self.retry(
                exc=RuntimeError(result.error), countdown=retry_delay(self.request.retries, base=retry_base),
            )
        # Lease moved under us between the provider call and here -- recovery
        # owns it now.
        return

    if finalize_failure(message, outcome.attempt, error=result.error or "", reason=_failure_reason(result)):
        message_status_changed.send(sender=Message, message=message, status=Message.Status.FAILED)
        logger.warning(
            "Campaign %s | recipient %s | account: %s (id=%s) | result: FAILED | reason: %s",
            message.campaign_id, message.to_phone, account.name, account.pk, result.error or "unknown",
        )
        # Called after the row is FAILED so the failure it is reacting to is
        # already inside the window it measures. This is what stops a campaign
        # from hammering a number WhatsApp has started refusing.
        from apps.whatsapp.health import note_send_failure

        note_send_failure(account, result.error or "")

        release_message_allowance(message)
        if message.billed_to_id:
            release_credits(
                user_id=message.billed_to_id, reference=f"message:{message.pk}",
                operation_id=f"msg-release:{message.pk}",
            )

    refresh_campaign_counters(message.campaign_id)


@app.task(name="apps.messaging.tasks.sweep_stuck_sends")
def sweep_stuck_sends_task():
    """
    Periodic (CELERY_BEAT_SCHEDULE). Reconcile every message left in SENDING by
    a worker that died inside the ambiguous provider window.

    This -- not any Celery ack setting -- is what guarantees no message stays
    stuck in SENDING. ``acks_late`` plus the Redis visibility timeout only
    *redeliver the task*, and a redelivered send_message_task hits the
    ``status != QUEUED`` guard and does nothing; the durable lease on the
    Message row plus this sweep is the authority. See apps/messaging/recovery.py.
    """
    from .recovery import sweep_stuck_sends

    return sweep_stuck_sends()
