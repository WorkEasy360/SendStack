import uuid

from django.conf import settings
from django.db import models

from apps.whatsapp.connections.session import decrypt_secret, encrypt_secret
from core.uploads import safe_extension


class AIAgentConfig(models.Model):
    """
    One AI agent per tenant, and everything about how far it is allowed to go.

    The defaults are deliberately timid. An agent that answers confidently
    from nothing is worse for a business than no agent at all -- it invents
    prices, promises refunds, and does it in the brand's own voice -- so out
    of the box it is off, it only speaks when no human owns the chat, it
    answers only from the knowledge the tenant gave it, and it hands over the
    moment it is unsure.
    """

    class Provider(models.TextChoices):
        # One entry per client module in `apps.ai.providers.registry`. Anthropic
        # is billed direct and takes an sk-ant- key; OpenAI is billed direct
        # and takes an sk- key, and is the one provider whose key, model and
        # endpoint may also come from the environment (OPENAI_API_KEY /
        # OPENAI_MODEL / OPENAI_BASE_URL); OpenRouter is one key in front of
        # many vendors' models and takes an sk-or- key, with model names
        # written as `vendor/model`; Custom is any OpenAI-compatible endpoint
        # the tenant names themselves via `base_url` below.
        ANTHROPIC = "ANTHROPIC", "Anthropic (Claude)"
        OPENAI = "OPENAI", "OpenAI"
        OPENROUTER = "OPENROUTER", "OpenRouter"
        CUSTOM = "CUSTOM", "Custom (OpenAI-compatible)"

    class ReplyScope(models.TextChoices):
        """
        Which incoming messages the agent is allowed to take.

        This is the single most consequential setting in the feature and the
        one users actually think about, so it is a named choice rather than
        two booleans they have to combine in their head.
        """

        EVERY = "EVERY", "Every incoming message"
        UNASSIGNED = "UNASSIGNED", "Only when no one is assigned"
        OUTSIDE_HOURS = "OUTSIDE_HOURS", "Only outside business hours"

    class Fallback(models.TextChoices):
        """What happens when the knowledge base cannot answer."""

        SAY_UNKNOWN = "SAY_UNKNOWN", "Say we do not have that information"
        ASK_SUPPORT = "ASK_SUPPORT", "Ask them to contact support"
        NOTIFY_TEAM = "NOTIFY_TEAM", "Notify a team member"
        HANDOFF = "HANDOFF", "Hand over to a person"

    tenant = models.OneToOneField("tenants.Tenant", on_delete=models.CASCADE, related_name="ai_agent")
    is_enabled = models.BooleanField(default=False)

    # Which base the agent answers from. Null means every active base the
    # tenant has, which is right for the common case of exactly one and is
    # what every pre-existing config means.
    knowledge_base = models.ForeignKey(
        "ai.KnowledgeBase", on_delete=models.SET_NULL, null=True, blank=True, related_name="agent_configs",
    )
    reply_scope = models.CharField(max_length=20, choices=ReplyScope.choices, default=ReplyScope.UNASSIGNED)
    fallback_action = models.CharField(max_length=20, choices=Fallback.choices, default=Fallback.HANDOFF)

    # Retrieval has to be *this* relevant before the model is called at all.
    # Below it there is nothing worth answering from, so the fallback runs
    # without spending a request -- see services.answer.
    min_relevance = models.FloatField(
        default=0.15, help_text="0-1. How closely the knowledge must match before the agent will answer.",
    )

    # Record what the customer asked about on their CRM timeline, and nudge
    # their lead score. Off by default: it writes to the customer record, and
    # that should be a decision rather than a surprise.
    crm_logging_enabled = models.BooleanField(default=True)
    crm_tagging_enabled = models.BooleanField(default=False)

    display_name = models.CharField(max_length=80, default="Assistant")
    provider = models.CharField(max_length=20, choices=Provider.choices, default=Provider.ANTHROPIC)
    model = models.CharField(max_length=100, default="claude-sonnet-5")
    # Only read when provider is Custom -- the OpenAI-compatible endpoint's
    # base URL, e.g. https://api.groq.com/openai/v1 or http://localhost:11434/v1.
    base_url = models.URLField(max_length=300, blank=True, default="")

    # The tenant's own key, encrypted at rest with the same Fernet key as the
    # WhatsApp provider credentials. Blank means "use the deployment's key",
    # which is how a single-tenant install avoids configuring this at all.
    api_key_encrypted = models.TextField(blank=True, default="")

    business_context = models.TextField(
        blank=True,
        help_text="Who the business is and how the agent should sound. Added to every request.",
    )
    temperature = models.FloatField(default=0.2)
    max_output_tokens = models.PositiveIntegerField(default=400)

    # How much of the thread the agent gets to read. Enough to follow a
    # conversation, bounded so a months-long thread cannot quietly turn every
    # reply into an expensive one.
    history_messages = models.PositiveIntegerField(default=10)
    knowledge_chunks = models.PositiveIntegerField(default=5)

    answer_from_knowledge_only = models.BooleanField(
        default=True,
        help_text="Refuse rather than guess when the answer is not in the knowledge base.",
    )
    # What a customer is told when the documents do not cover their question
    # and AI_KNOWLEDGE_ONLY is on. Editable because the right wording is the
    # tenant's own -- but the default is deliberately blunt: a refusal that
    # hedges ("I'm not sure, but it may be...") is the sentence a model
    # completes with an invention.
    knowledge_only_message = models.CharField(
        max_length=500,
        default="I couldn't find this information in the Knowledge Base.",
        help_text="Sent when knowledge-base-only answering is on and nothing relevant was found.",
    )
    reply_when_assigned = models.BooleanField(
        default=False,
        help_text="Also answer conversations an agent has already taken. Off means the agent stays out of their way.",
    )
    max_replies_per_conversation = models.PositiveIntegerField(
        default=10, help_text="0 for no limit. Reached, the conversation is handed to a person.",
    )

    handoff_enabled = models.BooleanField(default=True)
    handoff_message = models.TextField(
        blank=True,
        default="Let me get a colleague to help with that -- someone will be with you shortly.",
    )

    # What the agent may *do*, as opposed to say. Three switches rather than
    # one because they are three different sizes of mistake: a read tool that
    # goes wrong wastes a request, a write tool that goes wrong edits a
    # customer's record, and a send tool that goes wrong puts a message on
    # somebody's phone. Each is off until somebody turns it on, and `granted`
    # in apps.ai.tools is the single place that reads them.
    tools_enabled = models.BooleanField(
        default=False,
        help_text="Let the agent look things up and act, rather than only answering from what it was given.",
    )
    allow_write_tools = models.BooleanField(
        default=False,
        help_text="Let the agent update the customer's record -- save their details, add a label.",
    )
    allow_send_tools = models.BooleanField(
        default=False,
        help_text="Let the agent send an extra WhatsApp message on top of its reply.",
    )
    max_tool_rounds = models.PositiveSmallIntegerField(
        default=0,
        help_text="Model round-trips one reply may take while using tools. 0 for the platform default.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AI agent for tenant {self.tenant_id}"

    @classmethod
    def for_tenant(cls, tenant_id):
        """The tenant's config, or an unsaved default -- never a write on a read path."""
        config = cls.objects.filter(tenant_id=tenant_id).first()
        return config or cls(tenant_id=tenant_id)

    def get_api_key(self) -> str | None:
        return decrypt_secret(self.api_key_encrypted)

    def set_api_key(self, raw_key: str | None) -> None:
        self.api_key_encrypted = encrypt_secret(raw_key) if raw_key else ""


class KnowledgeBase(models.Model):
    """
    A named collection of documents the agent may answer from.

    One tenant can keep several: "ABC Electronics" for the everyday product
    and policy questions, "Summer Campaign" for a promotion that ends in
    September and must not be quoted in October. A retrieval always names
    exactly one base (or, for a tenant that never made a second, all of them),
    which is what makes "stop answering from the summer prices" a switch
    rather than a document hunt.

    Every tenant that has any documents at all has at least one base -- see
    `services.default_knowledge_base`, and the migration that moved the
    pre-existing flat pool into a "General" base.
    """

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="knowledge_bases")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="unique_knowledge_base_per_tenant")]
        ordering = ["-is_default", "name"]
        indexes = [models.Index(fields=["tenant", "is_active"])]

    def __str__(self):
        return self.name


