from django.conf import settings
from django.db import models

from core.media import resolve_media_items, spec_from_object


class _MessageContentMixin(models.Model):
    """Shared "template or inline message" fields, mirroring Campaign.effective_message()."""

    class MessageType(models.TextChoices):
        TEXT = "TEXT", "Text"
        IMAGE = "IMAGE", "Image"
        VIDEO = "VIDEO", "Video"
        DOCUMENT = "DOCUMENT", "Document"

    template = models.ForeignKey("templates_app.MessageTemplate", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    message_type = models.CharField(max_length=20, choices=MessageType.choices, default=MessageType.TEXT)
    message_body = models.TextField(blank=True)
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

    class Meta:
        abstract = True

    def effective_message(self) -> dict:
        # Mirrors Campaign.effective_message(), including the resolved
        # `media_items` list -- see core.media.resolve_media_items.
        if self.template_id:
            return {
                "message_type": self.template.message_type,
                "body": self.template.body,
                "media_url": self.template.media_url,
                "media_items": resolve_media_items(spec_from_object(self.template)),
                "caption": self.template.caption,
                "filename": self.template.filename,
                "variables": self.template.variables,
                "buttons": self.template.buttons,
                "template_id": self.template_id,
            }
        return {
            "message_type": self.message_type,
            "body": self.message_body,
            "media_url": self.media_url,
            "media_items": resolve_media_items(spec_from_object(self)),
            "caption": self.caption,
            "filename": self.filename,
            "variables": {},
            "buttons": self.buttons,
            "template_id": None,
        }


class AutomationRule(_MessageContentMixin):
    """Covers both the Welcome Message and keyword-based Auto Reply triggers."""

    class Trigger(models.TextChoices):
        WELCOME = "WELCOME", "Welcome message"
        KEYWORD = "KEYWORD", "Keyword match"
        BUTTON = "BUTTON", "Button tapped"

    class MatchType(models.TextChoices):
        EXACT = "EXACT", "Exact match"
        CONTAINS = "CONTAINS", "Contains"
        STARTS_WITH = "STARTS_WITH", "Starts with"
        ENDS_WITH = "ENDS_WITH", "Ends with"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="automation_rules")
    whatsapp_account = models.ForeignKey("whatsapp.WhatsAppAccount", on_delete=models.CASCADE, related_name="automation_rules")
    name = models.CharField(max_length=150)

    trigger_type = models.CharField(max_length=20, choices=Trigger.choices)
    keyword = models.CharField(max_length=200, blank=True)
    keyword_match_type = models.CharField(max_length=20, choices=MatchType.choices, default=MatchType.CONTAINS)
    # The id of the reply button this rule answers, for BUTTON triggers. Ids
    # come from the buttons on whatever message offered them (a template, a
    # campaign, a workflow node) -- see core.buttons.
    button_id = models.CharField(max_length=256, blank=True)
    priority = models.IntegerField(default=0, help_text="Higher priority keyword rules are evaluated first.")
    # Shortest gap between two firings of this rule for the *same* contact.
    # 0 keeps the original behaviour (answer every time). Anything above it
    # stops a contact who types the same keyword ten times in a row from
    # getting ten identical replies -- which reads as spam to the recipient
    # and, reported enough times, is what gets a number banned.
    cooldown_seconds = models.PositiveIntegerField(
        default=0, help_text="Minimum seconds before this rule answers the same contact again. 0 = no limit."
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-priority", "id"]
        indexes = [models.Index(fields=["whatsapp_account", "trigger_type", "is_active"])]

    def __str__(self):
        return self.name

    def matches_button(self, button_id: str) -> bool:
        if self.trigger_type != self.Trigger.BUTTON:
            return False
        return bool(self.button_id) and self.button_id == button_id

    def matches(self, text: str) -> bool:
        if self.trigger_type != self.Trigger.KEYWORD:
            return False
        haystack, needle = text.lower(), self.keyword.lower()
        if not needle:
            return False
        if self.keyword_match_type == self.MatchType.EXACT:
            return haystack == needle
        if self.keyword_match_type == self.MatchType.CONTAINS:
            return needle in haystack
        if self.keyword_match_type == self.MatchType.STARTS_WITH:
            return haystack.startswith(needle)
        if self.keyword_match_type == self.MatchType.ENDS_WITH:
            return haystack.endswith(needle)
        return False


class FollowUpRule(_MessageContentMixin):
    """Sent when a contact hasn't replied within `delay_minutes` of our last outbound message."""

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="follow_up_rules")
    whatsapp_account = models.ForeignKey("whatsapp.WhatsAppAccount", on_delete=models.CASCADE, related_name="follow_up_rules")
    name = models.CharField(max_length=150)
    delay_minutes = models.PositiveIntegerField()
    # Which outbound template arms this rule. Empty means every outbound
    # message on the account arms it -- the original behaviour, and still
    # right for a single-template account. Once a tenant sends more than one
    # campaign/template from the same number, that default means a "did you
    # get our Diwali offer?" rule also fires after an unrelated payment
    # reminder; picking specific templates here is how a rule stays scoped to
    # the conversation it's actually meant to continue.
    trigger_templates = models.ManyToManyField(
        "templates_app.MessageTemplate", blank=True, related_name="follow_up_rules_armed_by",
        help_text="Only arm this rule for messages sent with one of these templates. Leave empty to arm it for every outbound message.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["whatsapp_account", "is_active"])]

    def __str__(self):
        return self.name


class Workflow(models.Model):
    """
    A validated graph of TRIGGER/MESSAGE/WAIT/CONDITION/TAG/ASSIGN_AGENT/END
    nodes, per the platform's visual workflow architecture. See services.py
    for the node schema and validate_nodes() for what's enforced.
    """

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="workflows")
    whatsapp_account = models.ForeignKey("whatsapp.WhatsAppAccount", on_delete=models.CASCADE, related_name="workflows")
    name = models.CharField(max_length=150)
    nodes = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class WorkflowRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        WAITING = "WAITING", "Waiting"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="runs")
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="workflow_runs")
    conversation = models.ForeignKey("inbox.Conversation", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    current_node_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    context = models.JSONField(default=dict, blank=True)
    # Why a run stopped, when it stopped badly. A FAILED run with no
    # explanation is untriageable -- the graph, the contact and the provider
    # are all plausible culprits and nothing on the row says which.
    error = models.TextField(blank=True)
    # Nodes executed across *every* resume of this run, not just the current
    # one. A graph may legitimately loop through a WAIT (a drip sequence that
    # nudges until someone answers), so the only safe backstop against a run
    # that never ends is a lifetime budget; see services.MAX_TOTAL_STEPS.
    steps_taken = models.PositiveIntegerField(default=0)

    started_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["workflow", "status"]),
            # "Is this contact already in a run of this workflow?" -- asked on
            # every inbound message once workflows auto-trigger.
            models.Index(fields=["contact", "status"]),
        ]

    def __str__(self):
        return f"Run #{self.pk} of {self.workflow_id} ({self.status})"
