"""
Claiming, running, rescheduling and reclaiming durable jobs.

Everything in this module is written for a world where **any worker can die at
any instant**, including between the provider accepting a message and the row
recording that it did. The three mechanisms that make that survivable:

* **Claiming is a database operation, not a decision.** Two workers polling
  the same second both run ``SELECT ... FOR UPDATE SKIP LOCKED``; Postgres
  hands each of them a disjoint set of rows and neither waits for the other.
  There is no window in which both believe they own the same job.
* **The attempt is spent when the job is claimed, not when it fails.** A
  worker that is killed mid-job never reports anything, so counting failures
  would count zero and the job would be retried for ever. Counting claims
  bounds it.
* **The lease expires.** A job left PROCESSING by a dead worker is returned to
  PENDING once its lease is older than ``LEASE_SECONDS``, which is the only
  reason a redeploy in the middle of a campaign is survivable.

None of this makes a job run *exactly* once -- nothing can, across a process
boundary and a network. It makes a job run *at least* once and makes running
it twice harmless, which is the achievable version. The harmlessness lives in
the handlers; see apps/scheduling/handlers.py.
"""

import logging
import os
import socket
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db import models
from django.db.models import F
from django.utils import timezone

from .models import ScheduledJob
from .registry import JobPrecondition, UnknownJobKind, handler_for

logger = logging.getLogger("sendstack")


class JobInFlight(Exception):
    """This object's scheduled job is already executing and cannot be moved."""


def _setting(name, default):
    try:
        return int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def lease_seconds() -> int:
    """
    How long a claim is honoured before the job is treated as abandoned.

    Must comfortably exceed the longest a handler can legitimately take.
    Dispatching a campaign hands its messages to the broker and returns -- it
    does not wait for them to send -- so the default is generous by an order
    of magnitude rather than tight.
    """
    return max(_setting("SCHEDULER_LEASE_SECONDS", 900), 60)


def batch_size() -> int:
    return max(_setting("SCHEDULER_BATCH_SIZE", 100), 1)


def worker_identity() -> str:
    """Which process holds a lease. For the operator reading a stuck row, not for logic."""
    return f"{socket.gethostname()}:{os.getpid()}"[:120]


# ---------------------------------------------------------------------------
# Writing the schedule
# ---------------------------------------------------------------------------

def live_job(kind: str, object_id: int):
    """The PENDING or PROCESSING job for this object, if there is one."""
    return ScheduledJob.objects.filter(
        kind=kind, object_id=object_id, status__in=ScheduledJob.LIVE_STATUSES,
    ).first()


@transaction.atomic
def schedule(*, kind: str, object_id: int, tenant_id: int, run_at, max_attempts: int = 3) -> ScheduledJob:
    """
    Record that this object should be acted on at ``run_at``.

    Rescheduling is an **update of the same row**, never a second row: the
    partial unique constraint would refuse a second live job anyway, and
    silently leaving the old one behind is precisely the stale-schedule bug
    that Redis ETAs had -- an edit that moved a campaign to Friday but left
    Tuesday's dispatch armed.

    Refuses to move a job that is already executing. There is nothing honest
    to do there: the work has started, and saying "rescheduled" would be a
    lie the customer would find out about when it sent anyway.
    """
    existing = (
        ScheduledJob.objects.select_for_update()
        .filter(kind=kind, object_id=object_id, status__in=ScheduledJob.LIVE_STATUSES)
        .first()
    )

    if existing is not None:
        if existing.status == ScheduledJob.Status.PROCESSING:
            raise JobInFlight(f"{kind}:{object_id} is already running.")
        existing.scheduled_for = run_at
        existing.max_attempts = max_attempts
        existing.attempts = 0
        existing.last_error = ""
        existing.locked_at = None
        existing.locked_by = ""
        existing.save(update_fields=[
            "scheduled_for", "max_attempts", "attempts", "last_error", "locked_at", "locked_by", "updated_at",
        ])
        logger.info("Rescheduled %s:%s for %s.", kind, object_id, run_at.isoformat())
        return existing

    job = ScheduledJob.objects.create(
        tenant_id=tenant_id, kind=kind, object_id=object_id, scheduled_for=run_at, max_attempts=max_attempts,
    )
    logger.info("Scheduled %s:%s for %s.", kind, object_id, run_at.isoformat())
    return job


@transaction.atomic
def cancel(*, kind: str, object_id: int, reason: str = "") -> int:
    """
    Stop this object's scheduled execution.

    Cancels a PROCESSING job as well as a PENDING one. That does not reach
    into a running worker -- nothing can -- and it is not what protects a
    cancelled campaign from sending. What protects it is the handler
    re-reading the campaign's own status before it does anything (see
    handlers.dispatch_campaign_job), which is a check that cannot be raced
    because it happens at the moment of use. Marking the row is for the
    operator and the audit trail.
    """
    updated = (
        ScheduledJob.objects.filter(
            pk__in=ScheduledJob.objects.select_for_update()
            .filter(kind=kind, object_id=object_id, status__in=ScheduledJob.LIVE_STATUSES)
            .values("pk")
        )
        .update(
            status=ScheduledJob.Status.CANCELLED,
            last_error=reason[:500],
            locked_at=None,
            locked_by="",
            updated_at=timezone.now(),
        )
    )
    if updated:
        logger.info("Cancelled %s scheduled job(s) for %s:%s.", updated, kind, object_id)
    return updated


