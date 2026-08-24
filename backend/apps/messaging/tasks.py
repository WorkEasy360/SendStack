import logging
import random

from django.db.models import F
from django.utils import timezone

from apps.credits.services import consume_credit, release_credits
from config.celery import app

from . import gating
from .models import Message
from .services import attempt_send, ensure_sendable_account, mark_skipped, refresh_campaign_counters
from .signals import outbound_message_sent

logger = logging.getLogger("multisender")

MAX_SEND_RETRIES = 3

#: Spread added to a held message's wake-up time. Without it every message a
#: campaign parked behind one quiet window or one daily limit comes back at
#: the same instant, and the burst that arrives the moment the window opens is
#: exactly the traffic shape the limits exist to avoid.
_HOLD_JITTER_SECONDS = 30


@app.task(bind=True, max_retries=MAX_SEND_RETRIES)
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
        if self.request.retries < self.max_retries:
            Message.objects.filter(pk=message.pk).update(retry_count=F("retry_count") + 1)
            raise self.retry(
                exc=RuntimeError("No connected WhatsApp account is available."),
                countdown=30 * (2**self.request.retries),
            )
        if mark_skipped(message, "No connected WhatsApp account was available to send this message."):
            if message.billed_to_id:
                release_credits(user_id=message.billed_to_id, reference=f"message:{message.pk}")
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
        if mark_skipped(message, gate.reason):
            if message.billed_to_id:
                release_credits(user_id=message.billed_to_id, reference=f"message:{message.pk}")
            refresh_campaign_counters(message.campaign_id)
        return

    result = attempt_send(message)
    if result is None:
        return  # claimed by another worker / already resolved

    if result.success:
        logger.info(
            "Campaign %s | recipient %s | account: %s (id=%s) | delay: %s | result: SENT",
            message.campaign_id, message.to_phone, account.name, account.pk,
            f"{message.assigned_delay_seconds:.3f}s" if message.assigned_delay_seconds is not None else "n/a",
        )
        if message.billed_to_id:
            consume_credit(user_id=message.billed_to_id, reference=f"message:{message.pk}")
        if message.direction == Message.Direction.OUTBOUND and message.contact_id:
            # Announced for every outbound send, automated or not: "when did
            # we last speak to this person" is true of an auto-reply as much
            # as a hand-typed one, and the CRM's no-reply segments are wrong
            # without it.
            outbound_message_sent.send(sender=Message, message=message)
        if message.direction == Message.Direction.OUTBOUND and message.contact_id and not message.is_automated:
            from apps.automation.services import schedule_follow_up_checks

            schedule_follow_up_checks(message)
    else:
        # An ambiguous failure (the provider was reached but never answered)
        # is terminal, not retryable: the message may already be on the
        # recipient's phone, and a retry would deliver it a second time.
        if not result.ambiguous and self.request.retries < self.max_retries:
            Message.objects.filter(pk=message.pk).update(status=Message.Status.QUEUED, retry_count=F("retry_count") + 1)
            raise self.retry(exc=RuntimeError(result.error), countdown=30 * (2**self.request.retries))

        message.status = Message.Status.FAILED
        if result.ambiguous:
            logger.error(
                "Message %s left in an unknown delivery state; not retried to avoid a duplicate send: %s",
                message.pk, result.error,
            )
            note = (
                "Delivery could not be confirmed, so this message was not retried "
                "automatically -- it may or may not have been received."
            )
            detail = f"{result.error} " if result.error else ""
            # Trim the provider's detail, never the note -- error_message is
            # capped at 500 chars and the note is the part a user must see.
            message.error_message = detail[: 500 - len(note)] + note
        else:
            message.error_message = result.error or ""
        message.failed_at = timezone.now()
        message.save(update_fields=["status", "error_message", "failed_at", "updated_at"])
        logger.warning(
            "Campaign %s | recipient %s | account: %s (id=%s) | result: FAILED | reason: %s",
            message.campaign_id, message.to_phone, account.name, account.pk, result.error or "unknown",
        )
        # Called after the row is FAILED so the failure it is reacting to is
        # already inside the window it measures. This is what stops a campaign
        # from hammering a number WhatsApp has started refusing.
        from apps.whatsapp.health import note_send_failure

        note_send_failure(account, result.error or "")

        if message.billed_to_id:
            release_credits(user_id=message.billed_to_id, reference=f"message:{message.pk}")

    refresh_campaign_counters(message.campaign_id)
