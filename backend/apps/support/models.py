"""
A customer asking for help, and the context that makes the answer possible.

Deliberately **not a ticketing system.** There is no threading, no SLA clock,
no assignment queue, no canned replies and no customer-facing status workflow,
because building those badly is worse than not building them and every
deployment that needs them already has Zendesk or Freshdesk. What was missing
was smaller and more urgent: a customer inside the product hitting a problem
had nowhere to say so, and a support engineer answering an email had no way to
see what state that workspace was actually in.

So this is two things and stops:

* a **request** -- category, message, a reference the customer can quote;
* a **snapshot** -- what was true about their workspace when they wrote it.

The snapshot is the part that earns its keep. Support questions arrive hours
after the problem, by which time the device has reconnected, the campaign has
finished and the credit balance has moved. Capturing the state at the moment of
asking turns "my campaign did not work this morning" into a row that says the
device was DISCONNECTED and the balance was 0. It is written once, never
updated, and holds **no message content, no phone numbers and no contact
names** -- counts, statuses and identifiers only. A support table that quietly
accumulates customer message bodies is a data-protection problem wearing a
helpdesk badge.

`reference` is what the customer is shown and what they quote back. It is
random rather than sequential: a sequential reference tells every customer how
many support requests the product receives, and lets them guess somebody
else's -- and the lookup is tenant-scoped anyway, so guessing gains nothing but
there is no reason to invite it.
"""

import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


def generate_reference() -> str:
    """
    A short, unambiguous, unguessable reference.

    Crockford-ish alphabet: no I, L, O or U, so a reference read down a phone
    line or copied off a screenshot cannot come back as a different one. 8
    characters from a 32-symbol alphabet is 40 bits, which is far more than
    enough for a table this size and short enough to say out loud.
    """
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "".join(secrets.choice(alphabet) for _ in range(8))


class SupportRequest(models.Model):
    """One question from one workspace, with the state it was asked in."""

    class Category(models.TextChoices):
        BILLING = "BILLING", "Billing and payments"
        TECHNICAL = "TECHNICAL", "Something is not working"
        CAMPAIGN = "CAMPAIGN", "A campaign or message"
        ACCOUNT = "ACCOUNT", "My account or team"
        OTHER = "OTHER", "Something else"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        # One state between open and closed, and no more. A finer workflow
        # would be a promise about response handling this product does not
        # make and cannot keep.
        IN_PROGRESS = "IN_PROGRESS", "Being looked at"
        RESOLVED = "RESOLVED", "Resolved"
        CLOSED = "CLOSED", "Closed"

    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        NORMAL = "NORMAL", "Normal"
        HIGH = "HIGH", "High"
        URGENT = "URGENT", "Urgent"

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="support_requests",
    )
    # SET_NULL rather than CASCADE: the question and its answer outlive the
    # employee who asked it, and deleting a user should not erase the record of
    # a billing dispute their workspace raised.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    reference = models.CharField(max_length=12, unique=True, default=generate_reference, editable=False)
    category = models.CharField(max_length=16, choices=Category.choices, default=Category.OTHER)
    subject = models.CharField(max_length=160)
    message = models.TextField()

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    #: Set by whoever triages, never by the customer. A priority field a
    #: customer can set is a field where everything is urgent.
    priority = models.CharField(max_length=8, choices=Priority.choices, default=Priority.NORMAL)

    #: The request id of the browser call that raised this, so a support
    #: engineer can find the log lines and the queue jobs it turned into. See
    #: core.middleware for where the id comes from.
    correlation_id = models.CharField(max_length=64, blank=True)

    #: What the customer was looking at. A campaign id is by far the most
    #: common answer to "which one?", and asking for it here saves a round trip.
    campaign_id = models.PositiveIntegerField(null=True, blank=True)

    #: The workspace's state at the moment of asking. Counts and statuses only
    #: -- see the module docstring for what must never appear in it. Written
    #: once by services.capture_context and never updated, because a snapshot
    #: that moves is not a snapshot.
    context = models.JSONField(default=dict, blank=True)

    #: Notes by whoever handled it. Never shown to the customer -- there is no
    #: endpoint that returns it to one -- so it can say "third time this week,
    #: check their device" without that being a message to them.
    internal_note = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # "What is open right now", the only question the triage screen asks.
            models.Index(fields=["status", "-created_at"], name="support_open_idx"),
            models.Index(fields=["tenant", "-created_at"], name="support_tenant_idx"),
            models.Index(fields=["category", "status"], name="support_category_idx"),
        ]

    def __str__(self):
        return f"{self.reference} ({self.category})"

    @property
    def is_open(self) -> bool:
        return self.status in (self.Status.OPEN, self.Status.IN_PROGRESS)

    def resolve(self, *, note: str = "", now=None) -> None:
        self.status = self.Status.RESOLVED
        self.resolved_at = now or timezone.now()
        if note:
            self.internal_note = f"{self.internal_note}\n{note}".strip()
        self.save(update_fields=["status", "resolved_at", "internal_note", "updated_at"])
