import logging

from django.utils import timezone

from config.celery import app

from .mapping import custom_fields_for, detect_columns, row_value
from .models import ContactGroup, ContactImport, ContactTag
# The CSV decoder lives in readers.py now, beside the Excel reader; it is
# re-exported here because the streaming tests import it from this module.
from .readers import _READ_CHUNK_BYTES, _decoded_lines, open_rows  # noqa: F401
from .upsert import ContactQuotaReached, upsert_contact
from .utils import normalize_phone

logger = logging.getLogger("sendstack")

_MAX_STORED_ERRORS = 200


@app.task(bind=True)
def process_contact_import(self, import_id: int):
    try:
        contact_import = ContactImport.objects.select_related("tenant").get(pk=import_id)
    except ContactImport.DoesNotExist:
        logger.warning("ContactImport %s no longer exists.", import_id)
        return

    contact_import.status = ContactImport.Status.PROCESSING
    contact_import.save(update_fields=["status"])

    groups = list(ContactGroup.objects.filter(tenant=contact_import.tenant, id__in=contact_import.group_ids))
    tags = list(ContactTag.objects.filter(tenant=contact_import.tenant, id__in=contact_import.tag_ids))

    errors = []
    total_rows = created_count = updated_count = skipped_count = error_count = 0
    quota_stopped = False

    try:
        # CSV or Excel; readers.open_rows settles which and hands back text
        # rows either way.
        headers, reader = open_rows(contact_import.file, contact_import.file.name)
        # Headers are matched to fields rather than required to be exact, so a
        # file exported from a phone ("Mobile No.", "Firm") imports without a
        # column-mapping screen. See apps/contacts/mapping.py.
        mapping = detect_columns(headers)
        if "phone" not in mapping:
            raise ValueError(
                "No phone column found. Name one of the columns 'phone', 'mobile' or 'whatsapp'."
            )
        contact_import.column_mapping = mapping
        contact_import.save(update_fields=["column_mapping"])

        for row_number, row in enumerate(reader, start=2):
            total_rows += 1
            raw_phone = row_value(row, mapping, "phone")

            try:
                phone = normalize_phone(raw_phone)
            except ValueError:
                error_count += 1
                if len(errors) < _MAX_STORED_ERRORS:
                    errors.append({"row": row_number, "error": f"Invalid phone number: '{raw_phone}'"})
                continue

            custom_fields = custom_fields_for(row, mapping)
            defaults = {
                "name": row_value(row, mapping, "name"),
                "email": row_value(row, mapping, "email"),
                "company": row_value(row, mapping, "company"),
                "country": row_value(row, mapping, "country"),
                "custom_fields": custom_fields,
                "created_by_id": contact_import.created_by_id,
            }

            # The plan's ceiling, the gap-filling update rule and the consent
            # assertion all live in `upsert_contact`, shared with the pasted
            # importer -- see apps/contacts/upsert.py for why those three in
            # particular must not have two implementations.
            try:
                _, outcome = upsert_contact(
                    tenant=contact_import.tenant,
                    phone=phone,
                    defaults=defaults,
                    custom_fields=custom_fields,
                    groups=groups,
                    tags=tags,
                    consent_source=contact_import.consent_source,
                    consent_reference=contact_import.consent_reference,
                    actor=contact_import.created_by,
                    consent_detail=f"Declared on import #{contact_import.pk}.",
                )
            except ContactQuotaReached as exc:
                # Everything imported so far stays imported. Stopping is
                # the whole point -- silently dropping the rest, or
                # rolling back the part that worked, would both leave
                # somebody guessing which of their contacts arrived.
                quota_stopped = True
                errors.append({"row": row_number, "error": str(exc.detail)})
                error_count += 1
                break

            if outcome == "created":
                created_count += 1
            elif outcome == "updated":
                updated_count += 1
            else:
                skipped_count += 1

        contact_import.status = ContactImport.Status.COMPLETED
        if quota_stopped:
            # Counted, not created. The rows past the one that ran out of room
            # were never examined, so they are reported as skipped rather than
            # as errors -- there is nothing wrong with them, and the person
            # reading this report needs to know how many contacts are still
            # waiting on an upgrade.
            remaining = sum(1 for _ in reader)
            total_rows += remaining
            skipped_count += remaining
    except Exception as exc:  # noqa: BLE001 - always finish the import record, even on unexpected failures
        logger.exception("Contact import %s failed", import_id)
        contact_import.status = ContactImport.Status.FAILED
        error_count += 1
        errors.append({"row": None, "error": str(exc)})

    contact_import.total_rows = total_rows
    contact_import.created_count = created_count
    contact_import.updated_count = updated_count
    contact_import.skipped_count = skipped_count
    contact_import.error_count = error_count
    contact_import.errors = errors
    contact_import.completed_at = timezone.now()
    contact_import.save(
        update_fields=[
            "status", "total_rows", "created_count", "updated_count",
            "skipped_count", "error_count", "errors", "completed_at",
        ]
    )
