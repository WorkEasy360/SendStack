"""
The chatbot: what a business configured, what it is currently doing, and what
it did.

Four groups of table, and the split between them is the whole design:

* **Configuration** -- ``Chatbot``, ``ChatbotRule``, ``ChatbotWorkingHours``.
  What a person edits. Editable at any time, and editing it must never change
  what a live conversation is currently running.
* **The published record** -- ``ChatbotVersion``. A frozen copy of the
  configuration at the moment somebody pressed Publish. *This* is what answers
  customers, which is what makes editing safe and rollback possible.
* **Live state** -- ``ChatbotSession``. One per conversation: where the bot is,
  what it has learned, and whether it is still allowed to speak.
* **The trail** -- ``ChatbotEvent``. Every decision, in order, with timings.
  Written on the worker, read by the debug panel.

Nothing here duplicates an existing table. The customer is
``contacts.Contact``, the thread is ``inbox.Conversation``, the message is
``messaging.Message``, the documents are ``ai.KnowledgeDocument`` and the
number is ``whatsapp.WhatsAppAccount``. A chatbot is configuration hung off
those, not a parallel universe of them.
"""

from django.conf import settings
from django.db import models

from core.media import resolve_media_items, spec_from_object

#: Bound once, at module level, because ``ChatbotVersion`` has a field called
#: ``settings`` -- and a class attribute shadows a module global for the whole
#: of the class body, so ``settings.AUTH_USER_MODEL`` on a line *after* that
#: field would resolve to the JSONField. Naming the field something else would
#: be the other fix, but ``version.settings`` is what the thing is, and reading
#: well at every call site is worth one alias here.
AUTH_USER = settings.AUTH_USER_MODEL


