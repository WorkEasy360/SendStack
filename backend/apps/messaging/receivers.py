import logging

from django.dispatch import receiver
from django.utils import timezone

from apps.whatsapp.models import WhatsAppWebhookEvent
from apps.whatsapp.signals import webhook_event_stored

from .models import Message
from .services import refresh_campaign_counters

logger = logging.getLogger("multisender")

_TIMESTAMP_FIELD = {
    Message.Status.SENT: "sent_at",
    Message.Status.DELIVERED: "delivered_at",
    Message.Status.READ: "read_at",
    Message.Status.FAILED: "failed_at",
}

# Statuses only ever move forward; a late "sent" webhook must not undo an
# already-recorded "read". FAILED can arrive independently of this order.
_STATUS_RANK = {
    Message.Status.QUEUED: 0,
    Message.Status.SENDING: 1,
    Message.Status.SENT: 2,
    Message.Status.DELIVERED: 3,
    Message.Status.READ: 4,
}


@receiver(webhook_event_stored)
def apply_message_status_event(sender, event, **kwargs):
    if event.event_type != WhatsAppWebhookEvent.EventType.MESSAGE_STATUS or not event.related_message_id:
        return

    new_status = event.status
    if new_status not in Message.Status.values:
        logger.info("Ignoring webhook status '%s' with no matching Message.Status.", new_status)
        return

    try:
        message = Message.objects.get(whatsapp_account_id=event.account_id, provider_message_id=event.related_message_id)
    except Message.DoesNotExist:
        return

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
        refresh_campaign_counters(message.campaign_id)

    event.processed_at = timezone.now()
    event.save(update_fields=["processed_at"])
