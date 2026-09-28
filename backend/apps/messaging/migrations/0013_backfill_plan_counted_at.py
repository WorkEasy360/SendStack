"""
Stamp `plan_counted_at` on outbound messages that already went out.

Without this, switching the monthly allowance on would show every existing
tenant as having sent nothing this month, and hand them a second full month's
worth of messages on the day of the deploy.

Additive and idempotent: it only ever fills a column that is currently NULL,
on rows that already reached the provider, and it uses the timestamp those
rows already carry (`sent_at`, falling back to `queued_at`) rather than
inventing one. Nothing is deleted and no existing value is overwritten, so
running it against a populated database is safe and running it twice is a
no-op. Irreversible on purpose -- the reverse is dropping the column, which
the previous migration already owns.
"""

from django.db import migrations
from django.db.models import F
from django.db.models.functions import Coalesce

#: The statuses that mean "this left, or was in the act of leaving". A
#: CANCELLED or SKIPPED message never reached anybody and never spent an
#: allowance; a FAILED one is released rather than charged (see
#: apps.subscriptions.services.release_message_allowance), so neither is
#: backfilled.
DISPATCHED = ("SENDING", "SENT", "DELIVERED", "READ")


def stamp_dispatched_messages(apps, schema_editor):
    Message = apps.get_model("messaging", "Message")
    Message.objects.filter(
        direction="OUTBOUND",
        status__in=DISPATCHED,
        plan_counted_at__isnull=True,
    ).update(plan_counted_at=Coalesce(F("sent_at"), F("queued_at")))


class Migration(migrations.Migration):

    dependencies = [
        ("messaging", "0012_message_plan_counted_at_and_more"),
    ]

    operations = [
        migrations.RunPython(stamp_dispatched_messages, migrations.RunPython.noop),
    ]
