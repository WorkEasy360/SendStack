from django.db import models
from django.utils import timezone


class Plan(models.Model):
    """
    What a reseller sells, and the ceilings that come with it.

    Owned by whoever defined it: a reseller's plans are theirs to price and
    theirs alone to offer, and `owner=None` marks the platform's own. Nothing
    here talks to a payment processor -- `price_cents` is what the reseller
    bills their customer by their own arrangement, recorded so the app can
    show it rather than collect it.

    Every limit uses **0 for unlimited**, which is the same convention the
    team capacity and reply caps already use elsewhere in the product.
    """

    class Interval(models.TextChoices):
        MONTHLY = "MONTHLY", "Monthly"
        YEARLY = "YEARLY", "Yearly"

    owner = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="plans", null=True, blank=True,
    )
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)

    price_cents = models.PositiveIntegerField(default=0)
    currency = models.CharField(max_length=3, default="USD")
    interval = models.CharField(max_length=10, choices=Interval.choices, default=Interval.MONTHLY)

    max_messages_per_month = models.PositiveIntegerField(default=0)
    max_contacts = models.PositiveIntegerField(default=0)
    max_devices = models.PositiveIntegerField(default=0)
    max_users = models.PositiveIntegerField(default=0)

    # Which parts of the product this plan opens up. A dict rather than a
    # column each so a reseller can be given a new switch without a migration;
    # `FEATURES` is the list the UI renders and the only one that is read.
    features = models.JSONField(default=dict, blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    FEATURES = ("ai_agent", "workflows", "api_access", "click_tracking")

    class Meta:
        ordering = ["price_cents", "name"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="unique_plan_name_per_owner"),
        ]

    def __str__(self):
        return self.name

    def allows(self, feature: str) -> bool:
        """Unlisted features are on: a plan written before a switch existed should not lose it."""
        return bool(self.features.get(feature, True))

    @property
    def limits(self) -> dict:
        return {
            "messages_per_month": self.max_messages_per_month,
            "contacts": self.max_contacts,
            "devices": self.max_devices,
            "users": self.max_users,
        }


class Subscription(models.Model):
    """
    One tenant's place on one plan.

    The period is stored rather than derived from `started_at` so that a
    month's allowance is anchored to a real date somebody can point at, and so
    rolling it forward is an explicit act. `current_period_start` is what the
    message counter measures from -- see `apps.subscriptions.services.usage`.
    """

    class Status(models.TextChoices):
        TRIALING = "TRIALING", "Trialing"
        ACTIVE = "ACTIVE", "Active"
        PAST_DUE = "PAST_DUE", "Past due"
        CANCELLED = "CANCELLED", "Cancelled"

    tenant = models.OneToOneField("tenants.Tenant", on_delete=models.CASCADE, related_name="subscription")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)

    current_period_start = models.DateTimeField(default=timezone.now)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["status"])]

    def __str__(self):
        return f"Tenant {self.tenant_id} on {self.plan_id}"

    @property
    def is_live(self) -> bool:
        """Whether this subscription should still be letting work through."""
        return self.status in (self.Status.ACTIVE, self.Status.TRIALING)
