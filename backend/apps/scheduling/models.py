"""
Scheduled work that survives the loss of the broker.

Before this, "send this campaign at 9am on Friday" was
``dispatch_campaign.apply_async(eta=...)`` and nothing else. That instruction
existed in exactly one place -- a Redis list -- and Redis in this deployment is
a cache: it has no persistence guarantee, it is flushed by a restart with the
wrong config, and losing it is an ordinary operational event rather than a
disaster. A customer's Friday campaign disappearing because a cache was
restarted on Wednesday is not a failure mode a billing system may have.

So the durable record moves into PostgreSQL, which is the database that is
backed up, and Redis goes back to being what it is good at: carrying a job
that is due *right now* to whichever worker is free. The chain is

    ScheduledJob (Postgres, the truth)
        -> the poller finds it due
        -> Celery carries it to a worker
        -> the handler runs

and every link after the first is replaceable and re-runnable. Redis losing
everything costs at most one polling interval of latency.

**This model is deliberately not a copy of Campaign.** It stores when
something should happen and whether it has, and nothing about what the thing
is: `kind` plus `object_id` names a row in the table that already holds all of
that. A scheduled job that duplicated the campaign's audience or message would
be a second version of the truth, and the two would disagree the first time
somebody edited the campaign.
"""

from django.db import models
from django.utils import timezone


class ScheduledJob(models.Model):
    """
    One future execution of one thing, owned by one tenant.

    The lifecycle is::

        PENDING --(a worker claims it)--> PROCESSING --> COMPLETED
                                                     |-> FAILED (attempts spent)
                                                     |-> PENDING (retry)
        PENDING --(the customer changes their mind)--> CANCELLED

    `locked_at` is a **lease**, not a lock: a worker that dies holding a job
    leaves it PROCESSING for ever unless somebody reclaims it, so the runner
    treats a lease older than `LEASE_SECONDS` as abandoned and puts the job
    back. That is the difference between a scheduler that loses work when a
    container is redeployed and one that does not.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    class Kind(models.TextChoices):
        CAMPAIGN_DISPATCH = "CAMPAIGN_DISPATCH", "Dispatch a scheduled campaign"
        # Re-dispatch a campaign whose per-message countdowns were lost with
        # the broker. Its messages are still QUEUED rows in PostgreSQL; what
        # went is the pacing that was going to pick them up.
        CAMPAIGN_RESUME = "CAMPAIGN_RESUME", "Re-dispatch a stalled campaign"

    #: The two states in which a job still has a future. Everything that asks
    #: "is this already scheduled" asks about these.
    LIVE_STATUSES = (Status.PENDING, Status.PROCESSING)

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="scheduled_jobs")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    # The primary key of the row this job acts on, in the table `kind` names.
    # Not a GenericForeignKey: that would add two joins to the hottest query
    # in the module for the sake of a type this file already knows.
    object_id = models.PositiveIntegerField()

    scheduled_for = models.DateTimeField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)

    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=3)

    locked_at = models.DateTimeField(null=True, blank=True)
    # Which worker holds the lease. Recorded for the operator reading a stuck
    # job at 3am, never read by the code -- reclaiming is decided by the age
    # of the lease, because a worker that can no longer answer for itself is
    # exactly the case this has to handle.
    locked_by = models.CharField(max_length=120, blank=True)

    executed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["scheduled_for", "id"]
        indexes = [
            # The poller's only query: "PENDING and due, oldest first". A
            # composite in this column order lets one index range-scan both
            # halves of it; separate indexes on each would make Postgres pick
            # one and filter the rest of the table by hand.
            models.Index(fields=["status", "scheduled_for"], name="sched_status_due_idx"),
            # "Is this campaign scheduled, and when?" -- read by the campaign
            # detail page and by every reschedule.
            models.Index(fields=["kind", "object_id"], name="sched_kind_object_idx"),
            models.Index(fields=["tenant", "status"], name="sched_tenant_status_idx"),
        ]
        constraints = [
            # **The duplicate-send protection, at the scheduling layer.** One
            # live job per thing, enforced by the database rather than by
            # remembering to check: two PENDING jobs for one campaign is two
            # dispatches of that campaign, and no amount of care in the
            # service layer survives two requests arriving at once.
            #
            # Partial, so the history stays: a campaign scheduled, cancelled
            # and scheduled again keeps both rows, and only one of them is
            # live.
            models.UniqueConstraint(
                fields=["kind", "object_id"],
                condition=models.Q(status__in=("PENDING", "PROCESSING")),
                name="unique_live_scheduled_job_per_object",
            ),
        ]

    def __str__(self):
        return f"{self.kind}:{self.object_id} at {self.scheduled_for:%Y-%m-%d %H:%M} ({self.status})"

    @property
    def is_live(self) -> bool:
        return self.status in self.LIVE_STATUSES

    def is_due(self, now=None) -> bool:
        return self.status == self.Status.PENDING and self.scheduled_for <= (now or timezone.now())
