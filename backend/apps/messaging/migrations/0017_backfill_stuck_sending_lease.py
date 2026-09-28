"""
One-time reconciliation for messages already stranded in SENDING.

Before the send lease existed, a worker that died between the provider
accepting a message and the row recording it left the message in SENDING for
ever -- nothing ever looked at it again. This migration does NOT reset those
to QUEUED (a blind resend is a duplicate). It stamps an already-expired lease
on them so the recovery sweep (apps/messaging/recovery.py) picks them up on
its first run and classifies each one: with no attempt row to reason from, a
legacy SENDING message becomes RECOVERY_REQUIRED -- visible in campaign health
and the operator recovery list, settled by a human or a later reconciliation,
never silently resent and never silently dropped.

Campaign counters and stuck-RUNNING campaigns are left to that first sweep,
which recomputes them as it moves each message.
"""

from datetime import timedelta

from django.db import migrations


def stamp_expired_lease(apps, schema_editor):
    Message = apps.get_model("messaging", "Message")
    from django.utils import timezone

    past = timezone.now() - timedelta(days=1)
    Message.objects.filter(status="SENDING").update(
        lease_owner="", lease_expires_at=past, lease_heartbeat_at=None,
    )


def noop_reverse(apps, schema_editor):
    # Nothing to undo: clearing the lease again would just re-strand the rows.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("messaging", "0016_messagesendattempt_message_lease_expires_at_and_more"),
    ]

    operations = [
        migrations.RunPython(stamp_expired_lease, noop_reverse),
    ]
