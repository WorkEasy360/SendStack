from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.text import slugify


class Tenant(models.Model):
    """An isolated business scope in the SendStack hierarchy."""

    class Kind(models.TextChoices):
        PLATFORM = "PLATFORM", "Platform"
        RESELLER = "RESELLER", "Reseller"
        CUSTOMER = "CUSTOMER", "Customer"

    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=160, unique=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="children", null=True, blank=True
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["parent", "name"], name="unique_tenant_name_per_parent"),
        ]
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def is_descendant_of(self, tenant):
        current = self.parent
        while current is not None:
            if current.pk == tenant.pk:
                return True
            current = current.parent
        return False

    def __str__(self):
        return self.name


class TenantBranding(models.Model):
    """
    How the product presents itself to one tenant's users.

    Every field may be left blank, and blank means *inherit* rather than
    "empty": a reseller sets their brand once and each customer they create
    below them shows it, without anyone copying values downwards. Resolution
    walks up `Tenant.parent` and falls back to the platform's own defaults, so
    there is always something to render -- see `resolve` below.

    Kept as its own row rather than columns on Tenant because it is read on
    every page load by every user, and written about once in a tenant's life.
    """

    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="branding")

    product_name = models.CharField(max_length=60, blank=True)
    logo_url = models.URLField(blank=True)
    primary_color = models.CharField(
        max_length=7, blank=True, help_text="Hex like #0F766E. The whole accent ramp is derived from it."
    )
    support_email = models.EmailField(blank=True)

    # Where this brand is served. Unique so one host cannot resolve to two
    # brands; blank is stored as NULL precisely so many tenants may have none.
    custom_domain = models.CharField(max_length=255, blank=True, null=True, unique=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # What every chain ends at, when nobody above has set anything.
    DEFAULTS = {
        "product_name": "SendStack",
        "logo_url": "",
        "primary_color": "#059669",
        "support_email": "",
    }
    INHERITED_FIELDS = ("product_name", "logo_url", "primary_color", "support_email")

    def __str__(self):
        return f"Branding for tenant {self.tenant_id}"

    def save(self, *args, **kwargs):
        # "" and NULL both mean "no custom domain", but only NULL can repeat
        # under a unique constraint.
        if not self.custom_domain:
            self.custom_domain = None
        else:
            self.custom_domain = self.custom_domain.strip().lower()
        super().save(*args, **kwargs)

    @classmethod
    def resolve(cls, tenant) -> dict:
        """
        The branding a tenant's users should actually see.

        Each field is taken from the nearest ancestor that set it, so a
        customer can override their reseller's product name while still
        inheriting their colour. `depth` is bounded rather than trusted: a
        parent cycle introduced by bad data must not hang a page load.
        """
        resolved = dict(cls.DEFAULTS)
        remaining = set(cls.INHERITED_FIELDS)

        current = tenant
        seen = set()
        while current is not None and remaining and current.pk not in seen:
            seen.add(current.pk)
            branding = getattr(current, "branding", None)
            if branding is not None:
                for field in list(remaining):
                    value = getattr(branding, field, "")
                    if value:
                        resolved[field] = value
                        remaining.discard(field)
            current = current.parent

        return resolved


class BusinessProfile(models.Model):
    """
    What the business is, written once, read by every agent.

    Before this there were two answers to "who is this business": a free-text
    `business_context` box on the WhatsApp agent and a second one on the
    calling agent. They drifted, because nothing made them agree -- a tenant
    who told the chat agent about their new delivery charge had no reason to
    think the phone agent needed telling too.

    The fields are structured rather than one bigger text box so the same
    record can later drive onboarding, recommendations and the agent
    instructions without anything having to parse prose.

    Deliberately **not** stored here, because they already exist and a second
    copy would be a second thing to be wrong:

    * business hours -- `teams.WorkingHours`, rendered into the prompt below
    * quiet hours and opt-out policy -- `compliance.CompliancePolicy`
    * the business's name -- `Tenant.name`, unless overridden here
    * anything long enough to be a document -- the knowledge base, which is
      retrieved per question instead of sent on every one

    Like CompliancePolicy and AssignmentPolicy, `for_tenant` hands back an
    unsaved default rather than writing one, so reading this on the inbound
    message path never turns a read into a write.
    """

    class Tone(models.TextChoices):
        FRIENDLY = "FRIENDLY", "Friendly and warm"
        PROFESSIONAL = "PROFESSIONAL", "Professional"
        CONCISE = "CONCISE", "Short and direct"
        ENTHUSIASTIC = "ENTHUSIASTIC", "Enthusiastic"

    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="business_profile")

    # Blank means "use Tenant.name", which is right for the overwhelming
    # majority and is one less box on the onboarding screen.
    trading_name = models.CharField(
        max_length=150, blank=True, help_text="What customers call you, if that differs from your account name.",
    )
    description = models.TextField(
        blank=True, help_text="What the business does, in a sentence or two. 'We sell laptops and accessories.'",
    )
    products = models.TextField(blank=True, help_text="What you sell. One per line is fine.")
    services = models.TextField(blank=True, help_text="What you do for customers, as opposed to what you sell.")
    locations = models.TextField(blank=True, help_text="Where you are, and where you deliver or serve.")
    contact_details = models.TextField(
        blank=True, help_text="What the agent may give out: a phone number, an email, an address.",
    )

    # Short standing rules, not a policy library. "Never quote a discount
    # above 10%" belongs here; the returns policy belongs in the knowledge
    # base, where it is retrieved for the questions that need it instead of
    # being sent with every reply.
    policies = models.TextField(
        blank=True,
        help_text="Short rules the agent must always follow. Long policy documents belong in the knowledge base.",
    )
    escalation_rules = models.TextField(
        blank=True, help_text="When the agent should stop and fetch a person, beyond the usual cases.",
    )

    languages = models.CharField(
        max_length=200, blank=True, help_text="Languages you serve customers in, e.g. English, Hindi, Hinglish.",
    )
    tone = models.CharField(max_length=20, choices=Tone.choices, default=Tone.FRIENDLY)
    target_customers = models.TextField(blank=True, help_text="Who you sell to.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    #: Everything that is rendered into an agent prompt, and the words that
    #: introduce it. Ordered the way a person would explain their business.
    PROMPT_FIELDS = (
        ("description", "What we do"),
        ("products", "What we sell"),
        ("services", "Services we offer"),
        ("locations", "Where we are"),
        ("contact_details", "How customers can reach us"),
        ("target_customers", "Who we sell to"),
        ("policies", "Rules you must always follow"),
        ("escalation_rules", "When to fetch a person"),
    )

    def __str__(self):
        return f"Business profile for tenant {self.tenant_id}"

    @classmethod
    def for_tenant(cls, tenant_id):
        """The tenant's profile, or an unsaved default -- never a write on a read path."""
        profile = cls.objects.filter(tenant_id=tenant_id).select_related("tenant").first()
        return profile or cls(tenant_id=tenant_id)

    @property
    def is_configured(self) -> bool:
        """Whether anybody has actually said anything about this business."""
        return any((getattr(self, name) or "").strip() for name, _ in self.PROMPT_FIELDS)

    def display_name(self) -> str:
        if self.trading_name.strip():
            return self.trading_name.strip()
        tenant = getattr(self, "tenant", None)
        return tenant.name if tenant is not None else ""

    def to_prompt(self) -> str:
        """
        The business, written for a model to read.

        Empty fields are left out entirely, for the same reason the customer
        context leaves them out: a heading with nothing under it spends the
        model's attention on a fact that does not exist.
        """
        if not self.is_configured and not self.trading_name.strip():
            return ""

        lines = []
        name = self.display_name()
        if name:
            lines.append(f"You are answering on behalf of {name}.")

        for name_of_field, label in self.PROMPT_FIELDS:
            value = (getattr(self, name_of_field) or "").strip()
            if value:
                lines.append(f"{label}: {value}")

        if self.languages.strip():
            lines.append(f"Languages we serve customers in: {self.languages.strip()}")

        tone = dict(self.Tone.choices).get(self.tone)
        if tone:
            lines.append(f"How to sound: {tone.lower()}.")

        hours = self.hours_line()
        if hours:
            lines.append(f"Opening hours: {hours}")

        return "\n".join(lines)

    def hours_line(self) -> str:
        """
        Opening hours read from where they already live, not stored again here.

        Guarded because this runs inside prompt assembly on a live call: a
        tenant who has never opened the Team screen has no rows, and that is a
        blank line, not a failure.
        """
        try:
            from apps.teams.models import WorkingHours

            rows = list(WorkingHours.objects.filter(tenant_id=self.tenant_id, is_open=True).order_by("weekday"))
        except Exception:  # noqa: BLE001 - hours are decoration on a prompt, never a reason to fail
            return ""

        if not rows:
            return ""
        return "; ".join(
            f"{row.get_weekday_display()} {row.opens_at:%H:%M}-{row.closes_at:%H:%M}" for row in rows
        )


class AccountDeletionRequest(models.Model):
    """
    A customer asking for their workspace to be deleted.

    Deliberately a **request** rather than a button that deletes. Three reasons,
    and the third is the one that decides it:

    * A workspace is a business. Deleting one is not undoable, and there is no
      version of "are you sure?" that makes an instant, irreversible destroy
      button safe at 11pm on a Friday.
    * A compromised account must not be able to destroy the business it broke
      into. A request that a real person notices, with a cooling-off window, is
      the difference between a break-in and a catastrophe.
    * The deletion itself already exists, with guards, in
      ``manage.py delete_tenant`` -- which refuses a workspace with children,
      refuses the platform tenant, requires the slug typed out, and reports
      what it kept. Reimplementing that behind an HTTP endpoint would be
      reimplementing it *without* those guards.

    So this records the ask, starts a clock, and lets the customer change their
    mind. An operator carries it out.

    **It does not itself delete anything**, and the cooling-off window is not a
    dark pattern: the customer can cancel it, is told the date, and the whole
    thing is visible on their own settings page.
    """

    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", "Requested"
        CANCELLED = "CANCELLED", "Cancelled by the customer"
        COMPLETED = "COMPLETED", "Carried out"
        REJECTED = "REJECTED", "Declined"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="deletion_requests")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.REQUESTED)

    reason = models.TextField(blank=True, help_text="Why, in the customer's own words. Optional.")
    requested_at = models.DateTimeField(auto_now_add=True)
    # When an operator may act. The customer can cancel freely until then, and
    # the date is shown to them rather than being a quiet delay.
    eligible_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.CharField(max_length=500, blank=True)

    #: How long the customer has to change their mind.
    COOLING_OFF_DAYS = 7

    class Meta:
        ordering = ["-requested_at"]
        indexes = [models.Index(fields=["status", "eligible_at"], name="deletion_status_due_idx")]
        constraints = [
            # One open request per workspace. A second would make "has this
            # workspace asked to be deleted?" have two answers.
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(status="REQUESTED"),
                name="unique_open_deletion_request_per_tenant",
            ),
        ]

    def __str__(self):
        return f"Deletion request for tenant {self.tenant_id} ({self.status})"

    def save(self, *args, **kwargs):
        if not self.eligible_at:
            from datetime import timedelta

            from django.utils import timezone

            self.eligible_at = timezone.now() + timedelta(days=self.COOLING_OFF_DAYS)
        super().save(*args, **kwargs)

    @property
    def is_open(self) -> bool:
        return self.status == self.Status.REQUESTED


