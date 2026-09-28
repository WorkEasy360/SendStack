from django.conf import settings
from django.db import models

from .connections.session import decrypt_secret, encrypt_secret


class ActiveAccountManager(models.Manager):
    """
    The devices a tenant still has.

    Archived devices are hidden from every ordinary query -- the device list,
    the pickers a campaign or a chatbot chooses a number from, the plan's
    device count -- because as far as the user is concerned they deleted
    them. They stay in the table only so the campaigns and messages that
    reference them (both PROTECT) keep resolving in reports. Use
    ``all_objects`` to see those rows.
    """

    def get_queryset(self):
        return super().get_queryset().filter(archived_at__isnull=True)


class WhatsAppAccount(models.Model):

    class Status(models.TextChoices):
        DISCONNECTED = "DISCONNECTED", "Disconnected"
        CONNECTING = "CONNECTING", "Connecting"
        QR_READY = "QR_READY", "QR Ready"
        CONNECTED = "CONNECTED", "Connected"
        DISCONNECTING = "DISCONNECTING", "Disconnecting"
        ERROR = "ERROR", "Error"
        EXPIRED = "EXPIRED", "Expired"

    class AccountType(models.TextChoices):
        WHATSAPP = "WHATSAPP", "WhatsApp"
        BUSINESS = "BUSINESS", "WhatsApp Business"
        API = "API", "WhatsApp API"

    class ConnectionMethod(models.TextChoices):
        QR = "QR", "QR / Device Linking"
        CLOUD_API = "CLOUD_API", "WhatsApp Cloud API"

    class Provider(models.TextChoices):
        CLOUD_API = "CLOUD_API", "WhatsApp Cloud API"
        QR_PROVIDER = "QR_PROVIDER", "QR Connection Provider"

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="whatsapp_accounts",
        null=True,
        blank=True,
        help_text="Tenant that owns this account. Backfilled from `user` if omitted.",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="whatsapp_accounts",
    )

    name = models.CharField(
        max_length=100
    )

    phone_number = models.CharField(
        max_length=30,
        blank=True,
        null=True,
    )

    account_type = models.CharField(
        max_length=20,
        choices=AccountType.choices,
        default=AccountType.WHATSAPP,
    )

    connection_method = models.CharField(
        max_length=20,
        choices=ConnectionMethod.choices,
        default=ConnectionMethod.CLOUD_API,
    )

    provider = models.CharField(
        max_length=20,
        choices=Provider.choices,
        default=Provider.CLOUD_API,
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DISCONNECTED,
    )

    error_message = models.CharField(
        max_length=500,
        blank=True,
    )

    # Opaque identifiers handed back by the connected provider. Never used
    # for dispatch (that's `provider`, computed server-side from
    # `connection_method`) -- purely informational/for provider-side lookups.
    provider_account_id = models.CharField(max_length=255, blank=True)
    provider_session_id = models.CharField(
        max_length=255,
        unique=True,
        blank=True,
        null=True,
    )

    # WhatsApp Cloud API credentials. The access token and app secret are
    # never stored in plain text -- see connections/session.py.
    phone_number_id = models.CharField(max_length=64, blank=True)
    whatsapp_business_account_id = models.CharField(max_length=64, blank=True)
    access_token_encrypted = models.TextField(blank=True)
    app_secret_encrypted = models.TextField(blank=True)
    webhook_verify_token = models.CharField(max_length=128, blank=True)

    last_connected_at = models.DateTimeField(
        blank=True,
        null=True,
    )

    last_seen_at = models.DateTimeField(
        blank=True,
        null=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    # Set when the user deletes a device that campaigns or messages still
    # reference. Those relations are PROTECT on purpose -- send history
    # outlives the number it went out from -- so the row cannot leave the
    # table, but the device leaves the product: archived accounts are gone
    # from every list, cannot be sent from, and stop counting against the
    # plan's device limit. A device nothing references is deleted outright
    # and never gets here. See apps.whatsapp.views.perform_destroy.
    archived_at = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
    )

    #: Default manager, and so what every reverse relation and picker sees:
    #: live devices only.
    objects = ActiveAccountManager()
    #: Archived rows included -- for history, reporting and support.
    all_objects = models.Manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
        ]

    def __str__(self):
        return f"{self.name} - {self.phone_number or 'Not connected'}"

    def save(self, *args, **kwargs):
        if self.tenant_id is None and self.user_id and getattr(self.user, "tenant_id", None):
            self.tenant_id = self.user.tenant_id
        # `provider` is always derived from `connection_method`, never taken
        # from client input -- avoids arbitrary provider-string injection
        # into the registry lookup used for send/connect dispatch.
        self.provider = (
            self.Provider.QR_PROVIDER
            if self.connection_method == self.ConnectionMethod.QR
            else self.Provider.CLOUD_API
        )
        super().save(*args, **kwargs)

    def get_access_token(self) -> str | None:
        return decrypt_secret(self.access_token_encrypted)

    def set_access_token(self, raw_token: str | None) -> None:
        self.access_token_encrypted = encrypt_secret(raw_token) if raw_token else ""

    def get_app_secret(self) -> str | None:
        return decrypt_secret(self.app_secret_encrypted)

    def set_app_secret(self, raw_secret: str | None) -> None:
        self.app_secret_encrypted = encrypt_secret(raw_secret) if raw_secret else ""


