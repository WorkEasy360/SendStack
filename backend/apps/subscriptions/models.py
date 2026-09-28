import math
from datetime import timedelta

from django.conf import settings
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

    # Whether this plan appears on the public pricing page and may be picked
    # by somebody signing themselves up.
    #
    # **Defaults to False, and that is the important part.** A reseller's
    # private plan -- a negotiated price for one customer, a legacy tier, an
    # internal test plan -- must not become a public offer because a
    # commercialization phase shipped. Publishing is an explicit act.
    is_public = models.BooleanField(
        default=False,
        help_text="Show this plan on the public pricing page and allow self-service signup onto it.",
    )
    # Where it sits on that page. Ties break on price, then name, so a
    # deployment that never touches this still gets a sensible cheapest-first
    # ordering.
    display_order = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    FEATURES = ("ai_agent", "workflows", "api_access", "click_tracking", "number_filter", "group_grabber")

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

    def period_length(self) -> timedelta:
        """
        How long one billing period of this plan runs.

        Deliberately a fixed span rather than calendar arithmetic: the monthly
        allowance is a *window*, not a month on a wall calendar, and a fixed
        span is the only version where "used 40 of 1,000 since the 3rd" cannot
        drift by a day depending on which month it is asked in.
        """
        return timedelta(days=365 if self.interval == self.Interval.YEARLY else 30)