class Chatbot(models.Model):
    """
    One automated conversation partner, on one WhatsApp number.

    Scoped to an account rather than to the tenant because that is the unit a
    business actually thinks in: a support number and a sales number want
    different personalities, different documents and different hours, and a
    single tenant-wide agent (which is what ``ai.AIAgentConfig`` is) cannot
    express that. The tenant-wide agent is not replaced -- it stays the
    provider credentials and the default behaviour, and a chatbot layers its
    own overrides on top. See ``services.effective_ai_config``.
    """

    class BotType(models.TextChoices):
        """
        How this bot decides what to say.

        HYBRID is the default and the recommendation, because the two pure
        forms each fail in a way the other covers: a rule-only bot is silent
        the moment somebody phrases a question differently to how it was
        written down, and an AI-only bot spends a model call answering
        "hi" and can never be made to say one exact sentence about refunds.
        """

        RULE_BASED = "RULE_BASED", "Rules only"
        AI_BASED = "AI_BASED", "AI only"
        HYBRID = "HYBRID", "Rules first, then AI (recommended)"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PUBLISHED = "PUBLISHED", "Published"
        PAUSED = "PAUSED", "Paused"
        ARCHIVED = "ARCHIVED", "Archived"

    class Language(models.TextChoices):
        """
        What the bot replies in.

        AUTO is the default because a business in India gets English, Hindi and
        Hinglish through one number on the same day, and answering all three in
        whichever the customer used is both what they expect and what the
        model does naturally when it is not told otherwise.
        """

        AUTO = "AUTO", "Match the customer"
        ENGLISH = "ENGLISH", "English"
        HINDI = "HINDI", "Hindi"
        HINGLISH = "HINGLISH", "Hinglish"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="chatbots")
    whatsapp_account = models.ForeignKey(
        "whatsapp.WhatsAppAccount", on_delete=models.CASCADE, related_name="chatbots",
        help_text="The number this bot answers on.",
    )
    created_by = models.ForeignKey(AUTH_USER, on_delete=models.SET_NULL, null=True, related_name="+")

    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    bot_type = models.CharField(max_length=20, choices=BotType.choices, default=BotType.HYBRID)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    # --- what it says -------------------------------------------------------
    welcome_message = models.TextField(
        blank=True, help_text="Sent on the contact's first message. Blank sends nothing.",
    )
    fallback_message = models.TextField(
        blank=True,
        default="Sorry, I didn't quite get that. Could you rephrase it, or type 'agent' to reach a person?",
        help_text="Sent when nothing matched and the AI could not answer.",
    )
    unsupported_message = models.TextField(
        blank=True,
        default="Sorry, I currently understand text messages best. Please type your question.",
        help_text="Sent when the customer sends something the bot cannot read (a sticker, a location).",
    )
    outside_hours_message = models.TextField(
        blank=True,
        default=(
            "Thanks for contacting us. Our team is currently offline. "
            "We will respond during business hours."
        ),
    )

    # --- how it behaves -----------------------------------------------------
    ai_enabled = models.BooleanField(default=True)
    knowledge_base = models.ForeignKey(
        "ai.KnowledgeBase", on_delete=models.SET_NULL, null=True, blank=True, related_name="chatbots",
        help_text="Blank uses the tenant's default base.",
    )
    system_prompt = models.TextField(
        blank=True,
        help_text="Who this bot is. Added to the platform's own safety instructions, never instead of them.",
    )
    persona = models.CharField(
        max_length=200, blank=True, help_text="A short note on tone, e.g. 'warm, brief, never pushy'.",
    )
    language = models.CharField(max_length=20, choices=Language.choices, default=Language.AUTO)

    max_ai_response_chars = models.PositiveIntegerField(
        default=600, help_text="Replies longer than this are trimmed at a sentence boundary before sending.",
    )
    history_messages = models.PositiveIntegerField(
        default=10, help_text="How many recent messages the AI reads. Bounded so a long thread stays affordable.",
    )
    min_relevance = models.FloatField(
        default=0.15, help_text="0-1. How closely the knowledge must match before the AI will answer at all.",
    )

    # Randomised inside this window, so replies do not all land at exactly the
    # same beat. Zero to zero sends immediately, which is what every automated
    # reply in this codebase did before chatbots existed.
    response_delay_min_seconds = models.PositiveIntegerField(default=0)
    response_delay_max_seconds = models.PositiveIntegerField(default=0)

    # --- handing over -------------------------------------------------------
    human_handoff_enabled = models.BooleanField(default=True)
    handoff_message = models.TextField(
        blank=True,
        default="Let me get a colleague to help with that - someone will be with you shortly.",
    )
    #: Which conditions hand over, as ``{condition_key: bool|int}``. Read
    #: through ``services.handoff_rules`` so a missing key means the default
    #: rather than an exception -- a config saved before a condition existed
    #: must keep working.
    handoff_conditions = models.JSONField(default=dict, blank=True)

    # --- when it works ------------------------------------------------------
    working_hours_enabled = models.BooleanField(
        default=False, help_text="Off means the bot answers at any hour.",
    )
    #: The bot's own hours live in ``ChatbotWorkingHours``, deliberately
    #: separate from ``teams.WorkingHours``. They answer different questions:
    #: the team's hours say when a *person* will reply, the bot's say when the
    #: *bot* will. A business that wants a bot precisely because nobody is at a
    #: desk at 2am needs those two to differ.
    conversation_timeout_minutes = models.PositiveIntegerField(
        default=1440,
        help_text="Silence after which the next message starts a fresh session. 0 never times out.",
    )

    # --- how it fits with what was already there ----------------------------
    defer_to_automation = models.BooleanField(
        default=False,
        help_text=(
            "Let existing automation rules and workflows answer first, and only take what they don't. "
            "Off means this bot answers instead of them."
        ),
    )

    #: The version currently answering customers. Null until the first
    #: publish, which is what makes a DRAFT bot inert rather than half-live.
    published_version = models.ForeignKey(
        "ChatbotVersion", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="unique_chatbot_name_per_tenant"),
            # Two published bots on one number would both answer every message.
            # A partial index rather than application code, because "only one
            # can be live" is the property the whole engine depends on and a
            # check in a service is a check somebody can route around.
            models.UniqueConstraint(
                fields=["whatsapp_account"],
                condition=models.Q(status="PUBLISHED"),
                name="one_published_chatbot_per_account",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            # The single hottest read in the feature: "which bot answers this
            # number?", asked once per inbound message.
            models.Index(fields=["whatsapp_account", "status"]),
        ]

    def __str__(self):
        return self.name

    @property
    def is_live(self) -> bool:
        return self.status == self.Status.PUBLISHED and self.published_version_id is not None


