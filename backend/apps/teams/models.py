from django.conf import settings
from django.db import models


class TeamMember(models.Model):
    """
    An agent's working profile, kept separate from their login.

    `accounts.User` answers "who is this and what may they administer". This
    answers "are they on shift, how much can they take on, and should the
    queue hand them work" -- questions that change several times a day and
    have nothing to do with permissions. Merging the two would mean a manager
    toggling their own availability touched the same row that grants access.
    """

    class Role(models.TextChoices):
        MANAGER = "MANAGER", "Manager"
        AGENT = "AGENT", "Agent"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="team_members")
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="team_member")
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.AGENT)

    is_available = models.BooleanField(
        default=True, help_text="Off means the queue stops routing new conversations here.",
    )
    auto_assign = models.BooleanField(
        default=True, help_text="Off keeps this person out of automatic assignment without marking them away.",
    )
    # 0 means no ceiling. A cap is what stops round-robin handing a fourth
    # urgent chat to someone already juggling three.
    max_open_conversations = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user__username"]
        indexes = [models.Index(fields=["tenant", "is_available"])]

    def __str__(self):
        return f"{self.user_id} ({self.role})"

    def accepts_work(self) -> bool:
        return self.is_available and self.auto_assign


class AssignmentPolicy(models.Model):
    """
    How incoming conversations reach a person, and when they are late.

    One row per tenant, and -- like CompliancePolicy -- `for_tenant` returns an
    unsaved default rather than writing one, so reading this on the inbound
    path never turns a read into a write.
    """

    class Mode(models.TextChoices):
        OFF = "OFF", "Leave unassigned"
        ROUND_ROBIN = "ROUND_ROBIN", "Round robin"
        LEAST_BUSY = "LEAST_BUSY", "Least busy"

    tenant = models.OneToOneField("tenants.Tenant", on_delete=models.CASCADE, related_name="assignment_policy")
    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.OFF)

    # Reassigning on every inbound message would take a conversation away from
    # the agent already handling it, so the default only assigns what nobody
    # owns yet.
    reassign_when_unassigned = models.BooleanField(
        default=True, help_text="Also assign an existing conversation that has no owner when the contact writes again.",
    )
    respect_availability = models.BooleanField(default=True)

    # 0 disables the target. Measured from the contact's message to the first
    # reply typed by a person: an auto-reply is not somebody answering.
    first_response_target_minutes = models.PositiveIntegerField(default=15)
    resolution_target_minutes = models.PositiveIntegerField(default=0)

    working_hours_enabled = models.BooleanField(default=False)
    send_away_message = models.BooleanField(default=False)
    away_message = models.TextField(
        blank=True,
        default="Thanks for your message! Our team is offline right now and will reply when we're back.",
    )

    # Round robin's cursor. Stored rather than derived so the rotation survives
    # a restart and does not begin again at the first agent every morning.
    last_assigned_member = models.ForeignKey(
        TeamMember, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Assignment policy for tenant {self.tenant_id}"

    @classmethod
    def for_tenant(cls, tenant_id):
        """The tenant's policy, or an unsaved default with the same shape."""
        policy = cls.objects.filter(tenant_id=tenant_id).first()
        return policy or cls(tenant_id=tenant_id)


class WorkingHours(models.Model):
    """One row per weekday per tenant. A missing row means closed all day."""

    class Weekday(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="working_hours")
    weekday = models.IntegerField(choices=Weekday.choices)
    is_open = models.BooleanField(default=True)
    opens_at = models.TimeField(default="09:00")
    closes_at = models.TimeField(default="18:00")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "weekday"], name="unique_working_hours_per_weekday")]
        ordering = ["weekday"]

    def __str__(self):
        return f"{self.get_weekday_display()} {self.opens_at}-{self.closes_at}"


class CannedReply(models.Model):
    """
    A saved answer an agent inserts by typing `/shortcut`.

    The shortcut is what makes this worth having over copy-paste, so it is
    unique per tenant and matched exactly -- two replies answering to `/hours`
    would make the expansion a coin toss.
    """

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="canned_replies")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    shortcut = models.CharField(max_length=40, help_text="Typed as /shortcut in the inbox composer.")
    title = models.CharField(max_length=150)
    body = models.TextField()
    usage_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "shortcut"], name="unique_canned_reply_shortcut")]
        ordering = ["shortcut"]

    def __str__(self):
        return f"/{self.shortcut}"