def knowledge_document_upload_path(instance, filename):
    # A UUID stem so two uploads of "pricing.pdf" cannot collide; the name the
    # user recognises is kept separately in `original_filename`.
    #
    # The extension goes through core.uploads.safe_extension rather than a bare
    # splitext, because this builds a path under MEDIA_ROOT and the extension
    # is the one part of it an uploader still influences.
    return f"knowledge/tenant_{instance.tenant_id}/{uuid.uuid4().hex}{safe_extension(filename)}"


class KnowledgeDocument(models.Model):
    """
    One source the agent may answer from: a FAQ, a policy, a price list.

    Two shapes, one model. A document is either **typed** -- text the tenant
    wrote or pasted, which is how this started and is still the fastest way to
    record one fact -- or **uploaded**, a PDF/DOCX/TXT/CSV/XLSX whose text is
    extracted on the worker. Both end up as `content` plus chunks, so
    everything downstream (retrieval, the agent, the test panel) has one thing
    to deal with rather than two.

    Editing a document replaces its chunks rather than leaving orphans behind
    -- an out-of-date answer that has been "deleted" but is still being quoted
    back to customers is the failure mode worth designing against.
    """

    class Source(models.TextChoices):
        TEXT = "TEXT", "Typed in"
        FILE = "FILE", "Uploaded file"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Waiting to be read"
        PROCESSING = "PROCESSING", "Being read"
        READY = "READY", "Ready"
        FAILED = "FAILED", "Could not be read"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="knowledge_documents")
    knowledge_base = models.ForeignKey(
        KnowledgeBase, on_delete=models.CASCADE, null=True, blank=True, related_name="documents",
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    title = models.CharField(max_length=200)
    content = models.TextField(blank=True)
    source_url = models.URLField(blank=True)
    is_active = models.BooleanField(default=True)

    # --- uploaded documents -------------------------------------------------
    source_type = models.CharField(max_length=10, choices=Source.choices, default=Source.TEXT)
    file = models.FileField(upload_to=knowledge_document_upload_path, blank=True, null=True)
    original_filename = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=150, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    page_count = models.PositiveIntegerField(default=0)

    # Typed documents are READY the moment they are saved; an upload is not
    # readable until a worker has been through it. Kept on the row rather than
    # inferred from "has chunks" so a document that legitimately produced zero
    # chunks (an empty PDF) is distinguishable from one still in the queue.
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.READY)
    error = models.CharField(max_length=500, blank=True)
    chunk_count = models.PositiveIntegerField(default=0)
    indexed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title"]
        indexes = [
            models.Index(fields=["tenant", "is_active"]),
            models.Index(fields=["knowledge_base", "status"]),
        ]

    def __str__(self):
        return self.title

    @property
    def is_searchable(self) -> bool:
        return self.is_active and self.status == self.Status.READY


