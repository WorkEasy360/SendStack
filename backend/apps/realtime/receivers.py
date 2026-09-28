"""
What gets announced, and from where.

Publishing hangs off the signals the platform already sends rather than being
sprinkled through the services that do the work: an inbound message is
announced because it was recorded, not because some particular code path
remembered to say so. A send path added later is therefore live by default.
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.inbox.models import Conversation
from apps.inbox.signals import inbound_message_recorded
from apps.messaging.models import Message

from . import events

logger = logging.getLogger("sendstack")


def _message_payload(message) -> dict:
    return {
        "id": message.pk,
        "conversation": message.conversation_id,
        "contact": message.contact_id,
        "direction": message.direction,
        "status": message.status,
        "body": (message.body or "")[:500],
        "message_type": message.message_type,
        "queued_at": message.queued_at,
    }


@receiver(inbound_message_recorded)
def announce_inbound_message(sender, message, is_new_conversation, **kwargs):
    events.publish(message.tenant_id, "message.received", {
        **_message_payload(message), "is_new_conversation": is_new_conversation,
    })


@receiver(post_save, sender=Message)
def announce_message_change(sender, instance, created, **kwargs):
    """
    Outbound messages and their delivery ticks.

    Inbound ones are announced by the signal above instead, which fires once
    the conversation exists and carries the "new conversation" flag the inbox
    needs to decide whether to insert a row or update one.
    """
    if instance.direction == Message.Direction.INBOUND or not instance.conversation_id:
        return
    events.publish(
        instance.tenant_id, "message.sent" if created else "message.updated", _message_payload(instance),
    )


@receiver(post_save, sender=Conversation)
def announce_conversation_change(sender, instance, created, **kwargs):
    events.publish(instance.tenant_id, "conversation.updated", {
        "id": instance.pk,
        "status": instance.status,
        "assigned_agent": instance.assigned_agent_id,
        "unread_count": instance.unread_count,
        "awaiting_reply_since": instance.awaiting_reply_since,
        "last_message_at": instance.last_message_at,
    })
