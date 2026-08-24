"""
Moving a contact through NEW -> ENGAGED -> ... without anybody clicking.

The product promise is that a normal user never has to learn CRM vocabulary.
That only holds if the fields the CRM sorts and segments by fill themselves
in, so these transitions are driven by things that already happen: a reply
arrives, a deal is won, a month goes by in silence.

Two rules keep this safe to run automatically:

* **It never moves a contact backwards past a human.** A status a person set
  by hand is left alone by the silence sweep -- see `advance_to`, which only
  promotes along the ladder, and `mark_inactive_where_silent`, which only
  touches NEW/ENGAGED.
* **It never overwrites a terminal answer.** CUSTOMER and LOST are decisions,
  not observations; nothing here demotes them.
"""

import logging
from datetime import timedelta

from django.utils import timezone

from apps.contacts.models import Contact

from . import activity
from .models import Activity

logger = logging.getLogger("multisender")

#: How far along the ladder each status sits. Automatic promotion may only
#: move a contact *up* this order, so an automation cannot undo a person.
_RANK = {
    Contact.Lifecycle.NEW: 0,
    Contact.Lifecycle.INACTIVE: 0,
    Contact.Lifecycle.ENGAGED: 1,
    Contact.Lifecycle.QUALIFIED: 2,
    Contact.Lifecycle.CUSTOMER: 3,
    Contact.Lifecycle.LOST: 3,
}

#: A contact nobody has heard from in this long stops being "engaged".
INACTIVE_AFTER_DAYS = 60


def set_status(contact, status, *, actor=None, reason="") -> bool:
    """Set the status and record the change. Returns whether anything moved."""
    if contact.lifecycle_status == status:
        return False
    previous = contact.lifecycle_status
    contact.lifecycle_status = status
    contact.save(update_fields=["lifecycle_status", "updated_at"])
    activity.record(
        contact, Activity.Kind.STATUS_CHANGED,
        f"Status changed from {previous.title()} to {status.title()}"
        + (f" ({reason})" if reason else ""),
        actor=actor, metadata={"from": previous, "to": status, "reason": reason},
    )
    return True


def advance_to(contact, status, *, reason="") -> bool:
    """
    Promote to `status`, but only if it is further along than where they are.

    This is the entry point every automatic caller uses. A contact a manager
    marked CUSTOMER stays CUSTOMER when they send a chatty message the next
    day, because ENGAGED ranks below it.
    """
    if _RANK.get(status, 0) <= _RANK.get(contact.lifecycle_status, 0):
        return False
    return set_status(contact, status, reason=reason)


def on_inbound_reply(contact) -> bool:
    """They answered us: they are at least engaged."""
    return advance_to(contact, Contact.Lifecycle.ENGAGED, reason="they replied")


def on_deal_won(contact) -> bool:
    return advance_to(contact, Contact.Lifecycle.CUSTOMER, reason="a deal was won")


def on_deal_opened(contact) -> bool:
    return advance_to(contact, Contact.Lifecycle.QUALIFIED, reason="a deal was opened")


def mark_inactive_where_silent(tenant, *, days=INACTIVE_AFTER_DAYS, limit=5000) -> int:
    """
    Cool down contacts nobody has heard from in `days`.

    Only NEW and ENGAGED are touched. QUALIFIED means somebody opened a deal,
    CUSTOMER means they bought, LOST means somebody decided -- none of those
    become untrue because a chat went quiet, and quietly rewriting them would
    make the status field something users stop trusting.
    """
    cutoff = timezone.now() - timedelta(days=days)
    stale = Contact.objects.filter(
        tenant=tenant,
        lifecycle_status__in=[Contact.Lifecycle.NEW, Contact.Lifecycle.ENGAGED],
    ).filter(last_inbound_at__lt=cutoff) | Contact.objects.filter(
        tenant=tenant,
        lifecycle_status=Contact.Lifecycle.ENGAGED,
        last_inbound_at__isnull=True,
        created_at__lt=cutoff,
    )

    changed = 0
    for contact in stale.distinct().order_by("pk")[:limit]:
        if set_status(contact, Contact.Lifecycle.INACTIVE, reason=f"no reply in {days} days"):
            changed += 1
    return changed