class KnowledgeChunk(models.Model):
    """
    A retrievable piece of a document.

    Documents are split because a whole price list handed to the model as
    context is mostly irrelevant to any one question, and paying to send it
    every time makes each answer both worse and dearer.

    `knowledge_base` is denormalised from the document on purpose: retrieval
    filters on it for every question, and reaching through the document table
    to do that turns the hottest read in the feature into a join.
    """

    document = models.ForeignKey(KnowledgeDocument, on_delete=models.CASCADE, related_name="chunks")
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="knowledge_chunks")
    knowledge_base = models.ForeignKey(
        KnowledgeBase, on_delete=models.CASCADE, null=True, blank=True, related_name="chunks",
    )
    ordinal = models.PositiveIntegerField(default=0)
    content = models.TextField()

    # Where in the source this came from, so an answer can say "Pricing.pdf,
    # page 4" instead of quoting text from nowhere. Both are best-effort:
    # a TXT file has no pages and a PDF often has no headings.
    page = models.PositiveIntegerField(null=True, blank=True)
    heading = models.CharField(max_length=200, blank=True)

    # When this piece entered the knowledge base. Chunks are replaced wholesale
    # on every edit, so this is the age of the *current* text rather than of
    # the document -- which is the useful one when the question is "was the
    # customer quoted the old price or the new one?".
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    class Meta:
        ordering = ["document_id", "ordinal"]
        indexes = [
            models.Index(fields=["tenant"]),
            models.Index(fields=["knowledge_base"]),
        ]

    def __str__(self):
        return f"{self.document_id}#{self.ordinal}"

    @property
    def citation(self) -> str:
        """How this piece is named when an answer is explained."""
        source = self.document.original_filename or self.document.title
        if self.page:
            return f"{source}, page {self.page}"
        if self.heading:
            return f"{source} — {self.heading}"
        return source