class WhatsAppWebhookEvent(models.Model):
    """
    A deduplicated, idempotency-safe log of raw inbound webhook events.

    Downstream processing (updating message status, creating inbox messages)
    happens outside this app once messaging/inbox exist; this table's job is
    solely to record what the provider sent and guarantee each event is only
    ever processed once, per the platform's webhook-idempotency requirement.
    """

    class EventType(models.TextChoices):
        MESSAGE_STATUS = "MESSAGE_STATUS", "Message status"
        INBOUND_MESSAGE = "INBOUND_MESSAGE", "Inbound message"

    account = models.ForeignKey(
        WhatsAppAccount,
        on_delete=models.CASCADE,
        related_name="webhook_events",
    )
    event_type = models.CharField(max_length=20, choices=EventType.choices)
    provider_event_id = models.CharField(max_length=255)
    payload = models.JSONField()

    # Generic, provider-agnostic fields populated from WebhookEvent.message_id/.status
    # (see providers/base.py) so consumers never need to parse provider-specific payloads.
    related_message_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, blank=True)

    processed_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "provider_event_id"],
                name="unique_webhook_event_per_account",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "processed_at"]),
        ]

    def __str__(self):
        return f"{self.event_type}:{self.provider_event_id}"


class WhatsAppConnectionSession(models.Model):
    """
    One attempt at connecting a WhatsAppAccount (a QR pairing attempt, or a
    Cloud API credential check). Tracks lifecycle only -- no provider
    credentials and no QR image/payload live here. The QR payload itself is
    a short-lived artifact cached in Redis (see connections/manager.py) and
    is never persisted to the database.
    """

    class Status(models.TextChoices):
        CONNECTING = "CONNECTING", "Connecting"
        QR_READY = "QR_READY", "QR Ready"
        CONNECTED = "CONNECTED", "Connected"
        DISCONNECTED = "DISCONNECTED", "Disconnected"
        ERROR = "ERROR", "Error"
        EXPIRED = "EXPIRED", "Expired"

    whatsapp_account = models.ForeignKey(
        WhatsAppAccount,
        on_delete=models.CASCADE,
        related_name="connection_sessions",
    )
    provider = models.CharField(max_length=20, choices=WhatsAppAccount.Provider.choices)
    session_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CONNECTING)
    error_message = models.CharField(max_length=500, blank=True)
    qr_expires_at = models.DateTimeField(null=True, blank=True)
    connected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["whatsapp_account", "-created_at"]),
            models.Index(fields=["provider", "session_id"]),
        ]

    def __str__(self):
        return f"{self.whatsapp_account_id}:{self.session_id or 'pending'}:{self.status}"


