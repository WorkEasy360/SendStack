"""
Copy each existing consent event's phone number onto the event itself.

`ConsentEvent.phone` was added so the record survives the deletion of the
contact it is about -- but a column added today is empty for every row written
before today, and an opt-out record naming nobody is not evidence of anything.
This fills them in while the contacts are all still there to be read.

Runs in batches and touches nothing it does not need to: rows whose contact has
already gone (there should be none at this point, but a partially migrated
database is a real state) simply stay blank rather than failing the migration.
"""

from django.db import migrations

BATCH = 2000


def backfill(apps, schema_editor):
    ConsentEvent = apps.get_model("compliance", "ConsentEvent")

    pending = ConsentEvent.objects.filter(phone="", contact__isnull=False)
    updated = []
    for event in pending.select_related("contact").iterator(chunk_size=BATCH):
        event.phone = (event.contact.phone or "")[:16]
        if event.phone:
            updated.append(event)
        if len(updated) >= BATCH:
            ConsentEvent.objects.bulk_update(updated, ["phone"], batch_size=BATCH)
            updated = []
    if updated:
        ConsentEvent.objects.bulk_update(updated, ["phone"], batch_size=BATCH)


def unbackfill(apps, schema_editor):
    """
    Reversible, and deliberately a no-op.

    Clearing the column on the way back would destroy exactly the evidence this
    migration exists to preserve, and the field itself is removed by the
    schema migration below this one anyway. A reverse that loses data is worse
    than one that does nothing.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("compliance", "0003_consentevent_phone_alter_consentevent_contact_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