class KnowledgeEmbedding(models.Model):
    """
    One chunk's meaning, as a vector, for retrieval that does not depend on
    shared words.

    Why this is a table and not a column on KnowledgeChunk: a chunk exists the
    moment a document is saved, while its vector arrives later from a worker
    and may never arrive at all -- the endpoint can be down, the key can be
    wrong, the feature can be off. Keeping them apart means "chunked" and
    "embedded" are separately observable, and a missing vector degrades
    retrieval to lexical rather than making the chunk unreadable.

    `vector` is float32 packed little-endian and **normalised to unit length
    at write time**, which turns cosine similarity into a dot product and
    takes the square roots out of the hot loop. `model` is stored alongside
    because vectors from two different embedding models are not comparable at
    all: changing AI_EMBEDDING_MODEL has to invalidate what is here rather
    than silently mixing two coordinate systems and scoring nonsense.

    `tenant` and `knowledge_base` are denormalised from the chunk for the same
    reason KnowledgeChunk denormalises them from the document: this is filtered
    on for every question, and isolation that depends on a join is isolation
    somebody can forget to write.
    """

    chunk = models.OneToOneField(KnowledgeChunk, on_delete=models.CASCADE, related_name="embedding")
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="knowledge_embeddings")
    knowledge_base = models.ForeignKey(
        KnowledgeBase, on_delete=models.CASCADE, null=True, blank=True, related_name="embeddings",
    )

    model = models.CharField(max_length=100)
    dims = models.PositiveIntegerField(default=0)
    vector = models.BinaryField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "model"]),
            models.Index(fields=["knowledge_base", "model"]),
        ]

    def __str__(self):
        return f"embedding for chunk {self.chunk_id} ({self.model})"


class AIConversationState(models.Model):
    """
    What the agent has already done in one conversation.

    Separate from Conversation because it is the agent's own bookkeeping: a
    handoff must survive the conversation being reassigned, reopened, or read
    by somebody else, and none of those should be able to hand the customer
    back to the bot they already escaped.
    """

    conversation = models.OneToOneField("inbox.Conversation", on_delete=models.CASCADE, related_name="ai_state")
    replies_sent = models.PositiveIntegerField(default=0)
    handed_off_at = models.DateTimeField(null=True, blank=True)
    handoff_reason = models.CharField(max_length=200, blank=True)
    last_reply_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"AI state for conversation {self.conversation_id}"


