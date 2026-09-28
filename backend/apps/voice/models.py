import secrets

from django.conf import settings
from django.db import models

from apps.ai.models import AIAgentConfig
from apps.whatsapp.connections.session import decrypt_secret, encrypt_secret


def _webhook_token() -> str:
    """
    The unguessable half of a call's public webhook URL.

    Every carrier callback is unauthenticated by construction -- Twilio has no
    credential of ours to present -- so the URL itself carries the secret, and
    the signature check verifies the caller is really Twilio. Per call rather
    than per tenant: a token that leaks in a support log then exposes one
    finished call instead of every future one.
    """
    return secrets.token_urlsafe(24)


class VoiceAgentConfig(models.Model):
    """
    One calling agent per tenant: who it is, what it may do, and when it stops.

    The limits are the point of this model, not decoration. An AI on a phone
    call is the business speaking out loud to a customer, with no draft to
    review and no message history to correct afterwards -- so it gets a hard
    turn ceiling, a hard wall-clock ceiling, an explicit list of the actions it
    is permitted to take, and a human to hand to. Off by default.
    """

    class Provider(models.TextChoices):
        TWILIO = "TWILIO", "Twilio"

    tenant = models.OneToOneField("tenants.Tenant", on_delete=models.CASCADE, related_name="voice_agent")
    is_enabled = models.BooleanField(default=False)

    # --- Carrier -----------------------------------------------------------
    provider = models.CharField(max_length=20, choices=Provider.choices, default=Provider.TWILIO)
    # Encrypted at rest with the same Fernet key as the WhatsApp provider
    # credentials, so there is one key to rotate rather than two.
    account_sid_encrypted = models.TextField(blank=True, default="")
    auth_token_encrypted = models.TextField(blank=True, default="")
    from_number = models.CharField(
        max_length=20, blank=True, help_text="The number the call comes from, in E.164 (+14155551234).",
    )

    # Answering the phone, as opposed to dialling it. Off separately from
    # `is_enabled` because the two are different decisions: a business may be
    # happy for the agent to make its own reminder calls and not at all happy
    # for it to be the first voice a customer hears when they ring in.
    answer_inbound = models.BooleanField(default=False)
    inbound_greeting = models.CharField(
        max_length=300, blank=True, help_text="Said when somebody rings in. Blank uses the outbound greeting.",
    )
    # The unguessable half of this tenant's inbound webhook URL. Per tenant
    # rather than per call, because an incoming call has no session yet -- it
    # is this URL that says which tenant is being rung.
    inbound_token = models.CharField(max_length=64, default=_webhook_token, unique=True)

    # --- Voice -------------------------------------------------------------
    display_name = models.CharField(max_length=80, default="Assistant")
    voice = models.CharField(
        max_length=60, default="Polly.Aditi",
        help_text="The carrier's text-to-speech voice. Polly.Aditi speaks Indian English and Hindi.",
    )
    language = models.CharField(
        max_length=15, default="en-IN",
        help_text="Speech-recognition language, e.g. en-IN, en-US, hi-IN.",
    )
    speech_hints = models.TextField(
        blank=True,
        help_text="Comma-separated words the recogniser should expect: product names, your company name.",
    )

    # --- Model -------------------------------------------------------------
    # Which vendor answers, separate from `provider` above -- that one is the
    # carrier that carries the call, this one is the model that does the
    # talking, and a tenant can change either without touching the other.
    model_provider = models.CharField(
        max_length=20, choices=AIAgentConfig.Provider.choices, default=AIAgentConfig.Provider.ANTHROPIC,
    )
    model = models.CharField(max_length=100, default="claude-opus-5")
    # Only read when model_provider is Custom -- the OpenAI-compatible
    # endpoint's base URL, e.g. https://api.groq.com/openai/v1.
    base_url = models.URLField(max_length=300, blank=True, default="")
    # Effort is the latency dial for a live call. A carrier drops the call if a
    # webhook takes too long, so the agent trades depth for staying on the
    # line; the tenant can raise it where their calls are worth the wait.
    effort = models.CharField(
        max_length=10, default="low",
        help_text="low, medium, high, xhigh or max. Lower answers faster, which matters on a live call.",
    )
    temperature = models.FloatField(default=0.3)
    max_output_tokens = models.PositiveIntegerField(
        default=180, help_text="Kept small on purpose: a spoken reply is two or three sentences.",
    )
    api_key_encrypted = models.TextField(blank=True, default="")

    # --- Behaviour ---------------------------------------------------------
    business_context = models.TextField(
        blank=True, help_text="Who the business is and how the agent should sound. Added to every call.",
    )
    default_goal = models.TextField(
        blank=True,
        help_text="What the agent is trying to achieve when a call does not name its own goal.",
    )
    greeting = models.CharField(
        max_length=300, default="Hello, this is the assistant calling. How can I help you today?",
        help_text="The first thing said once the call connects.",
    )
    voicemail_message = models.CharField(
        max_length=300, blank=True,
        help_text="Left when the carrier reports an answering machine. Blank means hang up silently.",
    )
    knowledge_chunks = models.PositiveIntegerField(default=4)
    answer_from_knowledge_only = models.BooleanField(
        default=False,
        help_text="Refuse rather than guess when the answer is not in the knowledge base.",
    )

    # --- Ceilings ----------------------------------------------------------
    max_turns = models.PositiveIntegerField(
        default=20, help_text="Exchanges before the agent wraps the call up. 0 for no limit.",
    )
    max_call_seconds = models.PositiveIntegerField(
        default=300, help_text="Wall-clock ceiling for one call. 0 for no limit.",
    )
    silence_timeout_seconds = models.PositiveIntegerField(
        default=6, help_text="How long to wait for the person to start speaking before re-prompting.",
    )
    max_silent_turns = models.PositiveIntegerField(
        default=2, help_text="Consecutive silences before the agent assumes nobody is there and hangs up.",
    )
    daily_call_limit = models.PositiveIntegerField(
        default=0, help_text="Calls this tenant may place per day. 0 means unlimited.",
    )

    # --- What the agent is allowed to do -----------------------------------
    allow_transfer = models.BooleanField(default=True)
    transfer_number = models.CharField(
        max_length=20, blank=True, help_text="Where 'put me through to a person' goes, in E.164.",
    )
    allow_whatsapp_followup = models.BooleanField(
        default=True, help_text="Let the agent send the caller a WhatsApp message during the call.",
    )
    allow_callback_scheduling = models.BooleanField(default=True)
    record_calls = models.BooleanField(
        default=False,
        help_text="Ask the carrier to record. Recording a call without consent is illegal in many places.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Voice agent for tenant {self.tenant_id}"

    @classmethod
    def for_tenant(cls, tenant_id):
        """The tenant's config, or an unsaved default -- never a write on a read path."""
        config = cls.objects.filter(tenant_id=tenant_id).first()
        return config or cls(tenant_id=tenant_id)

    # The carrier credentials and the model key are each read far more often
    # than they are written, and never wanted in plaintext anywhere else.
    def get_account_sid(self) -> str | None:
        return decrypt_secret(self.account_sid_encrypted)

    def set_account_sid(self, raw: str | None) -> None:
        self.account_sid_encrypted = encrypt_secret(raw) if raw else ""

    def get_auth_token(self) -> str | None:
        return decrypt_secret(self.auth_token_encrypted)

    def set_auth_token(self, raw: str | None) -> None:
        self.auth_token_encrypted = encrypt_secret(raw) if raw else ""

    def get_api_key(self) -> str | None:
        return decrypt_secret(self.api_key_encrypted)

    def set_api_key(self, raw: str | None) -> None:
        self.api_key_encrypted = encrypt_secret(raw) if raw else ""

    @property
    def has_carrier_credentials(self) -> bool:
        return bool(self.get_account_sid() and self.get_auth_token() and self.from_number)


class CallSession(models.Model):
    """
    One phone call, from the moment it is asked for to the moment it is over.

    This row is created *before* the carrier is told to dial, because the
    webhook URL the carrier will call back on has to exist first. That means a
    session in QUEUED with no `provider_call_id` is a call that was never
    placed, which is exactly what the reconciliation task looks for.
    """

    class Direction(models.TextChoices):
        OUTBOUND = "OUTBOUND", "Outbound"
        INBOUND = "INBOUND", "Inbound"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RINGING = "RINGING", "Ringing"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETED = "COMPLETED", "Completed"
        NO_ANSWER = "NO_ANSWER", "No answer"
        BUSY = "BUSY", "Busy"
        FAILED = "FAILED", "Failed"
        CANCELED = "CANCELED", "Canceled"

    class Outcome(models.TextChoices):
        AGENT_ENDED = "AGENT_ENDED", "Agent ended the call"
        CALLER_ENDED = "CALLER_ENDED", "Caller hung up"
        TRANSFERRED = "TRANSFERRED", "Transferred to a person"
        VOICEMAIL = "VOICEMAIL", "Reached an answering machine"
        LIMIT_REACHED = "LIMIT_REACHED", "Hit a turn or time limit"
        NO_SPEECH = "NO_SPEECH", "Nobody spoke"
        ERROR = "ERROR", "Error"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="call_sessions")
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.SET_NULL, null=True, blank=True, related_name="call_sessions",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    direction = models.CharField(max_length=10, choices=Direction.choices, default=Direction.OUTBOUND)
    provider = models.CharField(max_length=20, default=VoiceAgentConfig.Provider.TWILIO)
    provider_call_id = models.CharField(max_length=100, blank=True, db_index=True)
    webhook_token = models.CharField(max_length=64, default=_webhook_token, unique=True)

    from_number = models.CharField(max_length=20, blank=True)
    to_number = models.CharField(max_length=20)

    status = models.CharField(max_length=15, choices=Status.choices, default=Status.QUEUED)
    outcome = models.CharField(max_length=15, choices=Outcome.choices, blank=True)

    # What this particular call is for. Overrides the config's default goal,
    # which is what makes one agent usable for a reminder call and a follow-up
    # call without reconfiguring it between them.
    goal = models.TextField(blank=True)
    # Facts handed to the agent for this call -- an order number, an amount
    # due. Rendered into the prompt, never guessed at.
    context = models.JSONField(default=dict, blank=True)
    # What the agent captured during the call, via the save_details tool.
    collected = models.JSONField(default=dict, blank=True)

    turns_used = models.PositiveIntegerField(default=0)
    silent_turns = models.PositiveIntegerField(default=0)
    recording_url = models.URLField(blank=True)
    summary = models.TextField(blank=True)
    error = models.CharField(max_length=500, blank=True)

    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "status"]),
        ]

    def __str__(self):
        return f"Call to {self.to_number} ({self.status})"

    @property
    def is_live(self) -> bool:
        return self.status in (self.Status.QUEUED, self.Status.RINGING, self.Status.IN_PROGRESS)


class CallTurn(models.Model):
    """
    One thing said, by either side, in order.

    The transcript is the only record of what an AI told a customer out loud,
    so it is written as the call happens rather than reconstructed at the end
    -- a call that drops mid-way must still leave behind everything that was
    said before it dropped.
    """

    class Role(models.TextChoices):
        AGENT = "AGENT", "Agent"
        CALLER = "CALLER", "Caller"
        SYSTEM = "SYSTEM", "System"
        TOOL = "TOOL", "Tool"

    session = models.ForeignKey(CallSession, on_delete=models.CASCADE, related_name="turns")
    ordinal = models.PositiveIntegerField(default=0)
    role = models.CharField(max_length=10, choices=Role.choices)
    text = models.TextField(blank=True)
    # The carrier's own confidence in what it heard. A low number is the usual
    # explanation for an answer that looks like a non-sequitur.
    confidence = models.FloatField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["session_id", "ordinal", "id"]
        indexes = [models.Index(fields=["session", "ordinal"])]

    def __str__(self):
        return f"{self.role}: {self.text[:40]}"
