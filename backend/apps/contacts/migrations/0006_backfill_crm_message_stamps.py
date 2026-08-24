"""
Fill in last_inbound_at / last_outbound_at for contacts that already existed.

The two columns are written going forward by the CRM receivers, but every
contact created before this migration has them NULL -- which means every
behavioural segment ("no reply in 3 days", "hot leads") reports zero on a
database full of real conversations, and the CRM looks broken on the exact
accounts that have the most history.

Derived entirely from messaging.Message, so this is a pure recomputation of
something already true rather than an invention. Written as one UPDATE per
column with a correlated subquery, so a tenant with a million messages is two
statements rather than a million saves.
"""

from django.db import migrations
from django.db.models import Max, OuterRef, Subquery


def backfill(apps, schema_editor):
    Contact = apps.get_model("contacts", "Contact")
    Message = apps.get_model("messaging", "Message")

    inbound = (
        Message.objects.filter(contact_id=OuterRef("pk"), direction="INBOUND")
        .order_by()
        .values("contact_id")
        .annotate(latest=Max("queued_at"))
        .values("latest")[:1]
    )
    outbound = (
        Message.objects.filter(contact_id=OuterRef("pk"), direction="OUTBOUND", sent_at__isnull=False)
        .order_by()
        .values("contact_id")
        .annotate(latest=Max("sent_at"))
        .values("latest")[:1]
    )

    Contact.objects.update(last_inbound_at=Subquery(inbound))
    Contact.objects.update(last_outbound_at=Subquery(outbound))
    # last_message_at has been on the model since the start but was never
    # written by anything; while we are here it becomes the later of the two,
    # so the column finally means what its name says.
    Contact.objects.filter(last_inbound_at__isnull=False).update(last_message_at=Subquery(inbound))
    Contact.objects.filter(
        last_outbound_at__isnull=False, last_message_at__isnull=True,
    ).update(last_message_at=Subquery(outbound))


def unbackfill(apps, schema_editor):
    # Reversing is a no-op rather than a NULL-out: these columns are recomputed
    # facts, and blanking them would destroy data the forward path would
    # simply rebuild.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0005_contactimport_column_mapping"),
        ("messaging", "0010_message_template"),
    ]

    operations = [migrations.RunPython(backfill, unbackfill)]
