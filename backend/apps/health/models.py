"""
Operational records: what the backup job did, and which background jobs died.

Two tables, one subject -- the things that happen outside a request, where
nobody is watching unless the application writes them down.

What the backup job did, recorded where the application can see it.

The backup runs *outside* the container, on the host, on a schedule this
process has no visibility of. Before this, that meant the honest answer to
"are backups working?" was BACKUP_VERIFIED_AT -- a date an operator typed in
by hand, which reports what somebody remembered rather than what happened.

A row per run closes that. The script reports in at the end of every run,
success or failure, and the application can then answer three questions it
could not answer before:

* when did a backup last **succeed**,
* how old is it,
* and when was one last actually **restored** somewhere.

The third is the one that matters. A backup nobody has restored is a file, and
this table is the difference between a deployment that believes it has backups
and one that knows.

Nothing sensitive lives here: a filename, a size, a destination label, and a
status. Not the archive, not the passphrase, not a credential, and not the
remote's full URL with any token in it.
"""

from django.db import models
from django.utils import timezone


class BackupRun(models.Model):
    """One execution of the backup job, or one restore test."""

    class Kind(models.TextChoices):
        BACKUP = "BACKUP", "Backup"
        # A backup restored into a throwaway database and checked. Recorded
        # in the same table because "when did we last back up" and "when did
        # we last prove we could restore" are the same operator's question
        # asked one step apart.
        RESTORE_TEST = "RESTORE_TEST", "Restore verification"
        # The **off-host copy** fetched back from the remote, checked byte for
        # byte, and restored. A stronger claim than RESTORE_TEST and a separate
        # row because it is a separate claim: a local restore proves the dump
        # is good, and says nothing about whether the object in the bucket is
        # readable, complete, or reachable with the credentials that would be
        # available on a rebuilt host. Written by
        # scripts/verify_offhost_backup.sh.
        OFFSITE_RESTORE_TEST = "OFFSITE_RESTORE_TEST", "Off-host restore verification"

    class Status(models.TextChoices):
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"

    kind = models.CharField(max_length=24, choices=Kind.choices, default=Kind.BACKUP)
    status = models.CharField(max_length=12, choices=Status.choices)

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(default=timezone.now)

    # The archive's basename only -- never the full path. A path leaks the
    # host's directory layout into a support screenshot for no benefit.
    artifact = models.CharField(max_length=255, blank=True)
    size_bytes = models.BigIntegerField(default=0)
    encrypted = models.BooleanField(default=False)

    # Where it went, as a **label** rather than a URL: "s3", "rclone",
    # "local". A destination string can carry an access key in its query
    # string, and this table is read by an endpoint.
    destination = models.CharField(max_length=64, blank=True)
    offsite = models.BooleanField(
        default=False, help_text="Whether this archive was copied off the host.",
    )

    detail = models.TextField(blank=True, help_text="Error text on failure; a short note otherwise.")

    class Meta:
        ordering = ["-finished_at"]
        indexes = [
            # "The last successful backup" -- the only query the monitoring
            # endpoint and the production check ever run.
            models.Index(fields=["kind", "status", "-finished_at"], name="backup_kind_status_idx"),
        ]

    def __str__(self):
        return f"{self.kind} {self.status} at {self.finished_at:%Y-%m-%d %H:%M}"

    @property
    def age_seconds(self) -> float:
        return (timezone.now() - self.finished_at).total_seconds()

    @classmethod
    def last_success(cls, kind=Kind.BACKUP):
        return cls.objects.filter(kind=kind, status=cls.Status.SUCCEEDED).first()

    @classmethod
    def last_failure(cls, kind=Kind.BACKUP):
        return cls.objects.filter(kind=kind, status=cls.Status.FAILED).first()


class DeadLetter(models.Model):
    """
    A background job that failed for the last time, kept where somebody will see it.

    Celery's answer to a task that has spent its retries is to log a traceback
    and move on. In a container that means the record of a customer's import
    dying is a few lines in a log stream that rotates, and the first anyone
    hears of it is the customer asking where their contacts went. There was no
    query in this system that could answer "what has been failing today".

    So the terminal failure is written down instead. One row per task that gave
    up: which task, what it was working on, the exception, how many attempts it
    had, and the correlation id -- so a support engineer with a request id from
    a browser can find the job it turned into three hops later.

    **Not a retry queue.** Nothing re-runs these. Automatic retry is the
    handler's own business and it has already happened; this is the record that
    it ran out, and re-running it is a decision a person makes with the
    knowledge of why it failed. `resolved_at` is that person marking it dealt
    with, and nothing else reads it.

    The traceback is trimmed and the arguments are stored as a short repr
    rather than a payload: a message-send task's arguments are ids, but a task
    added later might carry a phone number or a body, and an operations table
    that quietly accumulates message content is a data-protection problem
    wearing a monitoring hat.
    """

    #: Longest exception text kept. Enough for the exception and the last few
    #: frames, which is what identifies the failure; the full traceback is in
    #: the logs and in the error monitor, both of which are already scrubbed.
    MAX_DETAIL = 4000

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="dead_letters",
        null=True, blank=True,
        help_text="The workspace whose work this was, where the task knows. Blank for platform-wide jobs.",
    )

    task_name = models.CharField(max_length=255)
    task_id = models.CharField(max_length=255, blank=True)
    queue = models.CharField(max_length=64, blank=True)

    #: A short, safe rendering of what the task was called with. Never the raw
    #: payload -- see the class docstring.
    arguments = models.CharField(max_length=500, blank=True)

    exception_type = models.CharField(max_length=255, blank=True)
    detail = models.TextField(blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    #: The id that ties this back to the HTTP request that started it, however
    #: many queue hops ago. See config.celery.
    request_id = models.CharField(max_length=64, blank=True)

    failed_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-failed_at"]
        indexes = [
            # "What is unresolved right now", which is the only question the
            # operations dashboard asks.
            models.Index(fields=["resolved_at", "-failed_at"], name="deadletter_open_idx"),
            models.Index(fields=["task_name", "-failed_at"], name="deadletter_task_idx"),
            models.Index(fields=["tenant", "-failed_at"], name="deadletter_tenant_idx"),
        ]

    def __str__(self):
        return f"{self.task_name} failed at {self.failed_at:%Y-%m-%d %H:%M}"

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None