class Subscription(models.Model):
    """
    One tenant's place on one plan.

    The period is stored rather than derived from `started_at` so that a
    month's allowance is anchored to a real date somebody can point at.
    `current_period()` is what the message counter measures over -- it rolls
    the stored anchor forward by whole intervals for a live subscription, so
    the allowance resets every month without anybody pressing Renew, and
    leaves it exactly where it is for a cancelled or lapsed one, whose stored
    end date *is* the day its access runs out.

    `status` records what a person decided; `access_state()` says what that
    currently means. They are not the same question, which is why PAST_DUE can
    still be serving a customer and CANCELLED can still be serving one too.
    """

    class Status(models.TextChoices):
        TRIALING = "TRIALING", "Trialing"
        ACTIVE = "ACTIVE", "Active"
        PAST_DUE = "PAST_DUE", "Past due"
        CANCELLED = "CANCELLED", "Cancelled"
        # A trial that ran out, or a paid period that ended and was not
        # renewed. Recorded rather than inferred so the billing page can say
        # "your trial ended on the 14th" instead of "trialing" for ever -- but
        # `access_state` does not depend on this status being written. See the
        # trial note there.
        EXPIRED = "EXPIRED", "Expired"

    tenant = models.OneToOneField("tenants.Tenant", on_delete=models.CASCADE, related_name="subscription")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)

    current_period_start = models.DateTimeField(default=timezone.now)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    # When a free trial stops. NULL on every subscription that is not a trial,
    # including every row written before trials existed -- which is why the
    # trial rules below all begin by checking it is set.
    trial_started_at = models.DateTimeField(null=True, blank=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    #: What `access_state` can answer.
    FULL = "FULL"        # every entitlement the plan grants
    GRACE = "GRACE"      # past due, still inside the grace window
    EXPIRED = "EXPIRED"  # no paid entitlements at all

    class Meta:
        indexes = [models.Index(fields=["status"])]

    def __str__(self):
        return f"Tenant {self.tenant_id} on {self.plan_id}"

    def save(self, *args, **kwargs):
        # A period with a start and no end is not a period, and it is what
        # made `current_period_end` decorative before Phase 2: a cancellation
        # had no date to run out on and a renewal cleared the only one there
        # was. Filled in from the plan's interval whenever it is missing.
        if self.current_period_end is None and self.plan_id and self.current_period_start:
            # Rolled up to the period that contains *now*, not blindly one
            # interval past the anchor: a subscription written months ago and
            # only now given an end date would otherwise be handed one in the
            # past, and read as lapsed the instant the column was filled in.
            length = self.plan.period_length()
            end = self.current_period_start + length
            now = timezone.now()
            if length.total_seconds() > 0 and end < now:
                elapsed = int((now - self.current_period_start) // length)
                end = self.current_period_start + length * (elapsed + 1)
            self.current_period_end = end
        super().save(*args, **kwargs)

    # -- The period ---------------------------------------------------------

    def current_period(self, now=None) -> tuple:
        """
        The window the monthly allowance is measured over, right now.

        Rolled forward arithmetically rather than stored, because nothing in
        this product renews a subscription on a schedule: without this, a
        customer's first month would be their only one and the message
        allowance would never reset. A **live** subscription therefore steps
        its window on by whole intervals until it contains `now`; a cancelled
        or lapsed one does not move, because its stored end *is* the date its
        access runs out on and rolling it would be renewing it for free.

        Pure: this is read on the send path and writes nothing.
        """
        now = now or timezone.now()
        start = self.current_period_start
        end = self.current_period_end or (start + self.plan.period_length())

        if self.status not in (self.Status.ACTIVE, self.Status.TRIALING):
            return start, end

        # A trial past its end date does not roll its window on, for the same
        # reason a cancelled subscription does not: rolling it would hand out
        # a fresh monthly allowance to an account whose access has stopped.
        if self.status == self.Status.TRIALING and self.trial_ends_at is not None and now >= self.trial_ends_at:
            return start, end

        length = self.plan.period_length()
        if length.total_seconds() <= 0 or now < end:
            return start, end

        elapsed = int((now - start) // length)
        return start + length * elapsed, start + length * (elapsed + 1)

    # -- Access -------------------------------------------------------------

    @staticmethod
    def grace_period() -> timedelta:
        days = getattr(settings, "BILLING_PAST_DUE_GRACE_DAYS", 14)
        try:
            return timedelta(days=max(int(days), 0))
        except (TypeError, ValueError):
            return timedelta(days=14)

    def access_state(self, now=None) -> str:
        """
        Whether this subscription still grants what its plan says, and why.

        * ``ACTIVE`` / ``TRIALING`` -> FULL. Nothing in this deployment can
          decide a subscription lapsed -- there is no payment gateway -- so
          only a person changing the status ends it.
        * ``PAST_DUE`` -> FULL while inside the grace window measured from the
          end of the period that went unpaid, EXPIRED after. Cutting a
          customer off the instant an invoice slips is how a billing system
          loses a customer over a expired card.
        * ``CANCELLED`` -> FULL until ``current_period_end`` (they paid for
          the month; they keep the month), EXPIRED after it. A cancellation
          with no end date recorded has nothing left to run out, so it is
          EXPIRED immediately.
        """
        now = now or timezone.now()

        # **A trial ends because its date passed, not because a job noticed.**
        # This is the whole reason `trial_ends_at` is a column and the sweeper
        # is only a bookkeeper: if expiry depended on a Celery task, then a
        # dead beat container, a full queue or a bad deploy would silently
        # hand every trialling tenant unlimited premium access for as long as
        # nobody looked. Derived from the date, it cannot.
        if self.status == self.Status.TRIALING:
            if self.trial_ends_at is not None and now >= self.trial_ends_at:
                return self.EXPIRED
            return self.FULL

        if self.status == self.Status.ACTIVE:
            return self.FULL

        if self.status == self.Status.EXPIRED:
            return self.EXPIRED

        end = self.current_period_end
        if self.status == self.Status.PAST_DUE:
            if end is None:
                # No period recorded: grace runs from when the status last
                # moved, which is the only date this row has.
                end = self.updated_at or self.current_period_start
            # GRACE rather than FULL: it grants exactly what FULL grants --
            # every caller asks `!= EXPIRED` -- but it is the one state the UI
            # can put a banner on, and collapsing it into FULL would mean a
            # customer's first warning was the day everything stopped.
            return self.GRACE if now <= end + self.grace_period() else self.EXPIRED

        if self.status == self.Status.CANCELLED:
            return self.FULL if end is not None and now < end else self.EXPIRED

        return self.EXPIRED

    @property
    def is_live(self) -> bool:
        """Whether this subscription should still be letting work through."""
        return self.access_state() != self.EXPIRED

    # -- Trials -------------------------------------------------------------

    @property
    def is_trial(self) -> bool:
        return self.status == self.Status.TRIALING and self.trial_ends_at is not None

    def trial_days_remaining(self, now=None):
        """
        Whole days left in the trial, or None if this is not a running trial.

        Rounded **up**, because a customer with eleven hours left has a day
        left, and telling them "0 days" while they can still send is the kind
        of small lie that makes people distrust a billing page. Never negative.
        """
        if self.trial_ends_at is None or self.status != self.Status.TRIALING:
            return None
        now = now or timezone.now()
        remaining = (self.trial_ends_at - now).total_seconds()
        if remaining <= 0:
            return 0
        return math.ceil(remaining / 86400)

    def renew(self, now=None) -> None:
        """
        Start a fresh period. This is what resets the monthly allowance.

        Explicit rather than automatic: nothing here talks to a payment
        processor, so the moment a customer's month starts again is a decision
        their reseller makes.
        """
        now = now or timezone.now()
        self.current_period_start = now
        self.current_period_end = now + self.plan.period_length()
        self.status = self.Status.ACTIVE
        self.cancelled_at = None
        self.save(update_fields=[
            "current_period_start", "current_period_end", "status", "cancelled_at", "updated_at",
        ])


class SubscriptionNotice(models.Model):
    """
    One billing message this system has already sent, so it never sends it twice.

    The alternative -- "have we mailed this customer in the last day?" -- is a
    guess, and it gets the two cases that matter exactly backwards: a sweep
    that runs twice after a redeploy mails twice, and a customer whose trial
    genuinely ends the day after a payment failure gets one of the two notices
    swallowed.

    `reference` is what makes the row specific rather than merely typed: for a
    trial notice it is the trial's end date, so a customer who is given a
    *second* trial later gets a second warning, while the same trial can only
    ever produce one. A unique constraint enforces it, which means the
    protection survives concurrent sweeps -- a check-then-send would not.
    """

    class Kind(models.TextChoices):
        TRIAL_ENDING = "TRIAL_ENDING", "Trial ending soon"
        TRIAL_EXPIRED = "TRIAL_EXPIRED", "Trial expired"
        SUBSCRIPTION_ACTIVATED = "SUBSCRIPTION_ACTIVATED", "Subscription activated"
        PAYMENT_FAILED = "PAYMENT_FAILED", "Payment failed"
        SUBSCRIPTION_CANCELLED = "SUBSCRIPTION_CANCELLED", "Subscription cancelled"

    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="notices")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    reference = models.CharField(
        max_length=64,
        help_text="What makes this notice unique -- usually the date or payment it is about.",
    )
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sent_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["subscription", "kind", "reference"], name="unique_subscription_notice",
            ),
        ]

    def __str__(self):
        return f"{self.kind} for subscription {self.subscription_id}"

    @classmethod
    def claim(cls, subscription, kind, reference) -> bool:
        """
        Take the right to send this notice, once. True means "you send it".

        The insert *is* the claim: two sweeps racing on the same notice both
        try to write the row, the database lets exactly one through, and the
        loser is told no. Checking first and sending second would let both
        through in the gap.
        """
        from django.db import IntegrityError, transaction

        try:
            # Its own savepoint. Without one, the IntegrityError would poison
            # any transaction this is called inside -- every later query in it
            # would fail with "current transaction is aborted", turning a
            # duplicate notice into a broken sweep.
            with transaction.atomic():
                cls.objects.create(subscription=subscription, kind=kind, reference=str(reference)[:64])
        except IntegrityError:
            return False
        return True
