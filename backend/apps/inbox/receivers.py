import logging

from django.dispatch import receiver
from django.utils import timezone

from apps.whatsapp.models import WhatsAppWebhookEvent
from apps.whatsapp.signals import webhook_event_stored

from .services import record_inbound_message
from .signals import inbound_message_recorded

logger = logging.getLogger("multisender")


@receiver(webhook_event_stored)
def handle_inbound_message_event(sender, event, **kwargs):
    if event.event_type != WhatsAppWebhookEvent.EventType.INBOUND_MESSAGE:
        return

    message = record_inbound_message(event)

    event.processed_at = timezone.now()
    event.save(update_fields=["processed_at"])

    if message is not None:
        inbound_message_recorded.send(
            sender=message.__class__, message=message, is_new_conversation=getattr(message, "is_new_conversation", False),
        )
