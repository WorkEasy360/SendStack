import uuid

from django.conf import settings
from django.db import models

from core.uploads import safe_extension


class ContactGroup(models.Model):
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.PROTECT, related_name="contact_groups")
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="unique_contact_group_per_tenant")]
        ordering = ["name"]


class ContactTag(models.Model):
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.PROTECT, related_name="contact_tags")
    name = models.CharField(max_length=50)
    color = models.CharField(max_length=7, default="#2563eb")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="unique_contact_tag_per_tenant")]
        ordering = ["name"]


class Contact(models.Model):
    class Lifecycle(models.TextChoices):
        """
        What this person is to the business right now.

        Deliberately *not* the same thing as a deal's pipeline stage. A stage
        answers "where is this particular sale" and a contact can have several
        at once (or none); this answers "who is this person", has exactly one
        value, and is the thing a segment or an automation filters on. The
        five values are the fewest that still let a seller tell a stranger
        from someone mid-conversation from someone who has already paid.

        Nearly every row is moved here by `crm.lifecycle`, not by hand: NEW
        becomes ENGAGED when they first reply, CUSTOMER when a deal is won,
        and INACTIVE after a long silence. A user who never opens this field
        still gets a segmented contact list.
        """

        NEW = "NEW", "New"
        ENGAGED = "ENGAGED", "Engaged"
        QUALIFIED = "QUALIFIED", "Qualified"
        CUSTOMER = "CUSTOMER", "Customer"
        INACTIVE = "INACTIVE", "Inactive"
        LOST = "LOST", "Lost"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.PROTECT, related_name="contacts")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_contacts")
    name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=16)
    email = models.EmailField(blank=True)
    company = models.CharField(max_length=150, blank=True)
    country = models.CharField(max_length=2, blank=True)
    custom_fields = models.JSONField(default=dict, blank=True)

    # --- Consent -----------------------------------------------------------
    #
    # Two fields, answering two different questions, and conflating them is the
    # mistake this pair exists to prevent.
    #
    # `subscribed` is the **opt-out**: has this person told us to stop? It
    # starts True because somebody who has never said anything has not said
    # stop, and it is the flag the send gate has always honoured.
    #
    # `marketing_consent` is the **opt-in**: did they ever say yes, and can we
    # show where that yes came from? It starts UNKNOWN, and UNKNOWN is the
    # honest answer for every contact that already exists and for every row of
    # every CSV anybody has ever imported -- a phone number in a spreadsheet is
    # not evidence of consent, and defaulting these to GRANTED would be the
    # platform inventing consent on a customer's behalf.
    #
    # Precedence, enforced in one place (compliance.services.may_receive_marketing):
    #
    #     explicit opt-out  >  marketing consent  >  campaign eligibility
    #
    # An opt-out is authoritative and outranks any later "consent" from an
    # import, a CRM sync or an API caller.
    subscribed = models.BooleanField(default=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

    class MarketingConsent(models.TextChoices):
        UNKNOWN = "UNKNOWN", "Not recorded"
        GRANTED = "GRANTED", "Consent given"
        DENIED = "DENIED", "Consent refused"

    class ConsentSource(models.TextChoices):
        MANUAL = "MANUAL", "Entered by a team member"
        WEBSITE = "WEBSITE", "Website"
        FORM = "FORM", "Opt-in form"
        CRM = "CRM", "CRM or other system"
        API = "API", "API"
        CSV = "CSV", "File import"
        CUSTOMER_PROVIDED = "CUSTOMER_PROVIDED", "Provided by the customer"
        INBOUND = "INBOUND", "They messaged us first"
        OTHER = "OTHER", "Other"

    marketing_consent = models.CharField(
        max_length=10, choices=MarketingConsent.choices, default=MarketingConsent.UNKNOWN,
        help_text="Whether this contact has agreed to receive marketing. Never inferred from having their number.",
    )
    consent_source = models.CharField(
        max_length=20, choices=ConsentSource.choices, blank=True,
        help_text="Where the consent came from. The evidence, not the claim.",
    )
    consent_at = models.DateTimeField(null=True, blank=True, help_text="When consent was given or refused.")
    # Free text and short on purpose: the identifier of the form, campaign or
    # list the consent came from. Not a place for raw personal data -- storing
    # a copy of somebody's submission "as evidence" would collect more than the
    # purpose needs. The append-only ConsentEvent log carries the rest.
    consent_reference = models.CharField(
        max_length=120, blank=True,
        help_text="Which form, campaign or list this consent came from, e.g. 'newsletter-signup-2026'.",
    )

    # --- CRM ---------------------------------------------------------------
    # Whose customer this is. Separate from `created_by`, which records who
    # first typed the number in and never changes: the person who imported a
    # 5,000-row CSV does not thereby own 5,000 relationships.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="owned_contacts",
    )
    lifecycle_status = models.CharField(max_length=20, choices=Lifecycle.choices, default=Lifecycle.NEW)

    # 0-100, recomputed from behaviour alone -- see crm.scoring. Stored rather
    # than derived on read because sorting and filtering a 100k-row contact
    # list by "hottest first" has to be an indexed column, and because the
    # inputs (message counts, deal state) each cost a query to gather.
    lead_score = models.PositiveSmallIntegerField(default=0)
    lead_score_updated_at = models.DateTimeField(null=True, blank=True)

    # The two questions every follow-up decision needs: when did we last speak
    # to them, and when did they last answer. Both are denormalised from
    # messaging.Message because asking that table per contact is what turns a
    # contact list into a table scan.
    last_outbound_at = models.DateTimeField(null=True, blank=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True)

    # Where this contact came from, kept as first touch: the answer to "which
    # channel is bringing us customers" is the one that brought them, not the
    # last one they happened to use. The full history, including later
    # touches, is in leadgen.LeadEvent.
    source = models.CharField(max_length=30, blank=True)
    source_detail = models.JSONField(default=dict, blank=True)

    # --- WhatsApp registration -------------------------------------------
    # Whether this number is on WhatsApp at all, as last answered by WhatsApp
    # itself through the number filter (apps.leadtools). UNKNOWN until
    # somebody runs it. Stamped with a time rather than kept as a bare flag,
    # because a number that was registered last quarter may not be today and
    # a report that cannot say when it looked is not worth trusting.
    class WhatsAppStatus(models.TextChoices):
        UNKNOWN = "UNKNOWN", "Not checked"
        ON_WHATSAPP = "ON_WHATSAPP", "On WhatsApp"
        NOT_ON_WHATSAPP = "NOT_ON_WHATSAPP", "Not on WhatsApp"

    whatsapp_status = models.CharField(
        max_length=16, choices=WhatsAppStatus.choices, default=WhatsAppStatus.UNKNOWN,
    )
    whatsapp_checked_at = models.DateTimeField(null=True, blank=True)
    groups = models.ManyToManyField(ContactGroup, blank=True, related_name="contacts")
    tags = models.ManyToManyField(ContactTag, blank=True, related_name="contacts")
    last_message_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "phone"], name="unique_contact_phone_per_tenant")]
        indexes = [
            models.Index(fields=["tenant", "subscribed"]),
            # "Who on this account may be marketed to" -- asked by the campaign
            # audience builder and by every consent report. Without it, a
            # tenant with consent enforcement on scans its whole contact table
            # to build an audience.
            models.Index(fields=["tenant", "marketing_consent"]),
            # "Who on this list is actually reachable" -- the filter the
            # number-filter results screen and the contact list both offer.
            models.Index(fields=["tenant", "whatsapp_status"]),
            models.Index(fields=["tenant", "phone"]),
            # The CRM list and every smart segment filter on one of these
            # three columns within a tenant; without them each is a scan of
            # the whole contact table.
            models.Index(fields=["tenant", "lifecycle_status"]),
            models.Index(fields=["tenant", "-lead_score"]),
            models.Index(fields=["tenant", "owner"]),
            # "Who have we messaged but not heard back from", which is what
            # the no-reply segments and the Today digest both ask.
            models.Index(fields=["tenant", "last_inbound_at"]),
            models.Index(fields=["tenant", "last_outbound_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return self.name or self.phone

    @property
    def score_band(self) -> str:
        """HOT / WARM / COLD -- the only form of the score a user ever sees."""
        from apps.crm.scoring import band_for

        return band_for(self.lead_score)


def contact_import_upload_path(instance, filename):
    # A UUID stem rather than the name that arrived: this file lands under
    # MEDIA_ROOT, which nginx serves, so the uploader must have no say over
    # the path it is served from. safe_extension() is what guarantees the one
    # remaining piece -- the extension -- cannot become a path segment either.
    return f"contact_imports/tenant_{instance.tenant_id}/{uuid.uuid4().hex}{safe_extension(filename)}"


class ContactImport(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="contact_imports")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="contact_imports")
    file = models.FileField(upload_to=contact_import_upload_path)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    group_ids = models.JSONField(default=list, blank=True)
    tag_ids = models.JSONField(default=list, blank=True)

    # What the uploader is asserting about consent for the rows in this file,
    # and where it came from. Blank means "not asserting anything", which is
    # the default and leaves every imported contact at UNKNOWN.
    #
    # The alternative -- inferring consent from the act of uploading -- is the
    # single most common way a platform ends up sending marketing to people who
    # never agreed to it, and it is what the audit asked to be made impossible.
    # An importer that silently marks 10,000 strangers as opted-in is not a
    # convenience, it is a compliance incident with a progress bar.
    consent_source = models.CharField(
        max_length=20, blank=True,
        help_text="Where consent for these contacts came from. Blank means consent is not being asserted.",
    )
    consent_reference = models.CharField(
        max_length=120, blank=True,
        help_text="Which form, list or campaign this file's consent came from.",
    )
    # Which CSV header the importer decided was which field, worked out from
    # the file itself (see mapping.detect_columns). Stored so an import that
    # went wrong can be explained afterwards -- "it put your order numbers in
    # the name column" is only answerable if the guess was written down.
    column_mapping = models.JSONField(default=dict, blank=True)

    total_rows = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Import #{self.pk} ({self.status})"


class ContactSegment(models.Model):
    """A saved, reusable audience filter (e.g. City = Mumbai AND Tag = VIP)."""

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="contact_segments")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="contact_segments")
    name = models.CharField(max_length=100)
    filters = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="unique_contact_segment_per_tenant")]
        ordering = ["name"]

    def __str__(self):
        return self.name