class AIToolCall(models.Model):
    """
    One thing the agent did, as opposed to said.

    Kept apart from `AIUsageLog` for the same reason `KnowledgeAnswer` is:
    that table counts tokens and answers a billing question, this one records
    actions and answers "what did the assistant do to this customer's record,
    and on whose number". A table trying to be both would be indexed for
    neither, and the retention arguments are different too -- a token count
    stops mattering after the invoice, while an action taken on a customer's
    behalf is the thing somebody asks about three months later.

    Every owning identifier is stored, including the ones that are derivable
    by joining, because the question this table answers is usually asked from
    the wrong end: "which number sent that?" arrives with a phone in a
    screenshot, not a conversation id.
    """

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="ai_tool_calls")
    conversation = models.ForeignKey(
        "inbox.Conversation", on_delete=models.SET_NULL, null=True, blank=True, related_name="ai_tool_calls",
    )
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.SET_NULL, null=True, blank=True, related_name="ai_tool_calls",
    )
    whatsapp_account = models.ForeignKey(
        "whatsapp.WhatsAppAccount", on_delete=models.SET_NULL, null=True, blank=True, related_name="ai_tool_calls",
    )
    #: The provider-side device this ran against -- a QR bridge session or a
    #: Cloud API phone_number_id. Stored as text rather than a foreign key
    #: because it is whatever the provider said at the time, and a number
    #: re-paired next week must not rewrite what last week's row means.
    device_id = models.CharField(max_length=255, blank=True)

    name = models.CharField(max_length=100)
    #: What the model asked for, with anything secret-shaped taken out. Values
    #: are stringified and truncated on the way in -- see `tools._redact`.
    arguments = models.JSONField(default=dict, blank=True)
    #: What the model was told. The customer-safe sentence, not the exception.
    result = models.TextField(blank=True)
    is_error = models.BooleanField(default=False)
    #: The internal reason, for whoever is debugging. Never sent to the model
    #: and therefore never reachable by the customer.
    detail = models.CharField(max_length=500, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["conversation", "-created_at"]),
            models.Index(fields=["tenant", "name"]),
        ]

    def __str__(self):
        return f"{self.name} on conversation {self.conversation_id}"


class KnowledgeAnswer(models.Model):
    """
    One question the knowledge base was asked, and where the answer came from.

    Written for every retrieval, including the ones that found nothing and the
    ones run from the test panel. This is what makes "why did it tell a
    customer that?" answerable a week later -- without it the only record is
    the reply itself, which is exactly the thing under suspicion.

    Kept separate from AIUsageLog, which counts calls and tokens: that is a
    billing question, this is a correctness one, and mixing them would mean
    pruning one prunes the other.
    """

    class Confidence(models.TextChoices):
        """
        The user-facing form of a retrieval score.

        Three words instead of a number on purpose. "0.62" invites a business
        owner to tune a threshold they have no way to reason about; "Limited
        information" tells them what to do about it, which is add a document.
        """

        GOOD = "GOOD", "Good match"
        LIMITED = "LIMITED", "Limited information"
        NONE = "NONE", "No relevant information"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="knowledge_answers")
    knowledge_base = models.ForeignKey(
        KnowledgeBase, on_delete=models.SET_NULL, null=True, blank=True, related_name="answers",
    )
    conversation = models.ForeignKey(
        "inbox.Conversation", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    # The message the answer was sent as, where one was sent. Null for a test
    # from the admin panel and for anything that ended in a fallback.
    message = models.ForeignKey(
        "messaging.Message", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    question = models.TextField()
    answer = models.TextField(blank=True)
    confidence = models.CharField(max_length=10, choices=Confidence.choices, default=Confidence.NONE)
    relevance = models.FloatField(default=0.0)
    # [{document_id, title, citation, page}] -- denormalised so a source still
    # reads correctly after the document it came from has been deleted.
    sources = models.JSONField(default=list, blank=True)
    answered = models.BooleanField(default=False)
    fallback_used = models.CharField(max_length=20, blank=True)
    is_test = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["knowledge_base", "confidence"]),
        ]

    def __str__(self):
        return f"{self.confidence}: {self.question[:60]}"


class AIUsageLog(models.Model):
    """
    One row per call to the model.

    Written for every outcome, including the errors: "the agent went quiet
    last Tuesday" is otherwise unanswerable, and cost per tenant is not
    something to discover from an invoice.
    """

    class Outcome(models.TextChoices):
        ANSWERED = "ANSWERED", "Answered"
        HANDOFF = "HANDOFF", "Handed to a person"
        SUGGESTED = "SUGGESTED", "Suggested to an agent"
        SUMMARISED = "SUMMARISED", "Summarised"
        ERROR = "ERROR", "Error"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="ai_usage")
    conversation = models.ForeignKey(
        "inbox.Conversation", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    model = models.CharField(max_length=100)
    outcome = models.CharField(max_length=20, choices=Outcome.choices)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "-created_at"])]

    def __str__(self):
        return f"{self.outcome} ({self.model})"
