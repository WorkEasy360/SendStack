"""
How far a new customer has got, and whether they ever actually used the product.

Two records, and the difference between them is the difference between a
checklist and a metric.

`OnboardingState` is **one timestamp per milestone**, not a JSON blob. The
milestones are a fixed, small, known list that the product's own screens are
built around, and each one is queried on its own ("how many workspaces
connected WhatsApp but never sent?"). A blob would make every one of those
questions a table scan and every schema change invisible to a migration.

`FunnelEvent` is the append-only trail of those moments happening. The state
row answers "where is this customer now"; the event log answers "how did the
last hundred customers get there, and where did they stop". Neither can be
derived from the other, which is why both exist.

**Nothing here is written by the browser.** Every milestone is stamped by
`services.refresh`, which reads the underlying tables -- is there a connected
device, is there a contact, is there a message that actually sent -- and marks
what it finds true. A client that POSTs "I finished step 4" changes nothing.
That is not defensive programming for its own sake: the checklist drives the
activation metric, and a metric a customer can tick off by hand measures
nothing.
"""

from django.db import models
from django.utils import timezone

#: Funnel events a workspace can legitimately produce more than once. Everything
#: else is once-per-workspace and the database says so -- see FunnelEvent.Meta.
#: Module level rather than a class attribute because a nested ``class Meta``
#: body cannot see names from the class body around it.
REPEATABLE_FUNNEL_EVENTS = ("signup_started", "upgrade_started", "payment_completed")


class OnboardingState(models.Model):
    """
    One workspace's progress through first-run setup.

    Every milestone field is a nullable timestamp: NULL means not yet, a value
    means when. Booleans would have lost the date, and the date is what the
    funnel is made of.
    """

    tenant = models.OneToOneField("tenants.Tenant", on_delete=models.CASCADE, related_name="onboarding")

    # Whether this workspace should be *shown* the guided flow.
    #
    # False for every tenant that existed before onboarding did, which is the
    # whole point: a customer who has been sending campaigns for a year must
    # not be dropped into a "connect your WhatsApp" wizard because a release
    # shipped. They can still open the checklist and see it all ticked; they
    # are simply never sent there. Self-service signup sets this True.
    is_guided = models.BooleanField(
        default=False,
        help_text="Show this workspace the guided first-run flow. Off for workspaces created before onboarding.",
    )
    # The customer said "I know, hide it". Their choice, and it sticks; it
    # does not un-tick anything or stop milestones being recorded.
    dismissed_at = models.DateTimeField(null=True, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    legal_accepted_at = models.DateTimeField(null=True, blank=True)
    whatsapp_connected_at = models.DateTimeField(null=True, blank=True)
    contacts_added_at = models.DateTimeField(null=True, blank=True)
    consent_confirmed_at = models.DateTimeField(null=True, blank=True)
    test_message_sent_at = models.DateTimeField(null=True, blank=True)
    first_campaign_created_at = models.DateTimeField(null=True, blank=True)
    first_campaign_sent_at = models.DateTimeField(null=True, blank=True)

    # **The activation metric.** Not "signed up" -- signing up is a form being
    # submitted, and counting it as activation is how a product convinces
    # itself it is working while nobody uses it. This is set once, when the
    # workspace has a connected number, a contact it is allowed to message,
    # and a message that actually reached the provider. See
    # services.ACTIVATION_REQUIREMENTS.
    activated_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    #: Milestone field -> the label the checklist shows. Order is the order
    #: the customer meets them in, and the order the UI renders.
    STEPS = (
        ("started_at", "Create your account"),
        ("legal_accepted_at", "Accept the terms"),
        ("whatsapp_connected_at", "Connect WhatsApp"),
        ("contacts_added_at", "Add contacts"),
        ("consent_confirmed_at", "Confirm consent"),
        ("test_message_sent_at", "Send a test message"),
        ("first_campaign_created_at", "Create your first campaign"),
        ("first_campaign_sent_at", "Send your first campaign"),
    )

    class Meta:
        indexes = [
            # "Which workspaces activated, and when" -- the only funnel query
            # that runs over the whole table.
            models.Index(fields=["activated_at"], name="onboarding_activated_idx"),
        ]

    def __str__(self):
        return f"Onboarding for tenant {self.tenant_id}"

    @property
    def is_activated(self) -> bool:
        return self.activated_at is not None

    def next_step(self):
        """The first milestone not yet reached, or None when they are all done."""
        for field, label in self.STEPS:
            if getattr(self, field) is None:
                return {"key": field.removesuffix("_at"), "label": label}
        return None

    def as_steps(self) -> list:
        return [
            {
                "key": field.removesuffix("_at"),
                "label": label,
                "done": getattr(self, field) is not None,
                "completed_at": getattr(self, field),
            }
            for field, label in self.STEPS
        ]


class FunnelEvent(models.Model):
    """
    One thing that happened on the way from visitor to paying customer.

    Deliberately tiny, and deliberately **not** a general analytics pipeline.
    A name, a time, the workspace it belongs to, and a handful of numbers.

    What is *not* here is the point: no phone number, no message text, no
    email address, no token. A funnel needs to know that a first message was
    sent, never what it said or who to. Anything that would make this table
    worth stealing does not go in it.

    `tenant` is nullable for exactly one event -- ``signup_started`` -- which
    by definition happens before there is a workspace to attribute it to.
    """

    class Name(models.TextChoices):
        SIGNUP_STARTED = "signup_started", "Signup started"
        SIGNUP_COMPLETED = "signup_completed", "Signup completed"
        TRIAL_STARTED = "trial_started", "Trial started"
        ONBOARDING_STARTED = "onboarding_started", "Onboarding started"
        LEGAL_ACCEPTED = "legal_accepted", "Legal terms accepted"
        WHATSAPP_CONNECTED = "whatsapp_connected", "WhatsApp connected"
        CONTACT_ADDED = "contact_added", "First contact added"
        CONSENT_CONFIRMED = "consent_confirmed", "Consent confirmed"
        TEST_MESSAGE_SENT = "test_message_sent", "First message sent"
        FIRST_CAMPAIGN_CREATED = "first_campaign_created", "First campaign created"
        FIRST_CAMPAIGN_SENT = "first_campaign_sent", "First campaign sent"
        ACTIVATED = "activated", "Workspace activated"
        UPGRADE_STARTED = "upgrade_started", "Upgrade started"
        PAYMENT_COMPLETED = "payment_completed", "Payment completed"
        TRIAL_EXPIRED = "trial_expired", "Trial expired"

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="funnel_events", null=True, blank=True,
    )
    name = models.CharField(max_length=32, choices=Name.choices)
    occurred_at = models.DateTimeField(default=timezone.now)
    # Counts and plan names only. See the class docstring for what must never
    # appear here.
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["name", "occurred_at"], name="funnel_name_time_idx"),
            models.Index(fields=["tenant", "name"], name="funnel_tenant_name_idx"),
        ]
        constraints = [
            # Most of these events are milestones -- the *first* contact, the
            # *first* campaign -- and a milestone recorded twice is a funnel
            # that reports more first campaigns than it has customers. The
            # database enforces it rather than the service remembering to
            # check, so two concurrent requests cannot both be the first.
            #
            # Stated as "everything except the repeatable few", because that
            # list is short and stays short, while the milestone list grows.
            models.UniqueConstraint(
                fields=["tenant", "name"],
                condition=~models.Q(name__in=REPEATABLE_FUNNEL_EVENTS),
                name="unique_once_per_tenant_funnel_event",
            ),
        ]

    def __str__(self):
        return f"{self.name} for tenant {self.tenant_id}"
