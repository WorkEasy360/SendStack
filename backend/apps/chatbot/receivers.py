"""
What the chatbot app listens to.

Deliberately *not* the inbound-message signal. That one is already handled by
``automation.receivers``, which owns the ordered ladder deciding what answers a
customer, and a second independent listener would mean two things answering one
message with no ordering between them -- the exact bug the ladder exists to
prevent. The chatbot is a rung in that ladder, reached by an explicit call.

What is here are the two things that happen *after* an answer, and that nothing
else would otherwise notice.
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.inbox.models import Conversation
from apps.messaging.models import Message
from apps.messaging.signals import message_status_changed

logger = logging.getLogger("sendstack")


@receiver(message_status_changed, dispatch_uid="chatbot_track_delivery")
def track_delivery(sender, message, status, **kwargs):
    """
    Write a bot message's delivery tick into its conversation's trail.

    Only for messages the bot actually sent, and only for a conversation that
    has a session -- otherwise every campaign receipt in the workspace would
    land in the chatbot trace table.

    The status itself is already on the ``Message`` row and the inbox reads it
    from there; this exists so the debug panel can show delivery as the last
    step of the journey it has been narrating, which is where somebody
    debugging "the customer says they never got it" actually looks.
    """
    if message.direction != Message.Direction.OUTBOUND or not message.is_automated:
        return
    if message.conversation_id is None:
        return

    from .models import ChatbotEvent, ChatbotSession

    session = (
        ChatbotSession.objects
        .filter(conversation_id=message.conversation_id)
        .values("chatbot_id", "tenant_id")
        .first()
    )
    if session is None:
        return

    try:
        ChatbotEvent.objects.create(
            tenant_id=session["tenant_id"],
            conversation_id=message.conversation_id,
            chatbot_id=session["chatbot_id"],
            message_id=message.pk,
            correlation_id=f"delivery-{message.pk}",
            step=ChatbotEvent.Step.DELIVERY,
            outcome=(
                ChatbotEvent.Outcome.ERROR
                if status in (Message.Status.FAILED, Message.Status.SKIPPED)
                else ChatbotEvent.Outcome.OK
            ),
            detail=(message.error_message or status)[:500],
            metadata={
                "status": status,
                "failure_reason": message.failure_reason or "",
                "provider_message_id": message.provider_message_id or "",
            },
        )
    except Exception:  # noqa: BLE001 - a lost trace row must never fail a delivery receipt
        logger.debug("Could not record a chatbot delivery event.", exc_info=True)


@receiver(post_save, sender=Conversation, dispatch_uid="chatbot_follow_conversation_close")
def follow_conversation_close(sender, instance, created, **kwargs):
    """
    Keep the session's state honest when the conversation itself is closed.

    Somebody closing a thread in the inbox has ended it, and a bot that carried
    on answering it afterwards would be answering a conversation its own
    workspace considers finished. Closing also cancels anything the bot had
    scheduled but not yet sent.
    """
    if created or instance.status != Conversation.Status.CLOSED:
        return

    from django.db.models import F

    from .models import ChatbotSession

    try:
        ChatbotSession.objects.filter(conversation_id=instance.pk).exclude(
            state=ChatbotSession.State.CLOSED,
        ).update(
            state=ChatbotSession.State.CLOSED,
            delivery_token=F("delivery_token") + 1,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Could not close the chatbot session for conversation %s.", instance.pk, exc_info=True)
