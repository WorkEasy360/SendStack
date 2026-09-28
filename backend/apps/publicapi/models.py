"""
The rows behind the developer platform: keys, request logs, idempotency
records, webhook endpoints and webhook deliveries.

Every one of them carries `tenant` directly rather than reaching it through a
join. That is the rule the rest of this project follows on its hot tables and
it exists here for the same reason: isolation that depends on a join is
isolation somebody can forget to write.
"""

import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

from . import keys as key_utils
from .scopes import SCOPES


class ApiKey(models.Model):
    """
    One credential belonging to one tenant.

    The full key is never stored. `key_prefix` is in clear so a request can be
    resolved in one indexed query, and `hashed_secret` is the SHA-256 of the
    other half -- see keys.py for why that is the right hash for this and
    Argon2 is not.

    Revocation, expiry and disabling are three deliberately separate things:

    * **disabled** (`is_active = False`) is reversible. The integration is
      paused; somebody will turn it back on.
    * **revoked** (`revoked_at` set) is not. The secret is considered burned,
      and the row survives only so the request log can still name what made
      those calls.
    * **expired** (`expires_at` passed) is neither -- nobody did anything, the
      clock did.

    Kept apart so the error a developer gets says which of the three happened,
    because the fix is different for each.
    """

    class Environment(models.TextChoices):
        LIVE = "live", "Live"
        TEST = "test", "Test"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="api_keys")

    # The user this key acts as, and -- through User.credits -- the account
    # every message it sends is billed to. PROTECT rather than SET_NULL: a key
    # with no owner has nothing to charge and no identity to inherit, and
    # silently becoming that on a user deletion would be worse than refusing
    # the deletion until the keys are dealt with.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="api_keys",
        help_text="Requests act as this user; messages are billed to their credit balance.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    name = models.CharField(max_length=100, help_text="What is using this key, e.g. 'Shopify store'.")
    environment = models.CharField(max_length=8, choices=Environment.choices, default=Environment.LIVE)

    key_prefix = models.CharField(max_length=16, unique=True, db_index=True)
    hashed_secret = models.CharField(max_length=64)

    scopes = models.JSONField(default=list, blank=True)

    # Which WhatsApp accounts this key may send from. Empty means *all of the
    # tenant's* accounts, which is what a key created without thinking about
    # it gets. Non-empty narrows it further -- a warehouse integration that
    # should only ever use the dispatch number. It can never widen: the tenant
    # filter is applied first, separately, and does not consult this.
    allowed_accounts = models.ManyToManyField(
        "whatsapp.WhatsAppAccount",
        blank=True,
        related_name="api_keys",
        help_text="Leave empty to allow every WhatsApp account in this workspace.",
    )

    # --- Rate limits -----------------------------------------------------
    #
    # 0 means "use the deployment default" rather than "unlimited". That is
    # the opposite of the "0 means no limit" convention the plan quotas use,
    # and the difference is deliberate: a forgotten field on a *quota* should
    # not cost the customer money, and a forgotten field on a *rate limit*
    # should not cost the platform its workers. See throttling.py, the only
    # reader of these three.
    requests_per_minute = models.PositiveIntegerField(default=0)
    messages_per_minute = models.PositiveIntegerField(default=0)
    campaigns_per_day = models.PositiveIntegerField(default=0)

    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    last_used_at = models.DateTimeField(null=True, blank=True)
    last_used_ip = models.GenericIPAddressField(null=True, blank=True)
    # The User-Agent of the most recent request. This is the "which application
    # is using it" column in the dashboard -- self-reported and therefore not
    # evidence, but in practice it is how somebody recognises the key their old
    # Zapier zap is still holding.
    last_user_agent = models.CharField(max_length=255, blank=True)

    # Denormalised counters. The request log is authoritative and can answer
    # anything these can, but it is pruned on a retention schedule and reading
    # it is a scan; these two are what the list screen renders per row.
    request_count = models.BigIntegerField(default=0)
    message_count = models.BigIntegerField(default=0)

    # Set when this key replaced another. Rotation keeps both rows so the old
    # key's request history keeps a name, and so the dashboard can show
    # "rotated on the 3rd" rather than a key that simply vanished.
    rotated_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="rotations",
    )
    rotated_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.masked_key})"

    @property
    def masked_key(self) -> str:
        return key_utils.masked(self.environment, self.key_prefix)

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= timezone.now())

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def status(self) -> str:
        """One word for the dashboard's Status column, in refusal order."""
        if self.is_revoked:
            return "revoked"
        if self.is_expired:
            return "expired"
        if not self.is_active:
            return "disabled"
        return "active"

    def has_scope(self, scope: str) -> bool:
        return scope in (self.scopes or [])

    def scope_labels(self) -> list[str]:
        return [SCOPES[scope] for scope in (self.scopes or []) if scope in SCOPES]


