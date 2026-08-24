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

    # Set on an INBOUND message that is a contact tapping one of our reply
    # buttons: the id of the button tapped (`body` holds the label they saw).
    # Blank for everything else, including inbound plain text. Automation
    # rules and workflow conditions match on this -- see
    # apps.automation.services.trigger_button_rules.
    replied_button_id = models.CharField(max_length=256, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    provider_message_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    error_message = models.CharField(max_length=500, blank=True)
    retry_count = models.PositiveIntegerField(default=0)

    # The randomly generated delay, in seconds, that the campaign dispatcher
    # waited before handing this message to the provider. One independently
    # drawn value per message; NULL for messages that were never dispatched
    # by a campaign (inbox replies, automation sends, inbound messages).
    assigned_delay_seconds = models.FloatField(null=True, blank=True)

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
        ]

    def __str__(self):
        return f"Message #{self.pk} to {self.to_phone} ({self.status})"
