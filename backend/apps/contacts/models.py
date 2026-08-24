from django.conf import settings
from django.db import models


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
    subscribed = models.BooleanField(default=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

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
    groups = models.ManyToManyField(ContactGroup, blank=True, related_name="contacts")
    tags = models.ManyToManyField(ContactTag, blank=True, related_name="contacts")
    last_message_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "phone"], name="unique_contact_phone_per_tenant")]
        indexes = [
            models.Index(fields=["tenant", "subscribed"]),
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
    return f"contact_imports/tenant_{instance.tenant_id}/{filename}"


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
