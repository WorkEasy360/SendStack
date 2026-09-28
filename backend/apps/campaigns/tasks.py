import logging
import random

from apps.messaging.models import Message
from apps.messaging.tasks import send_message_task
from apps.tenants.models import TenantSettings
from config.celery import app

from .assignment import next_delay
from .models import Campaign

logger = logging.getLogger("sendstack")

#: Rows per bulk_update batch when persisting generated delays.
_DELAY_WRITE_BATCH = 500


def _sleep_seconds(pacing) -> float:
    """
    One sleep-mode break, in seconds.

    Random within the workspace's window, and drawn fresh each time. A fixed
    break is the same shape as a fixed delay -- perfectly regular, which is
    exactly the pattern sleep mode exists to avoid -- so a configuration with
    min == max still gets that one value, but anything wider varies.
    """
    minimum = float(pacing.sleep_min_seconds or 0)
    maximum = float(pacing.sleep_max_seconds or 0)
    if maximum < minimum:
        maximum = minimum
    return random.uniform(minimum, maximum) if maximum > minimum else minimum


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

    # The workspace's pacing rules, read once. Both are additions to the same
    # cumulative countdown below, which is what makes them real rather than
    # advisory: a message's eta is the sum of every gap in front of it, so a
    # batch pause or a sleep genuinely holds back everything behind it.
    pacing = TenantSettings.for_tenant(campaign.tenant_id)
    batch_size = int(pacing.batch_size or 0)
    batch_pause = float(pacing.batch_pause_seconds or 0)
    sleep_after = int(pacing.sleep_after_messages or 0) if pacing.sleep_enabled else 0

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
    position = 0
    pauses = 0.0
    for message_id, to_phone, account_id, account_name in pending.iterator(chunk_size=_DELAY_WRITE_BATCH):
        delay = next_delay(campaign)
        cumulative += delay

        # Applied *before* this message rather than after the previous one, so
        # the pause lands between batch n and batch n+1 and never after the
        # last message of the campaign -- where it would be a delay with
        # nothing behind it.
        if position and batch_size and batch_pause and position % batch_size == 0:
            cumulative += batch_pause
            pauses += batch_pause
        if position and sleep_after and position % sleep_after == 0:
            nap = _sleep_seconds(pacing)
            cumulative += nap
            pauses += nap
            logger.info(
                "Campaign %s | sleeping %.0fs after %s messages", campaign.pk, nap, position,
            )

        position += 1
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
        "Campaign %s | dispatched %s message(s) over ~%.1f seconds "
        "(%.1fs of it batch pauses and sleep) | delay window: %.3fs-%.3fs",
        campaign.pk, len(schedule), cumulative, pauses, minimum, maximum,
    )