class ApiRequestLog(models.Model):
    """
    One row per public-API request, written after the response is built.

    Deliberately narrow. It records *that* a call happened, by whom, to what,
    and how it ended -- never the request body, which on this API is a list of
    phone numbers and the text being sent to them. `error_code` is the one
    piece of failure detail kept, and it is ours: a fixed vocabulary that
    carries no customer data.
    """

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="api_request_logs")
    # SET_NULL rather than CASCADE: deleting a key must not erase the record of
    # what it did. `key_prefix` below is the copy that outlives it.
    api_key = models.ForeignKey(ApiKey, on_delete=models.SET_NULL, null=True, blank=True, related_name="request_logs")
    key_prefix = models.CharField(max_length=16, blank=True)

    method = models.CharField(max_length=10)
    path = models.CharField(max_length=255)
    status_code = models.PositiveSmallIntegerField()
    error_code = models.CharField(max_length=40, blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)

    # What the call produced, when it produced one of these. Lets "which API
    # call sent message 918?" be answered without joining three tables.
    message_id = models.BigIntegerField(null=True, blank=True)
    campaign_id = models.BigIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["api_key", "-created_at"]),
            models.Index(fields=["tenant", "status_code", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.method} {self.path} -> {self.status_code}"


class IdempotencyRecord(models.Model):
    """
    The stored answer to a request that carried an ``Idempotency-Key``.

    Two things are kept beside the response.

    `fingerprint` is a hash of the request body, so re-using one key for a
    *different* request is refused rather than silently answered with the wrong
    stored result. That is almost always a bug in the caller, and handing back
    the old answer would hide it.

    `state` distinguishes "in flight" from "finished", which is what makes two
    concurrent retries safe: the second is told the first is still running
    instead of being allowed through to send a second message.

    Scoped to the tenant rather than to the key, so rotating a key mid-job
    cannot make a retry from an in-flight worker send twice.
    """

    class State(models.TextChoices):
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETED = "COMPLETED", "Completed"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="idempotency_records")
    api_key = models.ForeignKey(ApiKey, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    key = models.CharField(max_length=255)
    endpoint = models.CharField(max_length=255)
    fingerprint = models.CharField(max_length=64)

    state = models.CharField(max_length=12, choices=State.choices, default=State.IN_PROGRESS)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "key"], name="unique_idempotency_key_per_tenant"),
        ]

    def __str__(self):
        return f"{self.key} ({self.state})"


class WebhookEndpoint(models.Model):
    """
    Somewhere a tenant wants events delivered.

    The signing secret is stored **encrypted, not hashed**, because unlike an
    API key we have to reproduce it: every delivery computes an HMAC with it.
    It uses the same Fernet key as the WhatsApp provider credentials, so this
    deployment has one secret-at-rest story rather than two.

    `consecutive_failures` and `auto_disabled_at` are the circuit breaker. An
    endpoint whose owner took it down months ago must stop consuming worker
    time, and must stop *visibly* -- disabled with a reason on the dashboard,
    not silently dropped.
    """

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="webhook_endpoints")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    name = models.CharField(max_length=100, blank=True)
    url = models.URLField(max_length=500)
    secret_encrypted = models.TextField(blank=True)

    # Which events to deliver here. Empty means *every* event, which is what a
    # developer wiring up their first endpoint almost always wants and what
    # they get by omitting the field.
    events = models.JSONField(default=list, blank=True)

    is_active = models.BooleanField(default=True)

    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    auto_disabled_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "is_active"])]

    def __str__(self):
        return self.name or self.url

    @staticmethod
    def generate_secret() -> str:
        """A signing secret, in the shape developers expect one to arrive in."""
        return f"whsec_{secrets.token_hex(24)}"

    def get_secret(self) -> str | None:
        from apps.whatsapp.connections.session import decrypt_secret

        return decrypt_secret(self.secret_encrypted)

    def set_secret(self, raw: str) -> None:
        from apps.whatsapp.connections.session import encrypt_secret

        self.secret_encrypted = encrypt_secret(raw)

    def wants(self, event_type: str) -> bool:
        return not self.events or event_type in self.events


class WebhookDelivery(models.Model):
    """
    One attempt-tracked delivery of one event to one endpoint.

    A row per (event, endpoint) pair, not per attempt: a retry updates this row
    and bumps `attempt_count`, so the delivery log reads as a list of events
    rather than a list of network calls.

    `response_snippet` is truncated hard. It exists so a developer can see
    *their own* 500 page, not so this table becomes a mirror of somebody
    else's website.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="webhook_deliveries")
    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, related_name="deliveries")

    event_type = models.CharField(max_length=50)
    # The id carried in the payload. Stable across retries, so a receiver can
    # deduplicate on it -- which is the other half of at-least-once delivery.
    event_id = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_ms = models.PositiveIntegerField(null=True, blank=True)
    response_snippet = models.CharField(max_length=300, blank=True)
    error = models.CharField(max_length=300, blank=True)

    next_attempt_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["endpoint", "-created_at"]),
            models.Index(fields=["status", "next_attempt_at"]),
        ]

    def __str__(self):
        return f"{self.event_type} -> endpoint {self.endpoint_id} ({self.status})"
