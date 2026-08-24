import csv
import io
import logging

from django.utils import timezone

from config.celery import app

from .mapping import custom_fields_for, detect_columns, row_value
from .models import Contact, ContactGroup, ContactImport, ContactTag
from .utils import normalize_phone

logger = logging.getLogger("multisender")

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

    try:
        raw = contact_import.file.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(raw))
        # Headers are matched to fields rather than required to be exact, so a
        # file exported from a phone ("Mobile No.", "Firm") imports without a
        # column-mapping screen. See apps/contacts/mapping.py.
        mapping = detect_columns(reader.fieldnames)
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

            contact, was_created = Contact.objects.get_or_create(
                tenant=contact_import.tenant,
                phone=phone,
                defaults={
                    "name": row_value(row, mapping, "name"),
                    "email": row_value(row, mapping, "email"),
                    "company": row_value(row, mapping, "company"),
                    "country": row_value(row, mapping, "country"),
                    "custom_fields": custom_fields,
                    "created_by_id": contact_import.created_by_id,
                },
            )

            if was_created:
                created_count += 1
            else:
                # Fill in gaps on the existing contact rather than overwriting data
                # someone may have edited by hand since the last import.
                changed_fields = []
                for field in ("name", "email", "company", "country"):
                    value = row_value(row, mapping, field)
                    if value and not getattr(contact, field):
                        setattr(contact, field, value)
                        changed_fields.append(field)
                new_custom_fields = {k: v for k, v in custom_fields.items() if k not in contact.custom_fields}
                if new_custom_fields:
                    contact.custom_fields = {**contact.custom_fields, **new_custom_fields}
                    changed_fields.append("custom_fields")

                if changed_fields:
                    contact.save(update_fields=changed_fields)
                    updated_count += 1
                else:
                    skipped_count += 1

            if groups:
                contact.groups.add(*groups)
            if tags:
                contact.tags.add(*tags)

        contact_import.status = ContactImport.Status.COMPLETED
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