class ChatbotVersion(models.Model):
    """
    A frozen copy of a chatbot, taken at publish.

    Configuration is edited in the tables above; a conversation runs against a
    snapshot here. That separation is what lets somebody rewrite their pricing
    rule at lunchtime without the customer half-way through a flow suddenly
    finding themselves at a node that no longer exists -- and it is what makes
    rollback a row selection rather than an undo history nobody kept.

    The snapshot is JSON rather than a set of copied rows because it is only
    ever read whole, by the engine, and never queried by its parts. Storing it
    as rows would double every configuration table for no query that wants it.
    """

    chatbot = models.ForeignKey(Chatbot, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()

    #: ``{"nodes": [...]}`` -- the flow graph. See ``flow.validate``.
    flow = models.JSONField(default=dict, blank=True)
    #: The rules as they stood, already in priority order.
    rules = models.JSONField(default=list, blank=True)
    #: Everything on the Chatbot row that changes what it says or does.
    settings = models.JSONField(default=dict, blank=True)
    #: The working-hours rows, as ``[{weekday, is_open, opens_at, closes_at}]``.
    working_hours = models.JSONField(default=list, blank=True)

    notes = models.CharField(max_length=300, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(AUTH_USER, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["chatbot", "version"], name="unique_chatbot_version"),
        ]
        indexes = [models.Index(fields=["chatbot", "-version"])]

    def __str__(self):
        return f"{self.chatbot_id} v{self.version}"


class ChatbotRule(models.Model):
    """
    One "when the customer says X, do Y".

    Deliberately not ``automation.AutomationRule``. That model is one trigger
    and one message, scoped to an account and evaluated by the legacy ladder;
    this one is scoped to a *bot*, matches eight ways including regex, and
    carries a *list* of actions. Widening the old model would have changed the
    meaning of every existing row and every existing test; a new table leaves
    the old feature exactly as it was for the tenants using it.
    """

    class MatchType(models.TextChoices):
        EXACT = "EXACT", "Exactly"
        CONTAINS = "CONTAINS", "Contains"
        STARTS_WITH = "STARTS_WITH", "Starts with"
        ENDS_WITH = "ENDS_WITH", "Ends with"
        REGEX = "REGEX", "Matches a pattern"
        KEYWORD = "KEYWORD", "Any of these words"
        BUTTON = "BUTTON", "A button was tapped"
        ANY = "ANY", "Any message"

    chatbot = models.ForeignKey(Chatbot, on_delete=models.CASCADE, related_name="rules")
    name = models.CharField(max_length=150)

    match_type = models.CharField(max_length=20, choices=MatchType.choices, default=MatchType.CONTAINS)
    #: What to match. A comma-separated list for KEYWORD, a button id for
    #: BUTTON, a regular expression for REGEX, ignored for ANY.
    pattern = models.CharField(max_length=500, blank=True)
    case_sensitive = models.BooleanField(default=False)

    #: Lower numbers run first, so "1" reads as "first" the way people write
    #: priorities down. A catch-all sits at 100.
    priority = models.IntegerField(
        default=10, help_text="Lower runs first. Put your catch-all rule on a high number.",
    )
    #: ``[{"type": "SEND_MESSAGE", ...}, ...]`` -- see ``actions.validate``.
    actions = models.JSONField(default=list, blank=True)

    cooldown_seconds = models.PositiveIntegerField(
        default=0, help_text="Shortest gap before this rule answers the same contact again. 0 = every time.",
    )
    is_active = models.BooleanField(default=True)

    #: Running totals, for the analytics screen. Incremented with an F()
    #: expression so two workers matching the same rule cannot lose a count.
    match_count = models.PositiveIntegerField(default=0)
    last_matched_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "id"]
        indexes = [models.Index(fields=["chatbot", "is_active", "priority"])]

    def __str__(self):
        return self.name


