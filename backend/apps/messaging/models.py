from django.conf import settings
from django.db import models


class Message(models.Model):
    """A single WhatsApp message to/from one contact -- campaign send, inbox reply, or inbound."""

    class MessageType(models.TextChoices):
        TEXT = "TEXT", "Text"
        IMAGE = "IMAGE", "Image"
        VIDEO = "VIDEO", "Video"
        DOCUMENT = "DOCUMENT", "Document"

    class Direction(models.TextChoices):
        OUTBOUND = "OUTBOUND", "Outbound"
        INBOUND = "INBOUND", "Inbound"

    class Kind(models.TextChoices):
        """
        What kind of contact this message is, which decides what may stop it.

        The compliance gate treats the three very differently, so getting this
        right on a new send path matters more than it looks:

        * MARKETING -- a campaign, a follow-up nudge, a scheduled workflow
          step. Nobody asked for it at this moment, so it is blocked for an
          unsubscribed contact and held outside the tenant's quiet hours.
        * SERVICE -- a reply to something the contact just did: an agent
          answering in the inbox, a welcome or keyword auto-reply. Still
          blocked for an unsubscribed contact, but never held for quiet
          hours, because holding a reply until morning is worse for the
          person waiting on it than sending it late at night.
        * NOTICE -- the confirmation of an opt-out or opt-in. Exempt from
          both: it goes to someone we may otherwise no longer message, and
          it is owed to them when they asked rather than after the next
          quiet window.

        Defaults to MARKETING, so a send path added later is protected by
        default and has to opt down rather than remember to opt in.
        """

        MARKETING = "MARKETING", "Marketing"
        SERVICE = "SERVICE", "Service"
        NOTICE = "NOTICE", "Compliance notice"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        SENDING = "SENDING", "Sending"
        SENT = "SENT", "Sent"
        DELIVERED = "DELIVERED", "Delivered"
        READ = "READ", "Read"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"
        # Terminal, like FAILED, but means the send was never attempted --
        # no connected WhatsApp account was available for this recipient.
        # Kept distinct so a campaign report can tell "the provider refused
        # this" apart from "we had nothing to send it from".
        SKIPPED = "SKIPPED", "Skipped"
        # The worker that was sending this message died (OOM, container
        # restart, deploy) inside the ambiguous window between handing the
        # message to the provider and recording the result -- so we cannot
        # prove whether WhatsApp received it. NOT a failure, and never
        # auto-retried: a blind retry here is a duplicate send. The recovery
        # sweep and the operator resolve endpoint move it forward from here.
        # See apps/messaging/recovery.py.
        RECOVERY_REQUIRED = "RECOVERY_REQUIRED", "Recovery required"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="messages")
    campaign = models.ForeignKey(
        "campaigns.Campaign", on_delete=models.CASCADE, null=True, blank=True, related_name="messages"
    )
    # The MessageTemplate this specific send was built from, if any -- set
    # regardless of path (campaign, automation rule, workflow MESSAGE node).
    # This is what lets a FollowUpRule scope itself to "only after Template X"
    # (see automation.models.FollowUpRule.trigger_templates): without it,
    # every follow-up rule on an account had to fire after every outbound
    # message, template or not.
    template = models.ForeignKey(
        "templates_app.MessageTemplate", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    conversation = models.ForeignKey(
        "inbox.Conversation", on_delete=models.CASCADE, null=True, blank=True, related_name="messages"
    )
    whatsapp_account = models.ForeignKey("whatsapp.WhatsAppAccount", on_delete=models.PROTECT, related_name="messages")
    contact = models.ForeignKey("contacts.Contact", on_delete=models.SET_NULL, null=True, blank=True, related_name="messages")

    # Who this send is billed to (campaign owner, or the agent who sent a reply). Never set for inbound messages.
    billed_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    direction = models.CharField(max_length=10, choices=Direction.choices, default=Direction.OUTBOUND)
    # True for welcome/keyword-reply/workflow/follow-up sends -- these never schedule further follow-up
    # checks themselves, so a chain of automated messages can't trigger an unbounded chain of follow-ups.
    is_automated = models.BooleanField(default=False)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.MARKETING)
    to_phone = models.CharField(max_length=30)
    message_type = models.CharField(max_length=20, choices=MessageType.choices, default=MessageType.TEXT)
    body = models.TextField(blank=True)
    media_url = models.URLField(blank=True)
    caption = models.TextField(blank=True)
    filename = models.CharField(max_length=255, blank=True)
    # Every attachment on this message, in send order. The three columns
    # above stay authoritative for the *first* one so existing readers
    # (reports, exports, the provider call) need no changes; see
    # core.media for the item shape and the single-attachment fallback.
    media_items = models.JSONField(default=list, blank=True)

    # Interactive reply/link buttons shown under this message. Empty means a
    # plain (non-interactive) send, which is what every row written before
    # buttons existed looks like; see core.buttons for the item shape.
    buttons = models.JSONField(default=list, blank=True)

    # The menu or the poll this message asks, when it asks one. Mutually
    # exclusive with each other and with `buttons`; empty is what every row
    # written before they existed looks like. Stored on the message and not
    # only on the template because the row is the record of what this contact
    # was actually offered -- a template edited afterwards must not change
    # what a sent message says it asked. See core.interactive.
    list_content = models.JSONField(default=dict, blank=True)
    poll_content = models.JSONField(default=dict, blank=True)

    # Set on an INBOUND message that is a contact tapping one of our reply
    # buttons: the id of the button tapped (`body` holds the label they saw).
    # Blank for everything else, including inbound plain text. Automation
    # rules and workflow conditions match on this -- see
    # apps.automation.services.trigger_button_rules.
    replied_button_id = models.CharField(max_length=256, blank=True)

    class Interaction(models.TextChoices):
        """
        *How* a contact chose the option in `replied_button_id`.

        A choice is a choice however it arrived, so everything downstream keeps
        matching on `replied_button_id` alone. This exists because the two
        arrivals are not equally informative about the *connection*: a run of
        button messages with no native taps means the buttons are not reaching
        anybody's phone, and a run with no taps but plenty of numbered replies
        means they are reaching people who simply cannot tap them. Collapsed
        into one counter those read identically, and the first one is an
        outage while the second is working as designed.
        """

        NATIVE_BUTTON = "NATIVE_BUTTON", "Tapped a button"
        NUMBERED_FALLBACK = "NUMBERED_FALLBACK", "Replied with the option number"
        TEXT_OPTION = "TEXT_OPTION", "Replied with the option's wording"

    #: Blank on every message that is not a contact choosing an option --
    #: including every row written before this column existed, which is why it
    #: is blank rather than defaulted to NATIVE_BUTTON.
    interaction_type = models.CharField(max_length=20, choices=Interaction.choices, blank=True, default="")

    class FailureReason(models.TextChoices):
        """
        *Why* a message did not go out, as a code rather than a sentence.

        `error_message` has always carried the explanation, and it is the right
        thing to show a person -- it is written for them, it names the number
        and the plan, and it changes when the wording improves. It is the wrong
        thing to count. "How many of last month's failures were a disconnected
        device?" was, before this, a LIKE against prose that four different
        code paths compose differently, and the answer silently changed the
        next time somebody improved a sentence.

        So the sentence stays and a stable code sits beside it. The code is what
        reports group by and what an operator alerts on; the sentence is what
        the customer reads.

        Blank on every row written before this existed, and on every message
        that did not fail -- deliberately not defaulted to anything, because a
        guessed reason on a historical row is worse than no reason at all.
        """

        # Refused by the compliance gate. Two codes rather than one: an
        # opt-out is the contact's own decision and a missing consent record is
        # the workspace's configuration, and the fix is different for each.
        OPTED_OUT = "opted_out", "Contact opted out"
        CONSENT_MISSING = "consent_missing", "No marketing consent on file"

        # Refused by billing/entitlements.
        QUOTA_EXCEEDED = "quota_exceeded", "Plan message allowance exhausted"
        SUBSCRIPTION_EXPIRED = "subscription_expired", "Subscription not active"
        INSUFFICIENT_CREDITS = "insufficient_credits", "Not enough credits"
        TENANT_SUSPENDED = "tenant_suspended", "Workspace suspended"

        # Refused because of the sending number.
        DEVICE_DISCONNECTED = "device_disconnected", "No connected device available"
        DEVICE_PAUSED = "device_paused", "Sending from this number is paused"
        RATE_LIMITED = "rate_limited", "Sending limit reached"

        # Held so long that sending became meaningless.
        HELD_TOO_LONG = "held_too_long", "Waited past the maximum hold time"

        # The provider was reached and said no, or could not be reached at all.
        INVALID_RECIPIENT = "invalid_recipient", "Provider rejected the recipient"
        PROVIDER_UNAVAILABLE = "provider_unavailable", "Provider could not be reached"
        PROVIDER_ERROR = "provider_error", "Provider returned an error"
        # Reached, never answered. Not retried, because the message may already
        # be on the recipient's phone -- see apps.messaging.tasks.
        DELIVERY_UNCONFIRMED = "delivery_unconfirmed", "Delivery could not be confirmed"

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)

    # --- The send lease -------------------------------------------------------
    #
    # A message in SENDING is owned, for a bounded time, by exactly one worker.
    # The lease is what makes worker death survivable: a SENDING row whose
    # `lease_expires_at` is in the past has no live owner, and the recovery
    # sweep (apps/messaging/recovery.py) is free to reconcile it. This is the
    # same mechanism apps/scheduling uses for ScheduledJob, one level down.
    #
    # Kept on Message (not only on MessageSendAttempt) so the sweep's hot query
    # -- "SENDING and lease_expires_at < now" -- is a single indexed scan.
    # `lease_owner` is written for the operator reading a stuck row and read by
    # the code exactly once: as a guard, so a worker whose lease has been
    # reclaimed cannot overwrite the newer state (see attempt_send / the sweep).
    lease_owner = models.CharField(max_length=120, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    lease_heartbeat_at = models.DateTimeField(null=True, blank=True)

    provider_message_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    error_message = models.CharField(max_length=500, blank=True)
    #: The machine-readable half of `error_message`. See FailureReason.
    failure_reason = models.CharField(
        max_length=32, choices=FailureReason.choices, blank=True, default="",
    )
    retry_count = models.PositiveIntegerField(default=0)

    # The randomly generated delay, in seconds, that the campaign dispatcher
    # waited before handing this message to the provider. One independently
    # drawn value per message; NULL for messages that were never dispatched
    # by a campaign (inbox replies, automation sends, inbound messages).
    assigned_delay_seconds = models.FloatField(null=True, blank=True)

    # When this message took one off the plan's monthly allowance, or NULL if
    # it never did. Set once, under the tenant's row lock, at the moment the
    # send gate authorised it -- see
    # apps.subscriptions.services.claim_message_allowance for why a stamp on
    # the row is the only version of this that is safe against concurrent
    # workers, and cleared again wherever the credit reservation is released.
    plan_counted_at = models.DateTimeField(null=True, blank=True)

    queued_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-queued_at"]
        indexes = [
            models.Index(fields=["campaign", "status"]),
            models.Index(fields=["conversation", "queued_at"]),
            models.Index(fields=["whatsapp_account", "provider_message_id"]),
            models.Index(fields=["tenant", "status"]),
            # Dashboard/report queries filter by tenant + a queued_at date range
            # (see reports/services.py _message_queryset) -- without this, that
            # scan has no index to use once the message table gets large.
            models.Index(fields=["tenant", "queued_at"]),
            # campaign_report()'s reply-count runs a correlated EXISTS subquery
            # per outbound message, matched on exactly these four columns
            # (whatsapp_account, contact, direction, queued_at > sent_at).
            # Measured without this index: ~585ms for one campaign report on a
            # 6,000-row Message table -- 60-100x every other endpoint measured.
            models.Index(fields=["whatsapp_account", "contact", "direction", "queued_at"]),
            # The monthly allowance count: tenant + a plan_counted_at range,
            # run once per send under a lock, so it is the one query in this
            # table that other sends actually wait behind.
            models.Index(fields=["tenant", "plan_counted_at"]),
            # "Why did this workspace's messages fail this month" -- the
            # question the failure codes exist to answer, and a scan of the
            # tenant's whole message history without this.
            models.Index(fields=["tenant", "failure_reason"]),
            # The recovery sweep's only query: SENDING rows whose lease has
            # expired, oldest first. Without this it is a full scan of every
            # in-flight message every 60 seconds.
            models.Index(fields=["status", "lease_expires_at"]),
        ]

    def __str__(self):
        return f"Message #{self.pk} to {self.to_phone} ({self.status})"


class MessageSendAttempt(models.Model):
    """
    One physical attempt to hand one Message to a provider.

    The Message row records *where the message is*; this records *what happened
    on each try to move it*, which is the information a crash destroys and the
    information recovery needs. A worker that dies mid-send leaves an attempt
    row that says how far it got -- claimed but never submitted (safe to
    retry), submission started but no id (unknown), or a provider id in hand
    (submitted, wait for webhooks).

    `idempotency_key` is deterministic -- ``tenant:campaign:message:attempt`` --
    never random, so the same logical send keeps its identity across retries
    (the attempt number is the only part that moves). It is *also* the wire
    correlation id: it is sent to the QR bridge as ``send_operation_id`` and to
    the Cloud API as ``biz_opaque_callback_data``, so a delivery webhook can be
    tied back to this attempt even when the provider's message id never reached
    us.

    `state_version` is an optimistic-concurrency guard: every transition is a
    conditional ``UPDATE ... WHERE state_version = n``, so a stale worker whose
    lease was reclaimed while it was slow cannot clobber the recovery worker's
    newer decision.
    """

    class Status(models.TextChoices):
        CLAIMED = "CLAIMED", "Claimed"            # lease taken, nothing sent yet
        SENDING = "SENDING", "Sending"            # provider call in progress
        SUBMITTED = "SUBMITTED", "Submitted"      # provider accepted; awaiting delivery lifecycle
        CONFIRMED = "CONFIRMED", "Confirmed"      # message reached SENT or beyond
        FAILED = "FAILED", "Failed"              # provider definitively refused
        UNKNOWN = "UNKNOWN", "Unknown"            # outcome cannot be determined
        EXPIRED = "EXPIRED", "Expired"            # this attempt's lease lapsed; superseded by recovery

    #: Attempt states from which nothing more will happen on this row.
    TERMINAL_STATUSES = (Status.CONFIRMED, Status.FAILED, Status.EXPIRED)

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="send_attempts")
    # Denormalised from the message for isolation and for the sweep's filters:
    # a recovery query scoped by a join is a query somebody can forget to scope.
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="message_send_attempts")
    campaign_id = models.PositiveIntegerField(null=True, blank=True)

    attempt_number = models.PositiveIntegerField()

    #: hostname:pid of the worker that claimed this attempt. For the operator,
    #: and for the stale-worker guard -- never for routing.
    worker_id = models.CharField(max_length=120, blank=True, default="")
    #: The Celery task id that ran this attempt, for tracing across the queue.
    task_id = models.CharField(max_length=255, blank=True, default="")

    account_id = models.PositiveIntegerField(null=True, blank=True)
    provider = models.CharField(max_length=64, blank=True, default="")

    idempotency_key = models.CharField(max_length=255, unique=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CLAIMED)
    state_version = models.PositiveIntegerField(default=0)

    started_at = models.DateTimeField()
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    submission_started_at = models.DateTimeField(null=True, blank=True)
    submission_completed_at = models.DateTimeField(null=True, blank=True)

    provider_message_id = models.CharField(max_length=255, blank=True, null=True)
    #: The provider's own last-known status string for this send, when one is
    #: available (a bridge operation lookup, a webhook). Free text -- it is the
    #: provider's vocabulary, not this application's.
    provider_status = models.CharField(max_length=64, blank=True, default="")
    last_error = models.CharField(max_length=500, blank=True, default="")
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["message", "attempt_number"], name="unique_attempt_number_per_message",
            ),
        ]
        indexes = [
            models.Index(fields=["message", "-attempt_number"]),
            models.Index(fields=["campaign_id", "status"]),
            models.Index(fields=["status", "lease_expires_at"]),
            models.Index(fields=["provider_message_id"]),
            models.Index(fields=["tenant", "status"]),
        ]

    def __str__(self):
        return f"Attempt {self.attempt_number} of message #{self.message_id} ({self.status})"


