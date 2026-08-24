import logging

from django.dispatch import receiver

from apps.inbox.signals import inbound_message_recorded

from . import services

logger = logging.getLogger("multisender")


@receiver(inbound_message_recorded)
def route_inbound_conversation(sender, message, is_new_conversation, **kwargs):
    """
    Start the response clock and find the message an owner.

    Runs for every inbound message, including one that turns out to be an
    opt-out: the team should still see that it arrived, and the clock stopping
    the moment somebody replies makes it self-correcting either way. The one
    thing that is held back for an opt-out is the away message, which
    `maybe_send_away_message` decides for itself.
    """
    conversation = message.conversation
    if conversation is None:
        return

    try:
        services.on_inbound(conversation)
        services.auto_assign(conversation)
        services.maybe_send_away_message(conversation, message)
    except Exception:
        # An inbound message is already recorded by the time this runs.
        # Failing here must not lose it, or take down the automation rules
        # and workflow steps queued behind this receiver.
        logger.exception("Team routing failed for conversation %s.", conversation.pk)