# ---------------------------------------------------------------------------
# Reading the schedule
# ---------------------------------------------------------------------------

@transaction.atomic
def claim_due(*, now=None, limit: int = None, worker: str = None) -> list:
    """
    Take ownership of up to ``limit`` due jobs, and return their ids.

    ``SKIP LOCKED`` is the whole design. Without it, two pollers running the
    same second serialise: the second blocks on the first's row locks, wakes
    up to find every row already claimed, and has spent a connection to
    discover it. With it, each poller takes what nobody else holds and neither
    one waits.

    The claim and the status change are one transaction, so a job is either
    unclaimed or claimed by exactly one worker -- never both, never neither.
    """
    now = now or timezone.now()
    limit = limit or batch_size()
    worker = worker or worker_identity()

    ids = list(
        ScheduledJob.objects.select_for_update(skip_locked=True)
        .filter(status=ScheduledJob.Status.PENDING, scheduled_for__lte=now)
        .order_by("scheduled_for", "id")
        .values_list("id", flat=True)[:limit]
    )
    if not ids:
        return []

    ScheduledJob.objects.filter(pk__in=ids).update(
        status=ScheduledJob.Status.PROCESSING,
        locked_at=now,
        locked_by=worker,
        # Spent on the claim, not on the failure: a worker killed mid-job
        # reports nothing, and a retry budget that only counts reported
        # failures never runs out.
        attempts=F("attempts") + 1,
        updated_at=now,
    )
    return ids


@transaction.atomic
def reclaim_stale(*, now=None) -> dict:
    """
    Return jobs whose worker died to the pool, and bury the ones out of attempts.

    A job sitting in PROCESSING with nobody running it is the failure mode
    that strands scheduled campaigns for ever, and it is produced by something
    entirely routine: a deploy. This is what makes a redeploy mid-campaign a
    delay rather than a lost campaign.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=lease_seconds())

    rows = list(
        ScheduledJob.objects.select_for_update(skip_locked=True)
        .filter(status=ScheduledJob.Status.PROCESSING, locked_at__lt=cutoff)
        .values_list("id", "attempts", "max_attempts")
    )
    if not rows:
        return {"requeued": 0, "failed": 0}

    requeue = [pk for pk, attempts, cap in rows if attempts < cap]
    exhausted = [pk for pk, attempts, cap in rows if attempts >= cap]

    requeued = failed = 0
    if requeue:
        requeued = ScheduledJob.objects.filter(pk__in=requeue).update(
            status=ScheduledJob.Status.PENDING, locked_at=None, locked_by="",
            last_error="The worker holding this job stopped responding; it was returned to the queue.",
            updated_at=now,
        )
    if exhausted:
        failed = ScheduledJob.objects.filter(pk__in=exhausted).update(
            status=ScheduledJob.Status.FAILED, locked_at=None, locked_by="",
            last_error="The worker holding this job stopped responding and no attempts remained.",
            updated_at=now,
        )

    if requeued or failed:
        logger.warning("Reclaimed stale scheduled jobs: %s requeued, %s failed.", requeued, failed)
    return {"requeued": requeued, "failed": failed}


# ---------------------------------------------------------------------------
# Running one job
# ---------------------------------------------------------------------------

def execute(job_id: int, *, now=None) -> str:
    """
    Run one claimed job and record what happened. Returns the resulting status.

    Reads the row fresh rather than trusting anything the caller carried: the
    job may have been cancelled between the claim and this call, and a worker
    that acted on a stale copy would send a campaign the customer stopped.

    Never raises for an ordinary handler failure -- the outcome belongs on the
    row, where an operator can see it, rather than in a traceback in a worker
    log nobody is reading.
    """
    now = now or timezone.now()
    job = ScheduledJob.objects.filter(pk=job_id).first()
    if job is None:
        logger.warning("Scheduled job %s no longer exists.", job_id)
        return ScheduledJob.Status.CANCELLED

    if job.status != ScheduledJob.Status.PROCESSING:
        # Cancelled while queued, or already finished by a previous delivery
        # of this same task. Both are ordinary: Celery may deliver a task more
        # than once, and doing nothing is the correct response to the second.
        logger.info("Scheduled job %s is %s, not running it.", job_id, job.status)
        return job.status

    try:
        handler = handler_for(job.kind)
    except UnknownJobKind:
        logger.exception("No handler registered for scheduled job kind %s.", job.kind)
        return _finish(job, ScheduledJob.Status.FAILED, now, f"No handler is registered for '{job.kind}'.")

    try:
        handler(job)
    except JobPrecondition as exc:
        # Not a failure. The job looked, the world had moved on, and it
        # correctly did nothing. Retrying would look again and decide the same
        # thing, so it is finished -- with the reason kept where an operator
        # answering "why didn't my campaign send?" will find it.
        logger.info("Scheduled job %s (%s:%s) did nothing: %s", job.pk, job.kind, job.object_id, exc)
        return _finish(job, ScheduledJob.Status.COMPLETED, now, str(exc)[:2000])
    except Exception as exc:  # noqa: BLE001 - the outcome is data, not a crash
        logger.exception("Scheduled job %s (%s:%s) failed.", job.pk, job.kind, job.object_id)
        detail = f"{type(exc).__name__}: {exc}"[:2000]
        if job.attempts >= job.max_attempts:
            return _finish(job, ScheduledJob.Status.FAILED, now, detail)
        # Back to PENDING at its original time, so the next poll picks it up
        # immediately. No backoff: these are wall-clock appointments that are
        # already late, and a customer's 9am campaign going out at 9:01 after
        # a blip is better than one going out at 9:30.
        job.status = ScheduledJob.Status.PENDING
        job.locked_at = None
        job.locked_by = ""
        job.last_error = detail
        job.save(update_fields=["status", "locked_at", "locked_by", "last_error", "updated_at"])
        return ScheduledJob.Status.PENDING

    return _finish(job, ScheduledJob.Status.COMPLETED, now, "")


def _finish(job, status, now, error) -> str:
    job.status = status
    job.executed_at = now
    job.locked_at = None
    job.locked_by = ""
    job.last_error = error
    job.save(update_fields=["status", "executed_at", "locked_at", "locked_by", "last_error", "updated_at"])
    return status


# ---------------------------------------------------------------------------
# Operational view
# ---------------------------------------------------------------------------

def overdue_count(*, now=None, tolerance_seconds: int = 300) -> int:
    """
    Jobs that should have run and have not.

    The one number that says whether the scheduler is alive. A poller that
    stopped shows up here within minutes; nothing else in the system would
    notice for as long as the customer took to complain.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=tolerance_seconds)
    return ScheduledJob.objects.filter(
        status=ScheduledJob.Status.PENDING, scheduled_for__lt=cutoff,
    ).count()


