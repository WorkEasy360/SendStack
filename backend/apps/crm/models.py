from django.conf import settings
from django.db import models
from django.utils import timezone


class Pipeline(models.Model):
    """
    One sales process. Most tenants need exactly one; a business selling two
    very different things (retail orders and wholesale contracts) needs two,
    because the stages a wholesale contract passes through say nothing useful
    about a retail order.

    Created on demand rather than up front -- see `services.default_pipeline`,
    which builds the standard stage list the first time a tenant opens a deal.
    """

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="pipelines")
    name = models.CharField(max_length=100)
    # The pipeline a new deal lands in when the caller doesn't name one. Kept
    # as a flag rather than "the lowest id" so a tenant can retire their first
    # pipeline without every later deal silently going back to it.
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="unique_pipeline_per_tenant")]
        ordering = ["name"]

    def __str__(self):
        return self.name


class PipelineStage(models.Model):
    """
    A column on the board.

    `kind` is the important field: "won" is a property of the stage, not of a
    name that happens to read like it. A tenant who renames WON to "Paid" or
    adds a second losing stage ("Went with a competitor") keeps working,
    because every report asks `kind`, never `name`.
    """

    class Kind(models.TextChoices):
        OPEN = "OPEN", "In progress"
        WON = "WON", "Won"
        LOST = "LOST", "Lost"

    pipeline = models.ForeignKey(Pipeline, on_delete=models.CASCADE, related_name="stages")
    name = models.CharField(max_length=60)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.OPEN)
    order = models.PositiveSmallIntegerField(default=0)
    probability = models.PositiveSmallIntegerField(
        default=0, help_text="Rough chance of winning at this stage, 0-100. Used for the weighted forecast."
    )
    color = models.CharField(max_length=7, default="#2563eb")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["pipeline", "name"], name="unique_stage_per_pipeline")]
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.pipeline_id}: {self.name}"


class Deal(models.Model):
    """
    One sale being worked, hanging off the contact who is buying.

    Deliberately thin. A WhatsApp-first seller wants to know what is in play,
    what it is worth and whose turn it is -- not line items, quotes and
    invoices, which are the accounting system's job.
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        WON = "WON", "Won"
        LOST = "LOST", "Lost"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="deals")
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="deals")
    pipeline = models.ForeignKey(Pipeline, on_delete=models.PROTECT, related_name="deals")
    stage = models.ForeignKey(PipelineStage, on_delete=models.PROTECT, related_name="deals")

    name = models.CharField(max_length=150)
    value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="INR")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="owned_deals"
    )
    expected_close_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    # Mirrors `stage.kind`, written by save(). It exists so "how much is still
    # open" is an indexed scan of this table rather than a join to stages on
    # every dashboard load; it is never set independently of the stage.
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "stage"]),
            models.Index(fields=["contact", "status"]),
            # "What is expected to close this week", asked by the Today digest.
            models.Index(fields=["tenant", "status", "expected_close_date"]),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # status and closed_at are derivations of the stage, so they are
        # computed here rather than trusted from the caller: a deal moved to
        # "Won" through any code path must be won everywhere, including in the
        # report that never looked at the stage table.
        if self.stage_id:
            kind = self.stage.kind
            self.status = kind
            if kind in (PipelineStage.Kind.WON, PipelineStage.Kind.LOST):
                self.closed_at = self.closed_at or timezone.now()
            else:
                self.closed_at = None
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"status", "closed_at"}
        super().save(*args, **kwargs)

    @property
    def weighted_value(self):
        return (self.value or 0) * (self.stage.probability or 0) / 100


class Task(models.Model):
    """
    Something a person has to do, with a time it has to be done by.

    The point of the whole CRM for a small team: the dashboard's "what should
    I do today" is a query over this table plus a handful of derived
    follow-ups, and nothing else on the platform can answer it.
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        DONE = "DONE", "Done"
        CANCELLED = "CANCELLED", "Cancelled"

    class Kind(models.TextChoices):
        FOLLOW_UP = "FOLLOW_UP", "Follow up"
        CALL = "CALL", "Call"
        MEETING = "MEETING", "Meeting"
        OTHER = "OTHER", "Other"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="crm_tasks")
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.CASCADE, null=True, blank=True, related_name="tasks"
    )
    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, null=True, blank=True, related_name="tasks")

    title = models.CharField(max_length=200)
    notes = models.TextField(blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.FOLLOW_UP)
    due_at = models.DateTimeField()
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="crm_tasks"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["due_at", "id"]
        indexes = [
            # The Today digest: open work for this tenant, soonest first.
            models.Index(fields=["tenant", "status", "due_at"]),
            models.Index(fields=["assigned_to", "status", "due_at"]),
            models.Index(fields=["contact", "status"]),
        ]

    def __str__(self):
        return self.title

    @property
    def is_overdue(self) -> bool:
        return self.status == self.Status.OPEN and self.due_at < timezone.now()


class ContactNote(models.Model):
    """
    A note about the person, not about one conversation.

    inbox.ConversationNote already exists and stays: it is a note on a thread,
    and it belongs with that thread. This one outlives every thread -- "pays
    late, always call before invoicing" is still true next year, on a
    conversation that has not been opened yet.
    """

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="contact_notes")
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="notes")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["contact", "-created_at"])]

    def __str__(self):
        return f"Note on contact {self.contact_id}"


class Activity(models.Model):
    """
    The contact timeline's CRM half: append-only, one row per thing that
    happened *to the record* rather than in the chat.

    Messages are deliberately **not** copied in here. There are already
    millions of them, they are already indexed by contact, and duplicating
    each one into a second table would double the write cost of the busiest
    path on the platform to serve a screen that is read occasionally. The
    profile timeline merges the two sources at read time instead -- see
    `services.contact_timeline`.
    """

    class Kind(models.TextChoices):
        CONTACT_CREATED = "CONTACT_CREATED", "Contact created"
        STATUS_CHANGED = "STATUS_CHANGED", "Status changed"
        OWNER_CHANGED = "OWNER_CHANGED", "Owner changed"
        TAG_ADDED = "TAG_ADDED", "Tag added"
        TAG_REMOVED = "TAG_REMOVED", "Tag removed"
        NOTE_ADDED = "NOTE_ADDED", "Note added"
        TASK_CREATED = "TASK_CREATED", "Task created"
        TASK_COMPLETED = "TASK_COMPLETED", "Task completed"
        DEAL_CREATED = "DEAL_CREATED", "Deal created"
        DEAL_STAGE_CHANGED = "DEAL_STAGE_CHANGED", "Deal moved"
        DEAL_WON = "DEAL_WON", "Deal won"
        DEAL_LOST = "DEAL_LOST", "Deal lost"
        CONSENT_CHANGED = "CONSENT_CHANGED", "Consent changed"
        AUTOMATION = "AUTOMATION", "Automation"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="crm_activities")
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="activities")
    kind = models.CharField(max_length=30, choices=Kind.choices)
    summary = models.CharField(max_length=255)
    # Whatever the entry needs to render itself later (old/new value, deal id,
    # task title). Free-form on purpose: a timeline entry is a historical
    # record, and a column added next year must not change what last year's
    # rows mean.
    metadata = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["contact", "-occurred_at"]),
            models.Index(fields=["tenant", "-occurred_at"]),
        ]

    def __str__(self):
        return f"{self.kind} on contact {self.contact_id}"