class ChatbotWorkingHours(models.Model):
    """One row per weekday per bot. A missing row means closed all day."""

    class Weekday(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    chatbot = models.ForeignKey(Chatbot, on_delete=models.CASCADE, related_name="working_hours")
    weekday = models.IntegerField(choices=Weekday.choices)
    is_open = models.BooleanField(default=True)
    opens_at = models.TimeField(default="09:00")
    closes_at = models.TimeField(default="18:00")

    class Meta:
        ordering = ["weekday"]
        constraints = [
            models.UniqueConstraint(fields=["chatbot", "weekday"], name="unique_chatbot_hours_per_weekday"),
        ]

    def __str__(self):
        return f"{self.get_weekday_display()} {self.opens_at}-{self.closes_at}"


class ChatbotSession(models.Model):
    """
    What the bot is doing in one conversation, right now.

    One per conversation, not one per (conversation, bot): a customer is in
    one conversation with one business, and two bots both holding state on the
    same thread is the bug this shape prevents. Transferring between bots
    rewrites ``chatbot`` on the same row.

    Kept apart from ``inbox.Conversation`` for the reason
    ``ai.AIConversationState`` is: this is the bot's own bookkeeping, and a
    handoff has to survive the conversation being reassigned, closed and
    reopened. Extending Conversation instead would have put bot columns on the
    table every inbox query reads.
    """

    class State(models.TextChoices):
        NEW = "NEW", "New"
        BOT_ACTIVE = "BOT_ACTIVE", "Bot handling"
        WAITING_FOR_CUSTOMER = "WAITING_FOR_CUSTOMER", "Waiting for the customer"
        WAITING_FOR_AGENT = "WAITING_FOR_AGENT", "Waiting for an agent"
        HUMAN_ACTIVE = "HUMAN_ACTIVE", "A person is handling this"
        RESOLVED = "RESOLVED", "Resolved"
        CLOSED = "CLOSED", "Closed"

    #: The states in which the bot is allowed to speak. Everything else is
    #: either a person's conversation or a finished one. RESOLVED and CLOSED
    #: are absent on purpose: a new message on a finished conversation
    #: *reopens* it (see ``engine``), which moves the state before this is
    #: consulted, rather than being answered inside a state that says the
    #: conversation is over.
    BOT_MAY_SPEAK = frozenset({State.NEW, State.BOT_ACTIVE, State.WAITING_FOR_CUSTOMER})

    class Resolution(models.TextChoices):
        UNRESOLVED = "UNRESOLVED", "Unresolved"
        BOT = "BOT", "Resolved by the bot"
        HUMAN = "HUMAN", "Resolved by a person"
        ABANDONED = "ABANDONED", "Abandoned"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="chatbot_sessions")
    conversation = models.OneToOneField(
        "inbox.Conversation", on_delete=models.CASCADE, related_name="chatbot_session",
    )
    chatbot = models.ForeignKey(
        Chatbot, on_delete=models.SET_NULL, null=True, blank=True, related_name="sessions",
    )
    #: The snapshot this session runs against, pinned when the session starts.
    #: A publish mid-conversation therefore does not move the customer's feet.
    version = models.ForeignKey(
        ChatbotVersion, on_delete=models.SET_NULL, null=True, blank=True, related_name="sessions",
    )

    state = models.CharField(max_length=25, choices=State.choices, default=State.NEW)
    resolution = models.CharField(max_length=15, choices=Resolution.choices, default=Resolution.UNRESOLVED)

    #: Where the customer is in the flow. Blank means not in one.
    current_node_id = models.CharField(max_length=64, blank=True)
    #: The QUESTION node whose answer we are waiting for, if any. Kept apart
    #: from ``current_node_id`` so "parked on a question" is distinguishable
    #: from "half-way through executing a chain".
    awaiting_node_id = models.CharField(max_length=64, blank=True)

    #: Everything the bot has learned in this conversation, ``{name: value}``.
    #: Session-scoped on purpose: a value the customer gave once ("size 9")
    #: belongs to this conversation, while anything that should outlive it is
    #: written to ``Contact.custom_fields`` by the same action. See
    #: ``variables.set_variable``.
    variables = models.JSONField(default=dict, blank=True)

    #: A few lines standing in for everything older than the history window.
    #: Written by the worker after a thread passes ``SUMMARY_AFTER_MESSAGES``,
    #: and prepended to the prompt instead of the messages it replaces -- which
    #: is what keeps a three-hundred-message thread from turning every reply
    #: into an expensive one.
    summary = models.TextField(blank=True)
    summary_message_count = models.PositiveIntegerField(default=0)

    #: Consecutive replies the bot could not answer. This, not a total, is
    #: what the "hand over after N failures" condition counts -- two failures
    #: separated by a good answer is a working bot.
    consecutive_fallbacks = models.PositiveIntegerField(default=0)
    ai_replies = models.PositiveIntegerField(default=0)
    bot_replies = models.PositiveIntegerField(default=0)
    messages_received = models.PositiveIntegerField(default=0)

    #: The language the customer is actually writing in, as detected. Stored
    #: so an answer stays in one language across a thread even when one
    #: message is too short to tell.
    detected_language = models.CharField(max_length=20, blank=True)

    handed_off_at = models.DateTimeField(null=True, blank=True)
    handoff_reason = models.CharField(max_length=200, blank=True)
    #: Set when a person hands the conversation back. Kept so "was this ever
    #: with a human?" survives a return to the bot.
    returned_to_bot_at = models.DateTimeField(null=True, blank=True)

    #: Invalidates delayed sends. Every scheduled reply carries the value this
    #: held when it was scheduled; a takeover, a close, or a newer inbound
    #: message bumps it, and the delayed task then declines to send. That is
    #: how "cancel the pending reply" works without a cancellable broker.
    delivery_token = models.PositiveIntegerField(default=0)

    #: The newest inbound message this session has already answered. A
    #: high-water mark rather than a set, because message ids increase: "less
    #: than or equal to this" catches a redelivered Celery task *and* a message
    #: overtaken by a newer one from the same customer. Written under the row
    #: lock in ``engine._claim``, which is what makes one inbound message
    #: produce exactly one reply however many times the task is delivered.
    last_processed_message_id = models.BigIntegerField(default=0)

    started_at = models.DateTimeField(auto_now_add=True)
    last_customer_message_at = models.DateTimeField(null=True, blank=True)
    last_bot_message_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "state"]),
            models.Index(fields=["chatbot", "state"]),
            models.Index(fields=["tenant", "-updated_at"]),
        ]

    def __str__(self):
        return f"Session on conversation {self.conversation_id} ({self.state})"

    @property
    def bot_may_speak(self) -> bool:
        return self.state in self.BOT_MAY_SPEAK and self.handed_off_at is None


