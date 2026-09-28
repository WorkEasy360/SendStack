"""
What each job kind does, and why running it twice is safe.

The scheduler guarantees *at least once*. Every handler here therefore has to
answer one question honestly: what happens if this runs a second time, five
minutes later, because a worker was killed after the work was done but before
the row said so?

For a campaign dispatch the answer is: nothing happens. The work is
"hand every QUEUED message to the broker", the messages themselves carry the
state, and a second pass finds none QUEUED because the first pass moved them.
Nobody is charged twice, because credits were reserved at launch and are
consumed per message, and nobody is messaged twice, because a message row can
only be sent once. The duplicate protection is not new machinery -- it is the
per-message row that was already the unit of work.
"""

import logging

from .models import ScheduledJob
from .registry import JobPrecondition, register

logger = logging.getLogger("sendstack")


@register(ScheduledJob.Kind.CAMPAIGN_DISPATCH)
def dispatch_campaign_job(job: ScheduledJob) -> None:
    """
    Send a campaign that was scheduled for now.

    **Every authorization is re-read here, none is inherited from the launch.**
    A campaign scheduled on Monday for Friday carries a decision made on
    Monday, and by Friday the workspace may be suspended, the subscription
    lapsed, the plan full, or the campaign cancelled by the customer. Trusting
    the scheduling-time decision is how a suspended customer sends ten
    thousand messages.

    The per-recipient rules -- consent, opt-out, quiet hours, the number's own
    limits, the monthly allowance claimed one message at a time -- are *not*
    re-checked here and must not be: they live in apps.messaging.gating, which
    runs immediately before each individual send. What is checked here is only
    what would make dispatching the whole campaign wrong.
    """
    from apps.campaigns.models import Campaign
    from apps.campaigns.tasks import dispatch_campaign

    campaign = Campaign.objects.select_related("tenant").filter(pk=job.object_id).first()
    if campaign is None:
        raise JobPrecondition(f"Campaign {job.object_id} no longer exists.")

    # The customer changed their mind. Checked at the moment of use, which is
    # why cancelling a campaign cannot race a worker that is already running:
    # whichever order they happen in, this read is the last word.
    if campaign.status not in (Campaign.Status.QUEUED, Campaign.Status.RUNNING):
        raise JobPrecondition(f"The campaign was {campaign.get_status_display().lower()} when its schedule came due.")

    if not campaign.tenant.is_active:
        campaign.status = Campaign.Status.PAUSED
        campaign.save(update_fields=["status", "updated_at"])
        raise JobPrecondition("The workspace is suspended.")

    # The subscription, live. A trial that expired on Wednesday must not send
    # Friday's campaign, and `access_state` answers that from the dates rather
    # than from whether a lifecycle job happened to run -- see
    # apps.subscriptions.models.Subscription.access_state.
    from apps.subscriptions import services as billing

    if billing.access_state(campaign.tenant) == "EXPIRED":
        campaign.status = Campaign.Status.PAUSED
        campaign.save(update_fields=["status", "updated_at"])
        raise JobPrecondition("The workspace has no live subscription.")

    logger.info("Scheduled campaign %s is due; dispatching.", campaign.pk)
    # Called inline rather than through the broker: this handler is already
    # running in a worker, and a second queue hop would be one more place for
    # the work to be lost. dispatch_campaign is itself safe to re-enter -- it
    # only ever acts on messages still QUEUED.
    dispatch_campaign(campaign.pk)


@register(ScheduledJob.Kind.CAMPAIGN_RESUME)
def resume_stalled_campaign_job(job: ScheduledJob) -> None:
    """
    Re-dispatch a campaign whose in-flight messages were stranded.

    **A different failure from a lost schedule, and it needed a different fix.**
    Once a campaign is dispatched, its pacing lives in per-message Celery
    countdowns -- `send_message_task.apply_async(countdown=...)` -- and a
    message that is held for quiet hours reschedules itself the same way. Those
    countdowns are in Redis. The *messages* are not: every one is a QUEUED row
    in PostgreSQL, so a Redis loss mid-campaign strands them rather than losing
    them, and the campaign simply stops with recipients still waiting.

    Nothing noticed that before. A customer had to press Resume, and only if
    they realised.

    Re-dispatching is safe because `dispatch_campaign` only ever acts on
    messages still QUEUED, and `messaging.services.attempt_send` claims each
    one with a conditional UPDATE -- so a message already in flight is claimed
    by whichever execution gets there first and the other sends nothing.
    """
    from apps.campaigns.models import Campaign
    from apps.campaigns.tasks import dispatch_campaign

    campaign = Campaign.objects.select_related("tenant").filter(pk=job.object_id).first()
    if campaign is None:
        raise JobPrecondition(f"Campaign {job.object_id} no longer exists.")
    if campaign.status not in (Campaign.Status.QUEUED, Campaign.Status.RUNNING):
        raise JobPrecondition(f"The campaign was {campaign.get_status_display().lower()}.")

    logger.warning(
        "Campaign %s had stranded messages; re-dispatching what is still queued.", campaign.pk,
    )
    dispatch_campaign(campaign.pk)
