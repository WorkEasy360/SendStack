"""
Creating or updating one contact from an import, whatever the import was.

Two importers now feed contacts in -- an uploaded CSV and a pasted list -- and
they agree on far more than they differ. Where they agree, they have to agree
*exactly*, because the rules involved are the ones a customer would notice
being applied inconsistently:

  * an existing contact has its **gaps filled, never its data overwritten**.
    Somebody who corrected a name by hand must not have it reverted by
    re-importing the list it came from;
  * the plan's contact ceiling is charged only for rows that genuinely add
    somebody, and it is taken under the tenant's own quota lock;
  * asserted consent is applied to new and existing rows alike, through
    ``compliance.set_marketing_consent``, which never clears an opt-out --
    so re-importing an old list can never resurrect somebody who left.

What the two importers do *not* share is how they read their input, how they
report progress, and what they do when the quota runs out mid-file. Those stay
in their own callers.
"""

import logging

from apps.subscriptions.entitlements import EntitlementError
from apps.subscriptions.services import quota_guard

from .models import Contact

logger = logging.getLogger("sendstack")

#: Fields filled in on an existing contact when they are currently blank.
FILLABLE_FIELDS = ("name", "email", "company", "country")


class ContactQuotaReached(Exception):
    """The plan's contact ceiling was hit while adding this row."""

    def __init__(self, detail):
        super().__init__(str(detail))
        self.detail = detail


def upsert_contact(*, tenant, phone, defaults, custom_fields=None, groups=(), tags=(),
                   consent_source="", consent_reference="", actor=None, consent_detail=""):
    """
    Create the contact, or fill in what is missing on the one already there.

    Returns ``(contact, outcome)`` where outcome is ``"created"``,
    ``"updated"`` or ``"unchanged"`` -- the three an import report counts
    separately. Raises ``ContactQuotaReached`` when the plan has no room for a
    *new* contact; a row matching somebody already on the account never raises,
    because it adds nobody.
    """
    custom_fields = custom_fields or {}

    # Looked up first so that a row matching somebody already on the account
    # never takes the tenant's row lock: an import of 10,000 rows that adds 12
    # contacts should contend 12 times, not 10,000. get_or_create still runs
    # inside the guard, so a row that loses the race and finds the contact
    # already there is correct rather than merely lucky.
    contact = Contact.objects.filter(tenant=tenant, phone=phone).first()
    if contact is None:
        try:
            with quota_guard(getattr(tenant, "pk", tenant), "contacts"):
                contact, created = Contact.objects.get_or_create(
                    tenant=tenant, phone=phone, defaults={**defaults, "custom_fields": custom_fields},
                )
        except EntitlementError as exc:
            raise ContactQuotaReached(exc.detail) from exc
    else:
        created = False

    if consent_source:
        try:
            from apps.compliance.services import set_marketing_consent

            set_marketing_consent(
                contact, True,
                source=consent_source,
                actor=actor,
                reference=consent_reference,
                detail=consent_detail,
            )
        except Exception:  # noqa: BLE001 - one bad row must not end the import
            logger.exception("Could not record import consent for contact %s.", contact.pk)

    outcome = "created"
    if not created:
        changed_fields = []
        for field in FILLABLE_FIELDS:
            value = defaults.get(field)
            if value and not getattr(contact, field):
                setattr(contact, field, value)
                changed_fields.append(field)

        new_custom_fields = {k: v for k, v in custom_fields.items() if k not in contact.custom_fields}
        if new_custom_fields:
            contact.custom_fields = {**contact.custom_fields, **new_custom_fields}
            changed_fields.append("custom_fields")

        if changed_fields:
            contact.save(update_fields=changed_fields)
            outcome = "updated"
        else:
            outcome = "unchanged"

    if groups:
        contact.groups.add(*groups)
    if tags:
        contact.tags.add(*tags)

    return contact, outcome
