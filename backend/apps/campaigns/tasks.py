import logging

from apps.messaging.models import Message
from apps.messaging.tasks import send_message_task
from config.celery import app

from .assignment import next_delay
from .models import Campaign

logger = logging.getLogger("multisender")

#: Rows per bulk_update batch when persisting generated delays.
_DELAY_WRITE_BATCH = 500


@app.task
def dispatch_campaign(campaign_id):
    try:
        campaign = Campaign.objects.get(pk=campaign_id)
    except Campaign.DoesNotExist:
        logger.warning("Campaign %s no longer exists.", campaign_id)
        return

    if campaign.status not in (Campaign.Status.QUEUED, Campaign.Status.RUNNING):
        return

    if campaign.status == Campaign.Status.QUEUED:
        campaign.status = Campaign.Status.RUNNING
        campaign.save(update_fields=["status", "updated_at"])

    minimum, maximum = campaign.delay_bounds()

    # order_by("id") is required, not cosmetic: Message.Meta.ordering is
    # "-queued_at" (newest first, for list views), so an unordered queryset
    # here hands out send slots in reverse -- the last recipient in the
    # audience gets messaged first and the first one waits out the whole
    # campaign. queued_at is also auto_now_add, so messages created in the
    # same tick tie and order arbitrarily; the primary key can't.
    pending = campaign.messages.filter(status=Message.Status.QUEUED).order_by("id").values_list(
        "id", "to_phone", "whatsapp_account_id", "whatsapp_account__name",
    )

    # A *fresh* delay is drawn for every single message -- never one delay
    # computed once and reused. The delays are cumulative because Celery's
    # countdown is measured from now rather than from the previous send, so
    # summing them is what turns "wait d before message n" into an actual
    # per-message gap of d. Nothing blocks here: the worker hands each
    # message to the broker with its own eta and returns immediately, and a
    # worker restart loses nothing because every message keeps its own
    # QUEUED row (see resume/retry, which simply re-dispatch what's left).
    schedule = []
    delayed_rows = []
    cumulative = 0.0
    for message_id, to_phone, account_id, account_name in pending.iterator(chunk_size=_DELAY_WRITE_BATCH):
        delay = next_delay(campaign)
        cumulative += delay
        schedule.append((message_id, cumulative))
        delayed_rows.append(Message(pk=message_id, assigned_delay_seconds=delay))
        logger.info(
            "Campaign %s | recipient %s | selected account: %s (id=%s) | generated delay: %.3f seconds | queued for send",
            campaign.pk, to_phone, account_name, account_id, delay,
        )

    if not schedule:
        return

    # Persisted before dispatch so a worker that picks a message up
    # immediately still sees the delay that was generated for it, and so the
    # campaign report can show what actually happened.
    Message.objects.bulk_update(delayed_rows, ["assigned_delay_seconds"], batch_size=_DELAY_WRITE_BATCH)

    for message_id, countdown in schedule:
        send_message_task.apply_async(args=[message_id], countdown=countdown)

    logger.info(
        "Campaign %s | dispatched %s message(s) over ~%.1f seconds | delay window: %.3fs-%.3fs",
        campaign.pk, len(schedule), cumulative, minimum, maximum,
    )