# ---------------------------------------------------------------------------
# Stranded campaigns
# ---------------------------------------------------------------------------

def stall_threshold_seconds() -> int:
    """
    How long a campaign must show no movement at all before it counts as stuck.

    **Deliberately generous.** A large campaign paces itself over hours --
    ten thousand messages at a second apart is nearly three hours of countdowns
    -- so a short threshold would "rescue" campaigns that are working perfectly
    and compress the pacing the customer chose. What this looks for is not
    slowness but *silence*: no message in the campaign has changed state at all
    for this long, which a healthy campaign of any size never does.
    """
    return max(_setting("CAMPAIGN_STALL_SECONDS", 3600), 300)


def find_stalled_campaigns(*, now=None, limit: int = 50) -> list:
    """
    Campaigns that still have queued recipients and have stopped moving.

    The query is deliberately narrow: only campaigns already in flight, only
    those with something left to send, and only those whose most recent message
    activity is older than the threshold. A campaign that is merely slow has
    *some* message that moved recently and is left alone.
    """
    from django.db.models import Max

    from apps.campaigns.models import Campaign
    from apps.messaging.models import Message

    now = now or timezone.now()
    cutoff = now - timedelta(seconds=stall_threshold_seconds())

    in_flight = Campaign.objects.filter(
        status__in=(Campaign.Status.QUEUED, Campaign.Status.RUNNING),
    ).values_list("id", flat=True)[:500]

    rows = (
        Message.objects.filter(campaign_id__in=list(in_flight))
        .values("campaign_id")
        .annotate(last_activity=Max("updated_at"), queued=models.Count("id", filter=models.Q(status=Message.Status.QUEUED)))
        .filter(queued__gt=0, last_activity__lt=cutoff)
        .order_by("last_activity")[:limit]
    )
    return [row["campaign_id"] for row in rows]


def schedule_stalled_campaign_recovery(*, now=None) -> int:
    """
    Queue a resume for every campaign that has stopped moving. Returns how many.

    Goes through the same durable job table as everything else rather than
    calling dispatch directly, so the recovery is itself recorded, retried and
    visible -- and so the partial unique constraint stops a second sweep
    queueing a second resume for a campaign that already has one pending.
    """
    from apps.campaigns.models import Campaign

    now = now or timezone.now()
    scheduled = 0
    for campaign_id in find_stalled_campaigns(now=now):
        if live_job(ScheduledJob.Kind.CAMPAIGN_RESUME, campaign_id) is not None:
            continue
        tenant_id = (
            Campaign.objects.filter(pk=campaign_id).values_list("tenant_id", flat=True).first()
        )
        if tenant_id is None:
            continue
        try:
            schedule(
                kind=ScheduledJob.Kind.CAMPAIGN_RESUME,
                object_id=campaign_id,
                tenant_id=tenant_id,
                run_at=now,
            )
            scheduled += 1
        except JobInFlight:
            continue
    if scheduled:
        logger.warning("Queued recovery for %s stalled campaign(s).", scheduled)
    return scheduled
