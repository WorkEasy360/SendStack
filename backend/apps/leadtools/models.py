"""
Lead tools: the number filter and the group grabber.

Both answer a question only a phone can answer -- "is this number on
WhatsApp?" and "who is in this group?" -- by asking it through one of the
workspace's QR-linked numbers. Neither exists on a Cloud API number, and each
is its own plan feature (see docs/ENTITLEMENTS.md).

What they are *not* is a source of consent. A number that turns out to be on
WhatsApp has not agreed to hear from anybody, and a group member has agreed
to be in a group, not to be marketed to. Everything these tools save goes
through the ordinary contact importer with consent left UNKNOWN, so the
consent gate in front of every campaign treats their output exactly as it
treats a pasted list -- which is the only honest way to treat it.
"""

from django.conf import settings
from django.db import models


class NumberCheck(models.Model):
    """
    One run of the number filter: a list of numbers, and what WhatsApp said
    about each.

    Kept as a record rather than answered inline because a run over ten
    thousand numbers takes minutes -- the phone is asked in paced batches so
    it does not look like the scraping WhatsApp bans numbers for -- and
    because "which of my list was reachable on the 3rd" is a question people
    come back to.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    class Source(models.TextChoices):
        PASTE = "PASTE", "Pasted list"
        CONTACTS = "CONTACTS", "Existing contacts"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="number_checks")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="number_checks",
    )
    # PROTECT: a check is evidence of what a particular number was asked;
    # deleting the number should not silently erase that.
    whatsapp_account = models.ForeignKey(
        "whatsapp.WhatsAppAccount", on_delete=models.PROTECT, related_name="number_checks",
    )

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    source = models.CharField(max_length=12, choices=Source.choices)
    # Only for Source.CONTACTS: which group was checked, or null for every contact.
    contact_group = models.ForeignKey(
        "contacts.ContactGroup", on_delete=models.SET_NULL, null=True, blank=True, related_name="number_checks",
    )

    total = models.PositiveIntegerField(default=0)
    checked = models.PositiveIntegerField(default=0)
    on_whatsapp_count = models.PositiveIntegerField(default=0)
    not_on_whatsapp_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=500, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "-created_at"])]

    def __str__(self):
        return f"Number check #{self.pk} ({self.status})"

    @property
    def progress(self) -> int:
        """0-100, for the screen's bar. A check with nothing in it is done."""
        if not self.total:
            return 100
        return min(100, round(100 * self.checked / self.total))


class NumberCheckEntry(models.Model):
    """One number in one check, and what WhatsApp said about it."""

    class Result(models.TextChoices):
        PENDING = "PENDING", "Not checked yet"
        ON_WHATSAPP = "ON_WHATSAPP", "On WhatsApp"
        NOT_ON_WHATSAPP = "NOT_ON_WHATSAPP", "Not on WhatsApp"
        ERROR = "ERROR", "Could not check"

    # `number_check`, not `check`: a field called `check` would shadow Django's own Model.check().
    number_check = models.ForeignKey(NumberCheck, on_delete=models.CASCADE, related_name="entries")
    #: E.164, with the "+", as contacts are stored.
    phone = models.CharField(max_length=16)
    #: The contact this number belonged to when the check was made, if any.
    #: SET_NULL: the check's answer stays valid after the contact is deleted.
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.SET_NULL, null=True, blank=True, related_name="number_check_entries",
    )
    result = models.CharField(max_length=16, choices=Result.choices, default=Result.PENDING)
    jid = models.CharField(max_length=64, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["pk"]
        constraints = [models.UniqueConstraint(fields=["number_check", "phone"], name="unique_phone_per_number_check")]
        indexes = [models.Index(fields=["number_check", "result"])]

    def __str__(self):
        return f"{self.phone}: {self.result}"


class GroupGrab(models.Model):
    """
    One pull of members out of one or more WhatsApp groups.

    The members themselves are not stored here: they become contacts through
    a ContactImport (linked below), which is what gives the grab the same
    quota, gap-filling and consent rules as every other way of adding people.
    This row records which groups were read, when, and by which number.
    """

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="group_grabs")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="group_grabs",
    )
    whatsapp_account = models.ForeignKey(
        "whatsapp.WhatsAppAccount", on_delete=models.PROTECT, related_name="group_grabs",
    )
    #: ``[{"id", "subject", "member_count", "hidden_count"}, ...]`` -- what
    #: was read, as it was at the time. Groups are renamed and left; the
    #: record should still say what it read.
    groups = models.JSONField(default=list, blank=True)

    contact_group = models.ForeignKey(
        "contacts.ContactGroup", on_delete=models.SET_NULL, null=True, blank=True, related_name="group_grabs",
    )
    contact_import = models.ForeignKey(
        "contacts.ContactImport", on_delete=models.SET_NULL, null=True, blank=True, related_name="group_grabs",
    )

    #: Distinct phone numbers handed to the importer.
    members_found = models.PositiveIntegerField(default=0)
    #: Members WhatsApp showed only by an anonymous id, with no number to import.
    members_hidden = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "-created_at"])]

    def __str__(self):
        return f"Group grab #{self.pk} ({len(self.groups)} groups)"
