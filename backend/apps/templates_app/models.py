import re

from django.conf import settings
from django.db import models

VARIABLE_RE = re.compile(r"\[(\w+)\]")


def extract_variables(*texts: str) -> list[str]:
    names = []
    for text in texts:
        for match in VARIABLE_RE.findall(text or ""):
            if match not in names:
                names.append(match)
    return names


class MessageTemplate(models.Model):
    class MessageType(models.TextChoices):
        TEXT = "TEXT", "Text"
        IMAGE = "IMAGE", "Image"
        VIDEO = "VIDEO", "Video"
        DOCUMENT = "DOCUMENT", "Document"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="message_templates")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="message_templates")

    name = models.CharField(max_length=150)
    message_type = models.CharField(max_length=20, choices=MessageType.choices, default=MessageType.TEXT)

    body = models.TextField(help_text="Use [Name], [OrderID], etc. as personalization variables.")
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

    # {variable_name: fallback_value} for any [Variable] used in body/caption.
    variables = models.JSONField(default=dict, blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="unique_template_name_per_tenant")]
        ordering = ["name"]

    def __str__(self):
        return self.name

    def detected_variables(self) -> list[str]:
        return extract_variables(self.body, self.caption)
