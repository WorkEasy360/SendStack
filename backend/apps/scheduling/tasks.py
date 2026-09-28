"""
The poller, and the task that runs one job.

Two tasks rather than one, deliberately. The poller is short, does no
customer work, and must finish quickly enough to run again on the next tick;
the executor does the actual work and may take as long as the work takes.
Fusing them would mean one slow campaign delayed every other tenant's
schedule behind it.

**Polling interval and precision.** The poller runs every
``SCHEDULER_POLL_INTERVAL_SECONDS`` (default 60). A job therefore fires
somewhere between its scheduled time and one interval later, so scheduling
precision is *within one minute, late-biased, never early*. That is the honest
statement and it is what the UI should say. Tightening it is a one-variable
change, at the cost of one indexed query per tick per beat process -- the
query is a range scan over ``(status, scheduled_for)`` and returns nothing
almost every time, which is why a minute is cheap and a second would still be
affordable but pointless for a product whose unit of scheduling is "9am".
"""

import logging

from config.celery import app

from . import services

logger = logging.getLogger("sendstack")


@app.task(name="apps.scheduling.tasks.run_due_scheduled_jobs")
def run_due_scheduled_jobs():
    """
    Find work that is due, claim it, and hand each piece to a worker.

    Safe to run in as many beat processes as you like: `claim_due` uses
    ``SKIP LOCKED``, so two pollers firing the same second split the work
    rather than duplicating or blocking it.
    """
    # Reclaim first. A job stranded by a dead worker is *more* overdue than
    # anything merely waiting, and putting it back before the claim means it
    # goes out in this same tick rather than the next one.
    reclaimed = services.reclaim_stale()

    # Campaigns whose per-message countdowns went with the broker. Their
    # messages are still QUEUED rows in PostgreSQL; nothing was going to pick
    # them up. Queued as ordinary jobs so the recovery is itself durable,
    # retried and visible. See services.stall_threshold_seconds for why this
    # cannot fire on a campaign that is merely slow.
    recovered = services.schedule_stalled_campaign_recovery()

    claimed = services.claim_due()
    for job_id in claimed:
        execute_scheduled_job.delay(job_id)

    if claimed or recovered or reclaimed["requeued"] or reclaimed["failed"]:
        logger.info(
            "Scheduler tick: claimed %s, recovered %s, requeued %s, failed %s.",
            len(claimed), recovered, reclaimed["requeued"], reclaimed["failed"],
        )
    return {"claimed": len(claimed), "recovered": recovered, **reclaimed}


@app.task(name="apps.scheduling.tasks.execute_scheduled_job")
def execute_scheduled_job(job_id):
    """
    Run one claimed job.

    No Celery retry, on purpose. Retrying belongs to the durable record, not
    to the broker: a Celery retry lives in Redis and dies with it, which is
    the exact problem this app exists to fix. `services.execute` puts a
    failed job back to PENDING and the next poll picks it up -- from
    PostgreSQL.
    """
    return services.execute(job_id)