class ChatbotEvent(models.Model):
    """
    One step of one message's journey, with a timestamp and a reason.

    This is the debug panel's whole data source, and the reason it exists is
    that "the bot did not reply" has about fifteen possible causes -- the
    number was disconnected, the state was HUMAN_ACTIVE, retrieval scored
    below the floor, the rule matched but its cooldown was live, the model
    errored -- and none of them are visible from the thread. The alternative
    to a table is reading worker logs, which a customer cannot do.

    Append-only, and pruned by ``tasks.prune_chatbot_events``: this is
    per-message and therefore the fastest-growing table in the feature.
    """

    class Step(models.TextChoices):
        RECEIVED = "RECEIVED", "Message received"
        NORMALIZED = "NORMALIZED", "Normalised"
        SESSION = "SESSION", "Session resolved"
        DEDUPE = "DEDUPE", "Duplicate check"
        STATE = "STATE", "State check"
        HOURS = "HOURS", "Business hours"
        LANGUAGE = "LANGUAGE", "Language detected"
        FLOW = "FLOW", "Flow step"
        RULE = "RULE", "Rule evaluation"
        AI_REQUEST = "AI_REQUEST", "AI request"
        RETRIEVAL = "RETRIEVAL", "Knowledge retrieval"
        TOOL = "TOOL", "Tool call"
        ACTION = "ACTION", "Action"
        RESPONSE = "RESPONSE", "Response generated"
        SEND = "SEND", "Handed to the provider"
        DELIVERY = "DELIVERY", "Delivery status"
        HANDOFF = "HANDOFF", "Handed to a person"
        FALLBACK = "FALLBACK", "Fallback"
        ERROR = "ERROR", "Error"

    class Outcome(models.TextChoices):
        OK = "OK", "OK"
        SKIPPED = "SKIPPED", "Skipped"
        ERROR = "ERROR", "Error"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="chatbot_events")
    conversation = models.ForeignKey(
        "inbox.Conversation", on_delete=models.CASCADE, null=True, blank=True, related_name="chatbot_events",
    )
    chatbot = models.ForeignKey(Chatbot, on_delete=models.SET_NULL, null=True, blank=True, related_name="events")
    message = models.ForeignKey(
        "messaging.Message", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    #: Ties every row for one inbound message together, and matches the
    #: ``correlation_id`` written into the structured log line for the same
    #: step -- so a row here can be found in the logs and vice versa.
    correlation_id = models.CharField(max_length=64, db_index=True)

    step = models.CharField(max_length=20, choices=Step.choices)
    outcome = models.CharField(max_length=10, choices=Outcome.choices, default=Outcome.OK)
    #: One sentence, safe to show a customer's own administrator. Never an
    #: exception string and never anything secret -- see ``trace.record``.
    detail = models.CharField(max_length=500, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["conversation", "created_at"]),
            models.Index(fields=["chatbot", "step", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.step}:{self.outcome} ({self.correlation_id})"


class ChatbotHandoff(models.Model):
    """
    One time a conversation left the bot for a person, and what happened next.

    A row rather than a pair of timestamps on the session, because a
    conversation can go bot -> human -> bot -> human across a day and the
    analytics question ("how often does the bot give up, and on what?") is
    about the individual events, not the latest one.
    """

    class Trigger(models.TextChoices):
        CUSTOMER_REQUEST = "CUSTOMER_REQUEST", "The customer asked for a person"
        REPEATED_FALLBACK = "REPEATED_FALLBACK", "The bot could not answer repeatedly"
        LOW_CONFIDENCE = "LOW_CONFIDENCE", "The AI was not confident"
        NEGATIVE_SENTIMENT = "NEGATIVE_SENTIMENT", "The customer was unhappy"
        KEYWORD = "KEYWORD", "A handoff keyword"
        RULE_ACTION = "RULE_ACTION", "A rule or flow node asked for it"
        TOOL_FAILURE = "TOOL_FAILURE", "A tool failed"
        AI_UNAVAILABLE = "AI_UNAVAILABLE", "The AI was unavailable"
        AGENT_TAKEOVER = "AGENT_TAKEOVER", "An agent took over"
        REPLY_LIMIT = "REPLY_LIMIT", "The bot hit its reply limit"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="chatbot_handoffs")
    conversation = models.ForeignKey("inbox.Conversation", on_delete=models.CASCADE, related_name="chatbot_handoffs")
    chatbot = models.ForeignKey(Chatbot, on_delete=models.SET_NULL, null=True, blank=True, related_name="handoffs")
    session = models.ForeignKey(
        ChatbotSession, on_delete=models.SET_NULL, null=True, blank=True, related_name="handoffs",
    )

    trigger = models.CharField(max_length=25, choices=Trigger.choices)
    detail = models.CharField(max_length=300, blank=True)
    assigned_to = models.ForeignKey(
        AUTH_USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    #: When a person handed it back. Null while the human still owns it.
    returned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["chatbot", "trigger"]),
        ]

    def __str__(self):
        return f"{self.trigger} on conversation {self.conversation_id}"


