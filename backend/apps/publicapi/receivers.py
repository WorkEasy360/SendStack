"""
Turning things that happen into webhook events.

Every receiver here is a thin translation: an existing signal fires, this
decides which public event it is, and `events.emit` does the rest. There is no
provider parsing, no payload inspection and -- importantly -- no second copy of
the button-reply resolution. `button.clicked` is emitted from a `Message` row
whose `replied_button_id` the existing inbound path already filled in, whether
the contact tapped a real button or typed "2" at a QR-linked number.

Nothing in this module may raise. These receivers sit on the send path and the
inbound path; an exception here would fail the send or drop the inbound message
that triggered it. `events.emit` swallows its own errors, and the two receivers
that do work before calling it are wrapped.
"""

import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.campaigns.models import Campaign
from apps.campaigns.signals import campaign_status_changed
from apps.messaging.models import Message
from apps.messaging.signals import message_status_changed, outbound_message_sent
from apps.inbox.signals import inbound_message_recorded
from apps.whatsapp.models import WhatsAppAccount

from . import events

logger = logging.getLogger("sendstack")


#: Message.Status -> the event emitted when a message reaches it. SKIPPED maps
#: to `message.failed` for the same reason the public status vocabulary
#: collapses it: an integration's branch is identical, and the distinction is
#: preserved in the payload's `failure_reason`.
_STATUS_EVENTS = {
    Message.Status.SENT: events.MESSAGE_SENT,
    Message.Status.DELIVERED: events.MESSAGE_DELIVERED,
    Message.Status.READ: events.MESSAGE_READ,
    Message.Status.FAILED: events.MESSAGE_FAILED,
    Message.Status.SKIPPED: events.MESSAGE_FAILED,
}


@receiver(outbound_message_sent)
def on_outbound_message_sent(sender, message, **kwargs):
    """
    `message.sent` -- the provider accepted it.

    Fired from this signal rather than from `message_status_changed`, because
    this one is guaranteed to fire exactly once per successful send. The status
    signal would also fire for a `SENT` delivery receipt arriving afterwards,
    and an integration seeing `message.sent` twice for one message would count
    it twice.
    """
    events.emit(events.MESSAGE_SENT, message.tenant_id, events.message_payload(message))


@receiver(message_status_changed)
def on_message_status_changed(sender, message, status, **kwargs):
    """`message.delivered`, `message.read`, `message.failed`."""
    event_type = _STATUS_EVENTS.get(status)
    # SENT is deliberately excluded here -- see on_outbound_message_sent.
    if event_type is None or event_type == events.MESSAGE_SENT:
        return
    events.emit(event_type, message.tenant_id, events.message_payload(message))


@receiver(inbound_message_recorded)
def on_inbound_message(sender, message, **kwargs):
    """
    `message.received`, or `button.clicked` when the contact chose an option.

    One or the other, never both. A button tap *is* the inbound message, and
    emitting two events for one arrival would make an integration that handles
    both double-process it.
    """
    try:
        if message.replied_button_id:
            events.emit(events.BUTTON_CLICKED, message.tenant_id, events.button_payload(message))
        else:
            events.emit(events.MESSAGE_RECEIVED, message.tenant_id, events.inbound_payload(message))
    except Exception:  # noqa: BLE001 -- see the module docstring
        logger.exception("Could not emit an inbound webhook event for message %s.", message.pk)


@receiver(campaign_status_changed)
def on_campaign_status_changed(sender, campaign, status, **kwargs):
    """`campaign.started`, `campaign.completed`, `campaign.failed`."""
    mapping = {
        Campaign.Status.QUEUED: events.CAMPAIGN_STARTED,
        Campaign.Status.RUNNING: events.CAMPAIGN_STARTED,
        Campaign.Status.SCHEDULED: None,
        Campaign.Status.COMPLETED: events.CAMPAIGN_COMPLETED,
        Campaign.Status.FAILED: events.CAMPAIGN_FAILED,
        # A cancelled campaign is reported as failed rather than getting an
        # event of its own: the brief's vocabulary has no `campaign.cancelled`,
        # and an integration's handling ("this campaign stopped without
        # finishing") is the same either way. `data.status` still says
        # "cancelled", so nothing is lost.
        Campaign.Status.CANCELLED: events.CAMPAIGN_FAILED,
    }
    event_type = mapping.get(status)
    if event_type is None:
        return
    events.emit(event_type, campaign.tenant_id, events.campaign_payload(campaign))


# --- Account connectivity ------------------------------------------------
#
# WhatsAppAccount has no lifecycle signal of its own, and adding one would mean
# touching every one of the twenty-odd places ConnectionManager writes a
# status. All of them go through `save()`, so a pre_save/post_save pair sees
# every transition with one extra query on a table that is written a handful of
# times a day.

_PREVIOUS_STATUS_ATTR = "_publicapi_previous_status"


@receiver(pre_save, sender=WhatsAppAccount)
def remember_account_status(sender, instance, **kwargs):
    """Stash what the row said before this save, so post_save can compare."""
    if not instance.pk:
        setattr(instance, _PREVIOUS_STATUS_ATTR, None)
        return
    try:
        previous = WhatsAppAccount.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    except Exception:  # noqa: BLE001 -- a webhook must never break a device save
        previous = None
    setattr(instance, _PREVIOUS_STATUS_ATTR, previous)


@receiver(post_save, sender=WhatsAppAccount)
def on_account_status_changed(sender, instance, created, **kwargs):
    """
    `account.connected` / `account.disconnected`.

    Only on a genuine *transition*. CONNECTING and QR_READY are steps on the
    way and are not events an integration can act on; what matters to a caller
    is exclusively whether this number can send right now, which is why
    everything that is not CONNECTED collapses into one `account.disconnected`.
    """
    previous = getattr(instance, _PREVIOUS_STATUS_ATTR, None)
    current = instance.status
    if previous == current:
        return

    was_connected = previous == WhatsAppAccount.Status.CONNECTED
    is_connected = current == WhatsAppAccount.Status.CONNECTED

    if is_connected and not was_connected:
        event_type = events.ACCOUNT_CONNECTED
    elif was_connected and not is_connected:
        event_type = events.ACCOUNT_DISCONNECTED
    else:
        # CONNECTING -> QR_READY -> ERROR and back again. Real transitions, but
        # not ones that change the answer to "can I send from this number?".
        return

    events.emit(event_type, instance.tenant_id, events.account_payload(instance))