class SendingPolicy(models.Model):
    """
    How hard one number is allowed to be pushed.

    A WhatsApp number that suddenly sends far more than it ever has is the
    single clearest signal that gets a number banned, and a banned number
    costs the customer their whole channel -- not just the campaign that
    triggered it. So every account has a ceiling, and a number that has only
    just been connected is ramped up to it over its first days rather than
    being handed the full allowance on day one.

    Every limit is nullable and falls back to the deployment default (see
    settings.WHATSAPP_*), which is the same pattern campaign send delays use:
    a deployment that only sets the environment variables gets consistent
    behaviour across every account, and a single problem number can still be
    given its own ceiling without touching the others.
    """

    account = models.OneToOneField(WhatsAppAccount, on_delete=models.CASCADE, related_name="sending_policy")

    daily_limit = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Most messages this number may send per day. Blank uses the platform default.",
    )
    hourly_limit = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Most messages this number may send per hour. Blank uses the platform default.",
    )

    warmup_enabled = models.BooleanField(
        default=True,
        help_text="Ramp a newly connected number up to its full daily limit over its first days.",
    )
    warmup_started_on = models.DateField(
        null=True, blank=True,
        help_text="Day the ramp is measured from. Set from the number's first send when the policy is created.",
    )

    auto_pause_enabled = models.BooleanField(
        default=True,
        help_text="Stop sending automatically when this number starts failing at an unusual rate.",
    )

    # Paused with no `paused_until` is indefinite -- that is what a suspected
    # ban or a human hitting pause looks like, and it takes a human to undo.
    # An automatic pause always sets one, so a blip that clears on its own
    # doesn't need anybody to notice it before the number sends again.
    is_paused = models.BooleanField(default=False)
    paused_at = models.DateTimeField(null=True, blank=True)
    paused_until = models.DateTimeField(null=True, blank=True)
    pause_reason = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "sending policies"

    def __str__(self):
        return f"Sending policy for account {self.account_id}"


class AccountSendQuota(models.Model):
    """
    One counter for one account over one window.

    Counting rows in `messaging_message` would drift the moment a message was
    retried or reassigned, and could not be reserved against: two workers both
    reading "999 of 1000" would both send. A slot here is taken by locking the
    row, so the number of messages that leave a number is the number of slots
    it actually had.
    """

    class Scope(models.TextChoices):
        DAY = "DAY", "Day"
        HOUR = "HOUR", "Hour"

    account = models.ForeignKey(WhatsAppAccount, on_delete=models.CASCADE, related_name="send_quotas")
    scope = models.CharField(max_length=5, choices=Scope.choices)
    window_start = models.DateTimeField()
    sent_count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "scope", "window_start"], name="unique_send_quota_window",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "scope", "-window_start"]),
        ]

    def __str__(self):
        return f"{self.account_id}:{self.scope}:{self.window_start:%Y-%m-%d %H}={self.sent_count}"


class AccountHealthEvent(models.Model):
    """
    Things that happened to a number that someone may need to act on.

    Separate from the audit log because these have no actor -- nobody *did*
    them, they happened to the account -- and because they are what the health
    panel and any future alerting read. Kept append-only.
    """

    class Event(models.TextChoices):
        DISCONNECTED = "DISCONNECTED", "Disconnected"
        RECONNECTED = "RECONNECTED", "Reconnected"
        PAUSED = "PAUSED", "Sending paused"
        RESUMED = "RESUMED", "Sending resumed"
        LIMIT_REACHED = "LIMIT_REACHED", "Send limit reached"
        FAILURE_SPIKE = "FAILURE_SPIKE", "Unusual failure rate"
        BAN_SUSPECTED = "BAN_SUSPECTED", "Possible block or ban"

    class Severity(models.TextChoices):
        INFO = "INFO", "Info"
        WARNING = "WARNING", "Warning"
        CRITICAL = "CRITICAL", "Critical"

    account = models.ForeignKey(WhatsAppAccount, on_delete=models.CASCADE, related_name="health_events")
    event = models.CharField(max_length=20, choices=Event.choices)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.INFO)
    detail = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.account_id}:{self.event}"
