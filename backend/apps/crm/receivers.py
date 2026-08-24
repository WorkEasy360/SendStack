"""
Keeping the CRM current without anyone maintaining it.

Everything here hangs off something the platform already does. The rule for
each handler is the same: it must be cheap (a message arriving is the hottest
path there is), it must be safe to fail (a CRM stamp is never worth losing a
message over), and it must not write when nothing changed.
"""

import logging

from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.compliance.models import ConsentEvent
from apps.contacts.models import Contact
from apps.inbox.signals import inbound_message_recorded
from apps.messaging.signals import outbound_message_sent

from . import activity, lifecycle, segments
from .models import Activity
from .scoring import score_contact

logger = logging.getLogger("multisender")


@receiver(inbound_message_recorded)
def crm_on_inbound(sender, message, **kwargs):
    """They answered: stamp it, promote them, rescore them."""
    contact = message.contact
    if contact is None:
        return
    try:
        now = message.queued_at or timezone.now()
        Contact.objects.filter(pk=contact.pk).update(last_inbound_at=now, last_message_at=now)
        contact.last_inbound_at = now
        lifecycle.on_inbound_reply(contact)
        # Rescoring here rather than on a timer is what makes "hot leads"
        # useful: the moment someone replies they should be at the top of the
        # list, not at the top of it tomorrow morning.
        score_contact(contact)
        segments.invalidate_counts(contact.tenant_id)
    except Exception:  # noqa: BLE001 - never let CRM bookkeeping drop a message
        logger.exception("CRM inbound handler failed for message %s", message.pk)


@receiver(outbound_message_sent)
def crm_on_outbound(sender, message, **kwargs):
    """We spoke: stamp it, so the no-reply segments have a clock to run from."""
    if not message.contact_id:
        return
    try:
        now = message.sent_at or timezone.now()
        Contact.objects.filter(pk=message.contact_id).update(last_outbound_at=now, last_message_at=now)
        segments.invalidate_counts(message.tenant_id)
    except Exception:  # noqa: BLE001
        logger.exception("CRM outbound handler failed for message %s", message.pk)


@receiver(post_save, sender=ConsentEvent)
def crm_on_consent(sender, instance, created, **kwargs):
    """An opt-out is the single most important thing on a contact's timeline."""
    if not created or not instance.contact_id:
        return
    try:
        activity.record(
            instance.contact, Activity.Kind.CONSENT_CHANGED,
            f"{instance.get_event_display()} ({instance.get_source_display()})",
            metadata={"event": instance.event, "source": instance.source},
            occurred_at=instance.created_at or timezone.now(),
        )
        if instance.event == ConsentEvent.Event.OPT_OUT:
            # An unsubscribed contact scores zero (see scoring), so they drop
            # out of every "who should I call" list immediately rather than at
            # the next nightly sweep.
            score_contact(instance.contact)
    except Exception:  # noqa: BLE001
        logger.exception("CRM consent handler failed for event %s", instance.pk)


@receiver(m2m_changed, sender=Contact.tags.through)
def crm_on_tag_change(sender, instance, action, pk_set, reverse, **kwargs):
    """
    Record tag changes on the timeline.

    Skipped entirely for bulk operations from the tag side (`reverse=True`),
    which is how the contacts page adds a tag to 5,000 people at once: that is
    one action by one person, and 5,000 timeline rows saying so is noise that
    would bury everything else on every one of those contacts.
    """
    if action not in ("post_add", "post_remove") or reverse or not pk_set:
        return
    try:
        from apps.contacts.models import ContactTag

        names = list(ContactTag.objects.filter(pk__in=pk_set).values_list("name", flat=True))
        if not names:
            return
        added = action == "post_add"
        activity.record(
            instance,
            Activity.Kind.TAG_ADDED if added else Activity.Kind.TAG_REMOVED,
            ("Tagged " if added else "Untagged ") + ", ".join(names[:5]),
            metadata={"tags": names},
        )
    except Exception:  # noqa: BLE001
        logger.exception("CRM tag handler failed for contact %s", getattr(instance, "pk", None))


@receiver(post_save, sender=Contact)
def crm_on_contact_created(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        activity.record(
            instance, Activity.Kind.CONTACT_CREATED,
            f"Contact added{f' from {instance.source}' if instance.source else ''}",
            metadata={"source": instance.source},
        )
        segments.invalidate_counts(instance.tenant_id)
    except Exception:  # noqa: BLE001
        logger.exception("CRM contact-created handler failed for %s", instance.pk)