class TenantSettings(models.Model):
    """
    One workspace's own sending and housekeeping preferences.

    Everything here already had a deployment-wide answer in ``settings.py`` and
    was consequently the same for every customer on the box. That is the wrong
    granularity for pacing: a customer running six warmed-up numbers into an
    opted-in list and a customer running one week-old number into a cold one
    need different delays, and neither of them can edit the deployment's
    environment.

    Follows ``compliance.CompliancePolicy`` exactly, including the two
    decisions that matter:

      * ``for_tenant`` returns an **unsaved** instance when a workspace has
        never opened the settings page, so the send path can consult it
        without a write on the hot path and without rows appearing behind
        somebody's back;
      * every value has a working default, and the defaults are the behaviour
        that was already in force -- so a workspace that never touches this
        screen sends exactly as it did before this model existed.

    Delay is the one exception to "the default is a value": NULL there means
    "use the deployment default", which is what keeps
    ``settings.CAMPAIGN_MIN/MAX_DELAY_SECONDS`` meaningful as the fallback and
    what every campaign resolved to before.
    """

    class DelayMode(models.TextChoices):
        RANDOM = "RANDOM", "Random within a window"
        FIXED = "FIXED", "The same every time"

    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="settings")

    # -- Delay --------------------------------------------------------------
    #
    # FIXED collapses the window to its minimum rather than adding a second
    # pair of columns: one number that is both bounds *is* a fixed delay, and
    # a mode that reads from different fields than the window it replaces is
    # how a setting ends up meaning one thing on screen and another in the
    # worker.
    delay_mode = models.CharField(max_length=10, choices=DelayMode.choices, default=DelayMode.RANDOM)
    min_delay_seconds = models.FloatField(
        null=True, blank=True, validators=[MinValueValidator(0)],
        help_text="Shortest gap between two messages. Blank uses the platform default.",
    )
    max_delay_seconds = models.FloatField(
        null=True, blank=True, validators=[MinValueValidator(0)],
        help_text="Longest gap between two messages. Ignored when the delay is fixed.",
    )

    # -- Batching -----------------------------------------------------------
    #
    # 0 disables, the same convention the plan limits and the retention
    # horizons use throughout this codebase: it is the reading that fails safe
    # if somebody clears the field.
    batch_size = models.PositiveIntegerField(
        default=0,
        help_text="Pause after this many messages. 0 sends continuously.",
    )
    batch_pause_seconds = models.PositiveIntegerField(
        default=60,
        help_text="How long to pause between batches.",
    )

    # -- Sleep --------------------------------------------------------------
    #
    # Distinct from batching, and deliberately so: a batch pause is a fixed,
    # predictable gap, while sleep is a long random one. Running both is the
    # point -- a short regular pause keeps throughput sane, and an occasional
    # long irregular one is what stops a number's send pattern looking like a
    # machine's.
    sleep_enabled = models.BooleanField(default=False)
    sleep_after_messages = models.PositiveIntegerField(
        default=500, help_text="Take a long break after this many messages.",
    )
    sleep_min_seconds = models.PositiveIntegerField(default=300)
    sleep_max_seconds = models.PositiveIntegerField(default=900)

    # -- Retry --------------------------------------------------------------
    #
    # These bound `apps.messaging.tasks.send_message_task`, which has always
    # retried a failed send three times. They cannot loosen the two rules that
    # protect delivery: an *ambiguous* failure is never retried (it risks a
    # duplicate), and the ceiling below is capped at MAX_SEND_RETRIES so a
    # workspace cannot ask a worker to retry a broken number indefinitely.
    retry_enabled = models.BooleanField(
        default=True,
        help_text="Try a failed message again before giving up on it.",
    )
    max_retries = models.PositiveSmallIntegerField(default=3)
    # 30 rather than a rounder number: it is the base `retry_delay` has always
    # been called with, and the promise this model makes is that a workspace
    # which never opens the settings screen behaves exactly as it did before
    # the screen existed.
    retry_delay_seconds = models.PositiveIntegerField(
        default=30, help_text="Base wait before the first retry; later ones back off from it.",
    )

    # -- Housekeeping -------------------------------------------------------
    #
    # 0 means "keep for ever", matching apps.health.retention. What this
    # horizon covers is deliberately narrow and is enforced in
    # `apps.tenants.storage`, not here: finished campaigns and the message
    # rows under them. It can never reach a contact, a credit, a payment, a
    # subscription, an audit event, a consent event or a running campaign.
    campaign_data_retention_days = models.PositiveIntegerField(
        default=0,
        help_text="Delete finished campaigns older than this many days. 0 keeps everything.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "tenant settings"

    def __str__(self):
        return f"Settings for tenant {self.tenant_id}"

    @classmethod
    def for_tenant(cls, tenant):
        """The workspace's settings, or an unsaved default set."""
        tenant_id = getattr(tenant, "pk", tenant)
        found = cls.objects.filter(tenant_id=tenant_id).first()
        return found or cls(tenant_id=tenant_id)

    def resolved_delay_bounds(self) -> tuple:
        """
        ``(min, max)`` in seconds, or ``(None, None)`` when this workspace has
        expressed no preference and the deployment default should apply.

        Returning None rather than substituting the default here is what keeps
        the precedence readable in one place: campaign value, then workspace
        value, then deployment value -- see
        ``apps.campaigns.assignment.resolve_delay_bounds``.
        """
        if self.min_delay_seconds is None and self.max_delay_seconds is None:
            return None, None

        minimum = self.min_delay_seconds
        if minimum is None:
            minimum = self.max_delay_seconds
        if self.delay_mode == self.DelayMode.FIXED:
            return float(minimum), float(minimum)

        maximum = self.max_delay_seconds
        if maximum is None:
            maximum = minimum
        return float(minimum), float(max(maximum, minimum))
