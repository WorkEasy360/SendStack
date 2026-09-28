import logging

from django.dispatch import receiver
from django.utils import timezone

from apps.credits.services import consume_credit, release_credits
from apps.subscriptions.services import release_message_allowance
from apps.whatsapp.models import WhatsAppWebhookEvent
from apps.whatsapp.signals import webhook_event_stored
from core.failpoints import crash_at

from .models import Message, MessageSendAttempt
from .services import refresh_campaign_counters
from .signals import message_status_changed

logger = logging.getLogger("sendstack")

_TIMESTAMP_FIELD = {
    Message.Status.SENT: "sent_at",
    Message.Status.DELIVERED: "delivered_at",
    Message.Status.READ: "read_at",
    Message.Status.FAILED: "failed_at",
}

# Statuses only ever move forward; a late "sent" webhook must not undo an
# already-recorded "read". FAILED can arrive independently of this order.
# RECOVERY_REQUIRED ranks with SENDING: it means "we don't know", so any
# real receipt (sent/delivered/read/failed) is news and moves the row on.
_STATUS_RANK = {
    Message.Status.QUEUED: 0,
    Message.Status.SENDING: 1,
    Message.Status.RECOVERY_REQUIRED: 1,
    Message.Status.SENT: 2,
    Message.Status.DELIVERED: 3,
    Message.Status.READ: 4,
}


def _resolve_message(event) -> Message | None:
    """
    Find the message this status event belongs to.

    Primary key is the provider's own message id (``related_message_id``).
    Fallback is the correlation id we sent at attempt time
    (``biz_opaque_callback_data`` / ``send_operation_id``) -> the send attempt
    -> its message. The fallback is what lets a delivery receipt land even when
    the worker died before it could persist the provider message id.
    """
    if event.related_message_id:
        message = Message.objects.filter(
            whatsapp_account_id=event.account_id, provider_message_id=event.related_message_id,
        ).first()
        if message is not None:
            return message

    payload = event.payload or {}
    correlation = payload.get("biz_opaque_callback_data") or payload.get("send_operation_id")
    if not correlation:
        return None

    attempt = (
        MessageSendAttempt.objects.filter(idempotency_key=correlation)
        .select_related("message")
        .first()
    )
    if attempt is None:
        return None
    message = attempt.message
    # Backfill the provider id we never got to record, so the next webhook for
    # this send matches on the fast path and the attempt row is complete.
    if event.related_message_id and not message.provider_message_id:
        Message.objects.filter(pk=message.pk, provider_message_id__isnull=True).update(
            provider_message_id=event.related_message_id,
        )
        MessageSendAttempt.objects.filter(pk=attempt.pk, provider_message_id__isnull=True).update(
            provider_message_id=event.related_message_id,
        )
        message.provider_message_id = event.related_message_id
    return message


def _settle_recovery(message: Message, new_status: str) -> None:
    """
    A webhook resolved a message that was RECOVERY_REQUIRED. Settle the credit
    that was left reserved when the outcome was unknown -- consume it if the
    message actually went out, release it (and the plan allowance) if the
    provider now says it failed. Both are idempotent on the message key.
    """
    if new_status == Message.Status.FAILED:
        release_message_allowance(message)
        if message.billed_to_id:
            release_credits(
                user_id=message.billed_to_id, reference=f"message:{message.pk}",
                operation_id=f"msg-release:{message.pk}",
            )
    else:  # SENT / DELIVERED / READ -- it was delivered after all
        if message.billed_to_id:
            consume_credit(
                user_id=message.billed_to_id, reference=f"message:{message.pk}",
                operation_id=f"msg-consume:{message.pk}",
            )


@receiver(webhook_event_stored)
def apply_message_status_event(sender, event, **kwargs):
    if event.event_type != WhatsAppWebhookEvent.EventType.MESSAGE_STATUS:
        return

    new_status = event.status
    if new_status not in Message.Status.values:
        logger.info("Ignoring webhook status '%s' with no matching Message.Status.", new_status)
        return

    message = _resolve_message(event)
    if message is None:
        return

    crash_at("BEFORE_WEBHOOK")

    was_recovery = message.status == Message.Status.RECOVERY_REQUIRED

    is_regression = (
        new_status != Message.Status.FAILED
        and _STATUS_RANK.get(new_status, -1) <= _STATUS_RANK.get(message.status, -1)
    )
    if not is_regression:
        update_fields = ["status", "updated_at"]
        message.status = new_status
        timestamp_field = _TIMESTAMP_FIELD.get(new_status)
        if timestamp_field:
            setattr(message, timestamp_field, timezone.now())
            update_fields.append(timestamp_field)
        message.save(update_fields=update_fields)

        if was_recovery:
            _settle_recovery(message, new_status)

        # Announced only inside the not-a-regression branch, so a late "sent"
        # arriving after a "read" does not emit an event that walks a
        # subscriber's state backwards.
        message_status_changed.send(sender=Message, message=message, status=new_status)
        refresh_campaign_counters(message.campaign_id)

    event.processed_at = timezone.now()
    event.save(update_fields=["processed_at"])
