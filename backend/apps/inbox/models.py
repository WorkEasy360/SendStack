from django.conf import settings
from django.db import models


class Conversation(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="conversations")
    whatsapp_account = models.ForeignKey("whatsapp.WhatsAppAccount", on_delete=models.CASCADE, related_name="conversations")
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="conversations")

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    assigned_agent = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_conversations"
    )
    labels = models.ManyToManyField("ConversationLabel", blank=True, related_name="conversations")

    unread_count = models.PositiveIntegerField(default=0)
    last_message_at = models.DateTimeField(null=True, blank=True)

    # Response tracking. `awaiting_reply_since` is when the oldest currently
    # unanswered inbound message arrived, and is cleared the moment a person
    # replies -- it is what makes "which chats are overdue" a query rather
    # than a scan of every thread. `first_response_seconds` is written once
    # and never revised, so a conversation that went quiet for a week and was
    # then answered still reports the response time it actually achieved.
    awaiting_reply_since = models.DateTimeField(null=True, blank=True)
    first_response_seconds = models.PositiveIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["whatsapp_account", "contact"], name="unique_conversation_per_account_contact"),
        ]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "-last_message_at"]),
            # "Show me everything still waiting on us", which the inbox asks
            # on every load once SLA targets are switched on.
            models.Index(fields=["tenant", "awaiting_reply_since"]),
        ]
        ordering = ["-last_message_at"]

    def __str__(self):
        return f"Conversation #{self.pk} with {self.contact_id}"


class ConversationLabel(models.Model):
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="conversation_labels")
    name = models.CharField(max_length=50)
    color = models.CharField(max_length=7, default="#2563eb")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="unique_conversation_label_per_tenant")]
        ordering = ["name"]

    def __str__(self):
        return self.name


class ConversationNote(models.Model):
    """An internal note visible only to agents, never sent to the customer."""

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="notes")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
