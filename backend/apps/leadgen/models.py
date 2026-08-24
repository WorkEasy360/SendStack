from django.conf import settings
from django.db import models


class ClickToChatLink(models.Model):
    """
    One way in: a wa.me link, the QR code printed from it, and the website
    button that points at it.

    All three are the same row on purpose. A shop that puts a QR on the
    counter, the same link in an Instagram bio and the same button on its site
    wants to know which of the three brought the customer, and that is one
    question about one destination -- not three separate features.
    """

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="click_to_chat_links")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    whatsapp_account = models.ForeignKey(
        "whatsapp.WhatsAppAccount", on_delete=models.CASCADE, related_name="click_to_chat_links",
    )

    name = models.CharField(max_length=150)
    code = models.CharField(max_length=16, unique=True)
    prefilled_message = models.TextField(
        blank=True,
        help_text="What the customer's message box is filled with. They can still edit it before sending.",
    )

    # The reference is appended to the prefilled text so the message that
    # arrives can be traced back to this link. It is visible to the customer --
    # WhatsApp shows them exactly what they are about to send, and there is no
    # hidden field in that message -- hence the switch for anyone who would
    # rather have a clean message than the attribution.
    include_reference = models.BooleanField(default=True)

    utm_source = models.CharField(max_length=100, blank=True)
    utm_medium = models.CharField(max_length=100, blank=True)
    utm_campaign = models.CharField(max_length=100, blank=True)

    is_active = models.BooleanField(default=True)
    click_count = models.PositiveIntegerField(default=0)
    lead_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "is_active"])]

    def __str__(self):
        return self.name

    @property
    def reference(self) -> str:
        """The token written into the prefilled message, and matched on the way back."""
        return f"#{self.code}"


class LeadEvent(models.Model):
    """
    Where one contact came from, recorded once, at the moment it was knowable.

    Kept as events rather than only as a column on Contact because a customer
    who first arrived from a QR poster and later clicked an ad has two true
    answers, and collapsing that to one loses the half that shows the ad
    working.
    """

    class Kind(models.TextChoices):
        CLICK_TO_CHAT = "CLICK_TO_CHAT", "Click-to-chat link"
        WIDGET = "WIDGET", "Website widget"
        AD_REFERRAL = "AD_REFERRAL", "Click-to-WhatsApp ad"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="lead_events")
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.CASCADE, null=True, blank=True, related_name="lead_events",
    )
    message = models.ForeignKey(
        "messaging.Message", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    link = models.ForeignKey(
        ClickToChatLink, on_delete=models.SET_NULL, null=True, blank=True, related_name="leads",
    )

    kind = models.CharField(max_length=20, choices=Kind.choices)
    # The ad's own payload as the provider sent it (headline, source_url,
    # ctwa_clid). Stored whole rather than picked apart: Meta adds fields to it,
    # and a column-per-field schema loses whatever was added since.
    payload = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["contact", "kind"]),
        ]

    def __str__(self):
        return f"{self.kind} for contact {self.contact_id}"


class LinkVisit(models.Model):
    """A tap on the short link, before WhatsApp opens -- the top of this funnel."""

    link = models.ForeignKey(ClickToChatLink, on_delete=models.CASCADE, related_name="visits")
    visited_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    referer = models.TextField(blank=True)

    class Meta:
        ordering = ["-visited_at"]
        indexes = [models.Index(fields=["link", "-visited_at"])]
