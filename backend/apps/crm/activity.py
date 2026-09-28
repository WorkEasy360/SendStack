"""
Writing to the contact timeline.

One function, called from every place that changes a contact's CRM state, so
a timeline entry can never be half-written or forgotten in one code path and
present in another. Recording is best-effort by construction: a timeline is a
convenience, and failing to write one must never roll back the change it was
describing.
"""

import logging

from .models import Activity

logger = logging.getLogger("sendstack")


def record(contact, kind, summary, *, actor=None, metadata=None, occurred_at=None) -> Activity | None:
    """Append one entry to `contact`'s timeline."""
    if contact is None or not getattr(contact, "pk", None):
        return None
    try:
        return Activity.objects.create(
            tenant_id=contact.tenant_id,
            contact=contact,
            kind=kind,
            summary=summary[:255],
            metadata=metadata or {},
            actor=actor if getattr(actor, "pk", None) else None,
            **({"occurred_at": occurred_at} if occurred_at else {}),
        )
    except Exception:  # noqa: BLE001 - a lost timeline row must not lose the change itself
        logger.exception("Could not record %s activity for contact %s", kind, contact.pk)
        return None