class PendingButtonPrompt(models.Model):
    """
    A question sent as a numbered list, and what each number meant.

    Written whenever a message with buttons goes out over a connection that
    cannot deliver real ones -- a QR-linked number, where WhatsApp reserves
    interactive messages for the official Cloud API. The recipient sees
    "1. Yes please / 2. Not now" and types a number back, and this row is what
    turns that number into the button id the automation engine already matches
    on. Without it the fallback is legible and inert: the customer answers and
    nothing happens.

    Deliberately *not* a field on Message. The lookup on the inbound path is
    "what is this conversation currently being asked?", which wants an index on
    conversation and expiry, not a scan of every message that ever carried
    buttons. Keeping it separate also means an unanswered question can expire
    without rewriting the message it came from.

    `tenant`, `contact` and `whatsapp_account` are denormalised from the
    conversation for the reason every other hot table here denormalises them:
    resolution is filtered by tenant on every inbound message, and isolation
    that depends on a join is isolation somebody can forget to write. Nothing
    in the contact's message can reach any of these -- they are read from the
    conversation row the inbound message already belongs to.
    """

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="pending_button_prompts")
    conversation = models.ForeignKey(
        "inbox.Conversation", on_delete=models.CASCADE, related_name="pending_button_prompts",
    )
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="+")
    whatsapp_account = models.ForeignKey("whatsapp.WhatsAppAccount", on_delete=models.CASCADE, related_name="+")
    #: The message that asked. Null once that message is deleted -- the options
    #: are copied below rather than read through it, so the prompt outlives it.
    message = models.ForeignKey(Message, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    #: The buttons exactly as they were offered, in the order they were
    #: numbered. Copied rather than referenced so that editing a template later
    #: cannot renumber a question already sitting on somebody's phone.
    options = models.JSONField(default=list, blank=True)

    #: When a reply stops counting as an answer to this question. A number
    #: typed an hour later is far more likely to be a house number than a
    #: choice, and acting on it would fire an automation nobody asked for.
    expires_at = models.DateTimeField()

    answered_at = models.DateTimeField(null=True, blank=True)
    #: Which option they picked, once they have. Kept for the audit trail.
    resolved_button_id = models.CharField(max_length=256, blank=True)
    #: How many times we have re-sent the options after an unusable reply.
    reprompt_count = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The one query on the inbound path: the open question for this
            # conversation, newest first.
            models.Index(fields=["conversation", "answered_at", "-created_at"]),
            models.Index(fields=["tenant", "-created_at"]),
        ]

    def __str__(self):
        return f"Pending question on conversation {self.conversation_id} ({len(self.options)} options)"

    @property
    def is_open(self) -> bool:
        from django.utils import timezone

        return self.answered_at is None and self.expires_at > timezone.now()
