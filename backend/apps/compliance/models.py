from datetime import time

from django.conf import settings
from django.db import models

DEFAULT_OPT_OUT_CONFIRMATION = (
    "You have been unsubscribed and will not receive further messages from us. "
    "Reply START at any time to resubscribe."
)

DEFAULT_OPT_IN_CONFIRMATION = "You are subscribed again. Reply STOP at any time to unsubscribe."

DEFAULT_OPT_OUT_FOOTER = "Reply STOP to unsubscribe."


class CompliancePolicy(models.Model):
    """
    One tenant's opt-out and quiet-hours rules.

    Every field has a working default, and `for_tenant` hands back an unsaved
    instance when a tenant has never opened the settings page -- so the send
    gate can consult a policy for any tenant without a write on the hot path,
    and without every tenant needing a row created up front.
    """

    tenant = models.OneToOneField("tenants.Tenant", on_delete=models.CASCADE, related_name="compliance_policy")

    # -- Opt-out handling ---------------------------------------------------
    stop_keywords_enabled = models.BooleanField(
        default=True,
        help_text="Unsubscribe a contact automatically when they reply with a stop word.",
    )
    extra_stop_keywords = models.JSONField(
        default=list, blank=True,
        help_text="Extra phrases meaning 'unsubscribe me', on top of the built-in multi-language list.",
    )
    extra_start_keywords = models.JSONField(default=list, blank=True)
    send_opt_out_confirmation = models.BooleanField(default=True)
    opt_out_confirmation = models.TextField(default=DEFAULT_OPT_OUT_CONFIRMATION, blank=True)
    send_opt_in_confirmation = models.BooleanField(default=True)
    opt_in_confirmation = models.TextField(default=DEFAULT_OPT_IN_CONFIRMATION, blank=True)

    # How campaigns offer the opt-out.
    #
    # The footer is the default because it costs nothing: it is plain text,
    # so it renders identically on every phone and every connection type. A
    # button is easier to tap and is honoured exactly rather than through
    # keyword matching, but it turns the message into an interactive one,
    # and interactive messages are not something a QR/device-linked
    # connection can guarantee will render. That is a trade worth offering,
    # not one to make on a customer's behalf.
    class OptOutStyle(models.TextChoices):
        FOOTER = "FOOTER", "A 'reply STOP' line"
        BUTTON = "BUTTON", "A tappable unsubscribe button"

    opt_out_style = models.CharField(max_length=10, choices=OptOutStyle.choices, default=OptOutStyle.FOOTER)
    opt_out_button_title = models.CharField(max_length=20, default="Unsubscribe")
    opt_out_footer = models.CharField(max_length=200, default=DEFAULT_OPT_OUT_FOOTER, blank=True)

    # -- Quiet hours --------------------------------------------------------
    quiet_hours_enabled = models.BooleanField(
        default=False,
        help_text="Hold campaign and automated messages outside the allowed window instead of sending them.",
    )
    quiet_hours_start = models.TimeField(default=time(21, 0), help_text="Local time the quiet window opens.")
    quiet_hours_end = models.TimeField(default=time(9, 0), help_text="Local time the quiet window closes.")
    timezone = models.CharField(
        max_length=64, default="UTC",
        help_text="Timezone quiet hours are measured in when a contact's own timezone is unknown.",
    )
    use_contact_timezone = models.BooleanField(
        default=False,
        help_text="Measure quiet hours in the contact's country timezone where it is known.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "compliance policies"

    def __str__(self):
        return f"Compliance policy for tenant {self.tenant_id}"

    @classmethod
    def for_tenant(cls, tenant):
        """
        The tenant's policy, or an unsaved default one.

        Unsaved rather than created on demand: this runs on every send, and a
        tenant that has never changed a setting should not be paying for a
        write -- nor should a read path be creating rows behind the user's
        back.
        """
        tenant_id = getattr(tenant, "pk", tenant)
        policy = cls.objects.filter(tenant_id=tenant_id).first()
        return policy or cls(tenant_id=tenant_id)


class ConsentEvent(models.Model):
    """
    The audit trail behind `Contact.subscribed`.

    The flag on the contact answers "may we message them"; this table answers
    "who decided that, when, and on what evidence" -- the part a regulator, a
    platform review, or an unhappy recipient actually asks about. Rows are only
    ever appended: opting back in writes a new row, it never edits the old one.
    """

    class Event(models.TextChoices):
        OPT_IN = "OPT_IN", "Opted in"
        OPT_OUT = "OPT_OUT", "Opted out"

    class Source(models.TextChoices):
        INBOUND_KEYWORD = "INBOUND_KEYWORD", "Replied with a stop/start word"
        INBOUND_BUTTON = "INBOUND_BUTTON", "Tapped the unsubscribe button"
        AGENT = "AGENT", "Changed by a team member"
        IMPORT = "IMPORT", "Contact import"
        API = "API", "API"
        SYSTEM = "SYSTEM", "System"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="consent_events")
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="consent_events")
    event = models.CharField(max_length=10, choices=Event.choices)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.SYSTEM)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    # The inbound message that carried the request, where there was one. It is
    # the evidence for the decision, so SET_NULL rather than CASCADE: losing
    # the message must not quietly erase the record of the opt-out itself.
    message = models.ForeignKey(
        "messaging.Message", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    detail = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["contact", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.contact_id}:{self.event}"
