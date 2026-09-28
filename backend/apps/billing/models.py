"""
The record of money, kept on this side of the payment provider.

Three tables, and the split between them is the security design.

`Checkout` is **what the server decided to charge**, written before the
customer is sent anywhere. The plan, the amount, the currency and the tenant
are all resolved server-side and frozen into this row; when the payment comes
back, the row -- not the callback -- says what should have been paid. That is
the answer to "the browser says it paid ₹499 for the ₹999 plan".

`Payment` is what the provider says actually happened, tied to the checkout it
settles. One live payment per checkout, enforced by the database, so a webhook
delivered five times cannot activate five subscriptions or extend a period
five times.

`WebhookEvent` is every callback this system has ever accepted, keyed on the
provider's own event id. It is both the idempotency ledger and the audit
trail: the unique constraint on `(provider, event_id)` is what makes "process
each event exactly once" a property of the schema rather than a hope.

**What is deliberately not here**: card numbers, CVVs, bank credentials,
UPI handles, anything that could be used to take money. This application never
sees them -- the customer types them on the provider's hosted page -- and the
only identifiers stored are the ones needed to ask the provider about a
payment later.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class Checkout(models.Model):
    """
    An intention to buy, priced by the server.

    Created when a customer presses Upgrade, before anything is sent to a
    provider. Everything a payment could lie about is written here first.
    """

    class Status(models.TextChoices):
        CREATED = "CREATED", "Created"
        PENDING = "PENDING", "Awaiting payment"
        PAID = "PAID", "Paid"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="checkouts")
    # Who pressed the button. SET_NULL because a checkout outlives an employee
    # leaving, and the workspace is what was actually billed.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    plan = models.ForeignKey("subscriptions.Plan", on_delete=models.PROTECT, related_name="checkouts")

    provider = models.CharField(max_length=32)
    # The provider's id for this order. Blank until the provider has been
    # asked, which is why it is not the primary key of anything.
    provider_order_id = models.CharField(max_length=128, blank=True)

    # **The amount the server decided.** In the currency's minor unit (paise
    # for INR, cents for USD), because storing money as a float is how a
    # rounding error becomes a refund request. Copied from the plan at
    # creation and never re-read from it: see the note on price changes in
    # services.verify_payment.
    amount_minor = models.PositiveIntegerField()
    currency = models.CharField(max_length=3)

    #: What the plan costs before any promotion, frozen at the same moment as
    #: `amount_minor`. Two columns rather than one plus a lookup, because a
    #: receipt has to say "₹999, less ₹200" a year later -- when the coupon may
    #: have been deleted and the plan reprinted. Zero on rows written before
    #: coupons existed; read it through `list_amount` rather than directly.
    list_amount_minor = models.PositiveIntegerField(default=0)
    discount_minor = models.PositiveIntegerField(default=0)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.CREATED)
    # Anything the provider needs handed back to it, and nothing secret. The
    # provider's *public* key id may appear here because the browser needs it
    # to open the checkout; a secret never may.
    metadata = models.JSONField(default=dict, blank=True)

    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status"], name="checkout_tenant_status_idx"),
            # The lookup a webhook does: it knows the provider's order id and
            # nothing else about us.
            models.Index(fields=["provider", "provider_order_id"], name="checkout_provider_order_idx"),
        ]
        constraints = [
            # A provider order id identifies one order. Two checkouts claiming
            # the same one would make the webhook's tenant mapping ambiguous,
            # which is the mapping that decides whose subscription gets
            # activated.
            models.UniqueConstraint(
                fields=["provider", "provider_order_id"],
                condition=~models.Q(provider_order_id=""),
                name="unique_provider_order_id",
            ),
        ]

    def __str__(self):
        return f"Checkout {self.pk} for tenant {self.tenant_id} ({self.status})"

    @property
    def is_open(self) -> bool:
        return self.status in (self.Status.CREATED, self.Status.PENDING)

    @property
    def list_amount(self) -> int:
        """
        The undiscounted price, safe to read on any row.

        A checkout written before promotions existed has no list amount
        recorded, and its charged amount *was* the list price -- so falling
        back to `amount_minor` is not a guess, it is what happened.
        """
        return self.list_amount_minor or self.amount_minor

    def is_expired(self, now=None) -> bool:
        return self.expires_at is not None and (now or timezone.now()) >= self.expires_at


class Payment(models.Model):
    """
    What the provider confirmed, and what this system did about it.

    One **captured** payment per checkout, enforced below. The constraint is
    the whole of the double-activation protection: a webhook redelivered, a
    browser callback racing that webhook, and an operator running
    reconciliation by hand all try to write this row, and exactly one of them
    wins.
    """

    class Status(models.TextChoices):
        CAPTURED = "CAPTURED", "Captured"
        FAILED = "FAILED", "Failed"
        REFUNDED = "REFUNDED", "Refunded"

    checkout = models.ForeignKey(Checkout, on_delete=models.PROTECT, related_name="payments")
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="payments")

    provider = models.CharField(max_length=32)
    provider_payment_id = models.CharField(max_length=128)

    # What the provider says was paid, kept beside what we asked for so a
    # mismatch is visible in the row rather than only in a log line.
    amount_minor = models.PositiveIntegerField()
    currency = models.CharField(max_length=3)

    status = models.CharField(max_length=12, choices=Status.choices)
    # Free text from the provider for a failure. Shown to nobody but an
    # operator: a gateway's decline reason is often more specific about
    # somebody's bank than they would like the product to repeat.
    failure_reason = models.CharField(max_length=255, blank=True)

    captured_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"], name="payment_tenant_time_idx"),
        ]
        constraints = [
            # The provider's payment id is unique per provider: the same
            # payment arriving twice is the same row, not a second one.
            models.UniqueConstraint(
                fields=["provider", "provider_payment_id"], name="unique_provider_payment_id",
            ),
            # And at most one *captured* payment per checkout. A failed
            # attempt followed by a successful retry is two rows and is
            # correct; two captures on one order is a double charge.
            models.UniqueConstraint(
                fields=["checkout"],
                condition=models.Q(status="CAPTURED"),
                name="unique_captured_payment_per_checkout",
            ),
        ]

    def __str__(self):
        return f"{self.status} payment for checkout {self.checkout_id}"


class WebhookEvent(models.Model):
    """
    Every provider callback this system has taken delivery of.

    The unique constraint on `(provider, event_id)` is the idempotency
    mechanism, and it is a database constraint rather than a check in a view
    because webhook redelivery is *concurrent*: providers retry on a timeout,
    and the retry frequently arrives while the first delivery is still being
    processed. A "have we seen this?" query would say no to both.

    The raw payload is kept for a bounded window (see
    BILLING_WEBHOOK_PAYLOAD_RETENTION_DAYS) so a disputed activation can be
    reconstructed, then dropped -- it is a third party's data about a
    customer's payment and nothing needs it after that.
    """

    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        PROCESSED = "PROCESSED", "Processed"
        IGNORED = "IGNORED", "Ignored"
        FAILED = "FAILED", "Failed"

    provider = models.CharField(max_length=32)
    # The provider's own id for the event. Every provider worth integrating
    # sends one; the adapter is responsible for producing a stable value even
    # where the provider does not, so this is never blank.
    event_id = models.CharField(max_length=128)
    event_type = models.CharField(max_length=64)

    # Nullable: an event may arrive for an order this system has never heard
    # of (a different environment sharing a webhook URL, a test event). That
    # is IGNORED, not an error, and the row still records it.
    checkout = models.ForeignKey(
        Checkout, on_delete=models.SET_NULL, null=True, blank=True, related_name="webhook_events",
    )
    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.SET_NULL, null=True, blank=True, related_name="webhook_events",
    )

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RECEIVED)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    payload = models.JSONField(default=dict, blank=True)
    payload_purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["provider", "-received_at"], name="webhook_provider_time_idx"),
            models.Index(fields=["status"], name="webhook_status_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "event_id"], name="unique_provider_webhook_event",
            ),
        ]

    def __str__(self):
        return f"{self.provider} {self.event_type} ({self.status})"


class Coupon(models.Model):
    """
    A discount somebody may apply at checkout, or a trial somebody may extend.

    **The plan is never touched.** A promotion is this row plus a `Checkout`
    recording what it took off; `Plan.price_cents` stays the list price for
    everybody, and a promotion ending changes nothing about the plan, about
    anybody's existing subscription, or about a payment that has already
    happened. That is the whole reason a coupon is a separate object rather
    than a temporary edit to a price: an edited price is a promotion you cannot
    end, cannot restrict to one tier, and cannot report on afterwards.

    Owned the way a Plan is: `owner=None` is the platform's own, and a
    reseller's belongs to them. A code is unique **per owner**, so two
    resellers can both run WELCOME20 without knowing about each other -- and
    the resolver only ever looks in the set the workspace is actually offered,
    which is what stops one reseller's discount being redeemed against
    another's plan.

    Everything a customer could lie about -- the discount, which plan it
    applies to, whether it has expired, how many times it has been used -- is
    decided here and never read from a request.
    """

    class Kind(models.TextChoices):
        PERCENT = "PERCENT", "Percentage off"
        FIXED = "FIXED", "Fixed amount off"
        # Not a discount at all: it moves `trial_ends_at`. Kept in the same
        # table because it is the same object to whoever runs the promotion --
        # a code, a window, a usage limit -- and splitting it would duplicate
        # every one of those rules for one differing field.
        TRIAL_EXTENSION = "TRIAL_EXTENSION", "Extra trial days"

    owner = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="coupons", null=True, blank=True,
    )
    # Stored uppercase and matched uppercase. Somebody typing `welcome20`
    # means the same thing as `WELCOME20`, and a case-sensitive coupon code is
    # a support-ticket generator.
    code = models.CharField(max_length=32)
    description = models.CharField(max_length=200, blank=True)

    kind = models.CharField(max_length=16, choices=Kind.choices)
    #: Percent (1-100) for PERCENT, minor units for FIXED, days for
    #: TRIAL_EXTENSION. One column, because the three are never mixed and the
    #: kind says which one this is.
    value = models.PositiveIntegerField()
    #: Only meaningful for FIXED. A fixed discount in the wrong currency is not
    #: a discount, it is an arithmetic accident, so it is checked rather than
    #: converted -- this application holds no exchange rates and should not
    #: invent one at checkout.
    currency = models.CharField(max_length=3, blank=True)

    #: Which plans this applies to. Empty means every plan the workspace may
    #: buy, which is the common case -- hence a M2M that is usually unset
    #: rather than a required field somebody has to fill in for the easy case.
    plans = models.ManyToManyField(
        "subscriptions.Plan", blank=True, related_name="coupons",
        help_text="Restrict to these plans. Leave empty to allow any plan.",
    )

    starts_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    #: 0 means no limit, the convention every other limit in this codebase uses.
    max_redemptions = models.PositiveIntegerField(
        default=0, help_text="Total redemptions across all workspaces. 0 = unlimited.",
    )
    max_per_tenant = models.PositiveIntegerField(
        default=1, help_text="Redemptions allowed per workspace. 0 = unlimited.",
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "code"], name="unique_coupon_code_per_owner"),
            # A zero-value coupon of any kind is a code that silently does
            # nothing, which is the worst kind of promotion because it looks
            # like it worked. A percentage over 100 would price the plan below
            # free. Both refused by the database rather than by whoever
            # remembers to check.
            models.CheckConstraint(condition=models.Q(value__gt=0), name="coupon_value_positive"),
            models.CheckConstraint(
                condition=~models.Q(kind="PERCENT") | models.Q(value__lte=100),
                name="coupon_percent_at_most_100",
            ),
        ]
        indexes = [models.Index(fields=["owner", "code"], name="coupon_owner_code_idx")]

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        self.code = (self.code or "").strip().upper()
        self.currency = (self.currency or "").strip().upper()
        super().save(*args, **kwargs)

    def is_live(self, now=None) -> bool:
        """Whether the window is open. Says nothing about limits -- see services."""
        now = now or timezone.now()
        if not self.is_active:
            return False
        if self.starts_at is not None and now < self.starts_at:
            return False
        if self.expires_at is not None and now >= self.expires_at:
            return False
        return True

    def discount_for(self, amount_minor: int) -> int:
        """
        What this takes off a given amount, in minor units.

        Clamped to the amount itself, so a 500-rupee fixed discount on a
        300-rupee plan makes it free rather than negative. Percentages round
        **down**, which puts the rounding error on the seller rather than
        charging a customer a paisa more than the page said.
        """
        amount_minor = max(int(amount_minor), 0)
        if self.kind == self.Kind.PERCENT:
            return min(amount_minor * int(self.value) // 100, amount_minor)
        if self.kind == self.Kind.FIXED:
            return min(int(self.value), amount_minor)
        return 0


class CouponRedemption(models.Model):
    """
    One use of one coupon by one workspace.

    Written when the checkout is created, not when the payment lands, and that
    is the load-bearing decision. A usage limit counted only at capture can be
    walked straight through by opening fifty checkouts at once, so "limited to
    100 redemptions" would mean nothing on the one day it mattered.

    A redemption therefore starts RESERVED and holds a slot. It becomes
    REDEEMED when the payment captures and RELEASED when the checkout fails or
    expires, at which point it stops counting --
    `services.expire_stale_checkouts` releases the ones nobody came back for.

    `checkout` is null only for a TRIAL_EXTENSION, which has no order behind
    it. The unique constraint on `(coupon, checkout)` makes reserving
    idempotent when a request is retried.
    """

    class Status(models.TextChoices):
        RESERVED = "RESERVED", "Reserved"
        REDEEMED = "REDEEMED", "Redeemed"
        RELEASED = "RELEASED", "Released"

    coupon = models.ForeignKey(Coupon, on_delete=models.PROTECT, related_name="redemptions")
    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="coupon_redemptions",
    )
    checkout = models.ForeignKey(
        Checkout, on_delete=models.CASCADE, related_name="coupon_redemptions", null=True, blank=True,
    )

    #: What it actually took off, frozen at reservation. Kept here rather than
    #: recomputed from the coupon, so editing a coupon's percentage later
    #: cannot rewrite what a past customer was charged.
    discount_minor = models.PositiveIntegerField(default=0)
    #: Days added, for a TRIAL_EXTENSION. Zero for the discount kinds.
    trial_days = models.PositiveIntegerField(default=0)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RESERVED)

    created_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    #: The statuses that occupy a slot against a coupon's limits. RELEASED
    #: does not, which is the entire reason there are three rather than two.
    COUNTED = (Status.RESERVED, Status.REDEEMED)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["coupon", "status"], name="redemption_coupon_status_idx"),
            models.Index(fields=["tenant", "-created_at"], name="redemption_tenant_time_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["coupon", "checkout"],
                condition=models.Q(checkout__isnull=False),
                name="unique_coupon_per_checkout",
            ),
        ]

    def __str__(self):
        return f"{self.coupon_id} by tenant {self.tenant_id} ({self.status})"