class ChatbotMediaAsset(models.Model):
    """
    A file the bot can send by name -- "the brochure", "the price list".

    A thin pointer at the existing media library rather than a second upload
    system: the URL is whatever the library gave, and this row is only the
    *name* a rule or flow node refers to it by. Without it a rule has to carry
    a raw URL, which breaks silently the day the file is replaced.
    """

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="chatbot_assets")
    chatbot = models.ForeignKey(
        Chatbot, on_delete=models.CASCADE, null=True, blank=True, related_name="assets",
        help_text="Blank makes this asset available to every bot in the workspace.",
    )
    key = models.SlugField(max_length=60, help_text="What a rule refers to this by, e.g. 'brochure'.")
    label = models.CharField(max_length=150)
    media_type = models.CharField(max_length=20, default="DOCUMENT")
    media_url = models.URLField(max_length=500)
    filename = models.CharField(max_length=255, blank=True)
    caption = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "chatbot", "key"], name="unique_chatbot_asset_key"),
        ]
        indexes = [models.Index(fields=["tenant", "key"])]

    def __str__(self):
        return f"{self.key} ({self.label})"

    def as_message_spec(self) -> dict:
        """The shape ``automation.send_automated_message`` expects."""
        spec = {
            "message_type": self.media_type,
            "body": "",
            "media_url": self.media_url,
            "caption": self.caption,
            "filename": self.filename,
            "variables": {},
            "buttons": [],
            "template_id": None,
        }
        spec["media_items"] = resolve_media_items(spec_from_object(self))
        return spec
