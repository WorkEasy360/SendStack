from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from core.media import resolve_media_items, spec_from_object


class Campaign(models.Model):

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SCHEDULED = "SCHEDULED", "Scheduled"
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        PAUSED = "PAUSED", "Paused"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"
        FAILED = "FAILED", "Failed"

    class MessageType(models.TextChoices):
        TEXT = "TEXT", "Text"
        IMAGE = "IMAGE", "Image"
        VIDEO = "VIDEO", "Video"
        DOCUMENT = "DOCUMENT", "Document"

    class AudienceType(models.TextChoices):
        ALL = "ALL", "All contacts"
        GROUP = "GROUP", "Group"
        TAG = "TAG", "Tag"
        SEGMENT = "SEGMENT", "Saved segment"
        CONTACT_IDS = "CONTACT_IDS", "Selected contacts"

    class AccountAssignmentMode(models.TextChoices):
        MANUAL = "MANUAL", "Manual (single account)"
        AUTO = "AUTO", "Automatic / random"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="campaigns")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="campaigns")
    name = models.CharField(max_length=150)

    # In MANUAL mode this is *the* sending account. In AUTO mode it is the
    # campaign's primary/fallback account (recipients are dealt across every
    # connected account instead) -- kept non-null either way so every
    # pre-existing campaign, query, filter and report keeps working unchanged.
    whatsapp_account = models.ForeignKey("whatsapp.WhatsAppAccount", on_delete=models.PROTECT, related_name="campaigns")
    account_assignment_mode = models.CharField(
        max_length=10,
        choices=AccountAssignmentMode.choices,
        default=AccountAssignmentMode.MANUAL,
    )

    # Per-message random send delay, in seconds. NULL means "use the
    # deployment default" (settings.CAMPAIGN_MIN/MAX_DELAY_SECONDS), which is
    # what every campaign created before this feature existed resolves to.
    min_delay_seconds = models.FloatField(null=True, blank=True, validators=[MinValueValidator(0)])
    max_delay_seconds = models.FloatField(null=True, blank=True, validators=[MinValueValidator(0)])
    template = models.ForeignKey(
        "templates_app.MessageTemplate", on_delete=models.SET_NULL, null=True, blank=True, related_name="campaigns"
    )

    # Inline message content, used only when no template is selected.
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

    # Defaults on, because a broadcast with no way out of it is the thing that
    # gets numbers reported, and a recipient who cannot find the exit uses the
    # "block" button instead. Off is a deliberate choice for the cases where it
    # is genuinely wrong -- a one-off transactional notice, an audience that
    # opted in minutes ago -- not the default nobody thought about.
    append_opt_out = models.BooleanField(
        default=True,
        help_text="Add an unsubscribe button (or a 'reply STOP' line) to every message in this campaign.",
    )

    # On by default because a campaign whose links are not tracked can report
    # that it was delivered and read, and nothing about whether it worked.
    # Turned off for a message where the raw URL matters to the reader -- a
    # link they are expected to recognise, or one going somewhere that
    # rejects unknown referrers.
    track_links = models.BooleanField(
        default=True,
        help_text="Shorten every link in this campaign so clicks can be attributed to the contact who clicked.",
    )

    audience_type = models.CharField(max_length=20, choices=AudienceType.choices, default=AudienceType.ALL)
    audience_group = models.ForeignKey("contacts.ContactGroup", on_delete=models.SET_NULL, null=True, blank=True, related_name="campaigns")
    audience_tag = models.ForeignKey("contacts.ContactTag", on_delete=models.SET_NULL, null=True, blank=True, related_name="campaigns")
    audience_segment = models.ForeignKey("contacts.ContactSegment", on_delete=models.SET_NULL, null=True, blank=True, related_name="campaigns")
    audience_contact_ids = models.JSONField(default=list, blank=True)

    scheduled_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    total_recipients = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    delivered_count = models.PositiveIntegerField(default=0)
    read_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    # Recipients that could not be attempted at all because no connected
    # account was available for them -- distinct from failed_count, which
    # means the provider was reached and refused the send.
    skipped_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "status"])]

    def __str__(self):
        return self.name

    def delay_bounds(self) -> tuple[float, float]:
        """Resolved (min, max) send delay for this campaign, in seconds."""
        from .assignment import resolve_delay_bounds

        return resolve_delay_bounds(self)

    def effective_message(self) -> dict:
        # `media_items` is always resolved (never the raw column) so callers
        # get one list whether the source predates multi-attachment support or
        # not -- see core.media.resolve_media_items.
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
        }
