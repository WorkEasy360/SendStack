import logging

from django.dispatch import receiver

from apps.inbox.signals import inbound_message_recorded

from . import services

logger = logging.getLogger("sendstack")


@receiver(inbound_message_recorded)
def attribute_new_lead(sender, message, is_new_conversation, **kwargs):
    """
    Work out where a contact came from, at the only moment it is knowable.

    Runs for every inbound message rather than only new conversations: an ad
    referral can arrive on a message from somebody who wrote in months ago,
    and that is still the ad doing its job.
    """
    try:
        services.attribute_inbound(message)
    except Exception:
        # Attribution is reporting. Losing it must never cost the message it
        # was trying to describe.
        logger.exception("Lead attribution failed for message %s.", message.pk)
