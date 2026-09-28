import re

from django.conf import settings
from django.db import models

#: The two personalization syntaxes, matched by one expression.
#:
#: ``[Name]`` is this platform's original syntax and every template, campaign
#: and automation rule ever saved uses it, so it stays exactly as it was.
#: ``{{name}}`` is the syntax everyone arriving from another WhatsApp tool
#: types by reflex, and templates written that way used to send the braces to
#: the customer verbatim -- a personalization bug that looks like the product
#: is broken. Both are recognised, both resolve against the same context, and
#: neither is rewritten on save: whichever one somebody typed is the one they
#: see when they open the template again.
#:
#: Two capture groups, exactly one of which matches; `variable_name` below is
#: what reads them so no caller has to know which.
VARIABLE_RE = re.compile(r"\[(\w+)\]|\{\{\s*(\w+)\s*\}\}")


def variable_name(match) -> str:
    """The name inside a VARIABLE_RE match, whichever syntax it was written in."""
    return match.group(1) or match.group(2) or ""


def extract_variables(*texts: str) -> list[str]:
    names = []
    for text in texts:
        for match in VARIABLE_RE.finditer(text or ""):
            name = variable_name(match)
            if name and name not in names:
                names.append(name)
    return names


def content_texts(obj) -> list:
    """
    Every author-written string on a message that personalization applies to.

    Body and caption are the obvious two. The rest are the interactive content:
    a list's header, footer and row labels, and a poll's question and options
    are as much "the message" as the body is, and a menu row reading
    "Reorder {{last_product}}" is one of the more useful things a list can do.
    Gathered in one place so ``detected_variables`` and the renderer cannot
    disagree about which fields carry variables.
    """
    texts = [getattr(obj, "body", "") or "", getattr(obj, "caption", "") or ""]

    list_content = getattr(obj, "list_content", None) or {}
    texts.extend([list_content.get("header", ""), list_content.get("footer", ""), list_content.get("button", "")])
    for section in list_content.get("sections", []):
        texts.append(section.get("title", ""))
        for row in section.get("rows", []):
            texts.extend([row.get("title", ""), row.get("description", "")])

    poll_content = getattr(obj, "poll_content", None) or {}
    texts.append(poll_content.get("question", ""))
    texts.extend(poll_content.get("options", []))

    for button in getattr(obj, "buttons", None) or []:
        texts.append(button.get("title", ""))

    return [text for text in texts if text]


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

    # Optional since polls arrived: a poll's question *is* the message and
    # WhatsApp renders it as a bubble of its own, so a poll template
    # legitimately has no body. Every other kind still needs content of some
    # sort, which the serializer checks -- see MessageTemplateSerializer.
    body = models.TextField(
        blank=True, help_text="Use [Name] or {{name}} as personalization variables.",
    )
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

    # A tappable menu, and a poll. Mutually exclusive with each other and with
    # `buttons` -- WhatsApp sends the three as different kinds of message, so
    # the serializer refuses a payload carrying two. Empty is what every row
    # written before they existed looks like, and is what `content_kind`
    # reads as a plain TEXT message. See core.interactive for both shapes.
    list_content = models.JSONField(default=dict, blank=True)
    poll_content = models.JSONField(default=dict, blank=True)

    # {variable_name: fallback_value} for any [Variable] or {{variable}} used
    # in body/caption.
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
        return extract_variables(*content_texts(self))

    @property
    def content_kind(self) -> str:
        """TEXT / BUTTON / LIST / POLL -- see core.interactive.content_kind."""
        from core.interactive import content_kind

        return content_kind(self)
