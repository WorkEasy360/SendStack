from django.conf import settings
from django.db import models


class TrackedLink(models.Model):
    """
    One shortened URL, belonging to one recipient.

    Attribution is the whole point, so a link is *not* shared between
    recipients: a campaign to 5,000 contacts with one link in the body creates
    5,000 rows. That is what makes "who clicked" answerable at all, and it is
    what every ad platform does for the same reason. The rows are small and
    write-once; the cost is disk, and the alternative is a click counter that
    cannot tell you anything you would act on.

    `original_url` is what the tenant typed. `target_url` is where the
    redirect actually goes -- the same URL with UTM parameters merged in --
    and is kept separately so that turning UTMs off later does not rewrite
    history for links already sent.
    """

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="tracked_links")
    campaign = models.ForeignKey(
        "campaigns.Campaign", on_delete=models.CASCADE, null=True, blank=True, related_name="tracked_links"
    )
    # SET_NULL rather than CASCADE: deleting a conversation must not silently
    # destroy the click history that a report has already counted.
    message = models.ForeignKey(
        "messaging.Message", on_delete=models.SET_NULL, null=True, blank=True, related_name="tracked_links"
    )
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.SET_NULL, null=True, blank=True, related_name="tracked_links"
    )

    code = models.CharField(max_length=16, unique=True)
    original_url = models.TextField()
    target_url = models.TextField()

    click_count = models.PositiveIntegerField(default=0)
    first_clicked_at = models.DateTimeField(null=True, blank=True)
    last_clicked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["campaign", "contact"]),
            models.Index(fields=["tenant", "-created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"/{self.code} -> {self.original_url[:60]}"


class LinkClick(models.Model):
    """
    One tap, appended and never updated.

    Kept as individual rows rather than only a counter on TrackedLink because
    "clicked twice an hour apart" and "clicked twice in the same second" mean
    different things, and because a retargeting audience is built by joining
    these rows to contacts.
    """

    link = models.ForeignKey(TrackedLink, on_delete=models.CASCADE, related_name="clicks")
    clicked_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    referer = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["link", "-clicked_at"])]
        ordering = ["-clicked_at"]

    def __str__(self):
        return f"Click on /{self.link_id} at {self.clicked_at}"
