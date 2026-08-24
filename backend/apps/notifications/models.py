from django.conf import settings
from django.db import models


class Notification(models.Model):
    """
    Something one user needs to see, addressed to them by name.

    Deliberately thin: a kind, a line of text, and where to go. Anything
    richer would have to be kept in step with the object it describes, and a
    notification that contradicts the thing it points at is worse than a
    notification that simply links to it.
    """

    class Kind(models.TextChoices):
        MENTION = "MENTION", "Mentioned in a note"
        ASSIGNED = "ASSIGNED", "Conversation assigned"
        SLA_BREACH = "SLA_BREACH", "Reply overdue"
        SYSTEM = "SYSTEM", "System"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="notifications")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.SYSTEM)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    # A path within the app ("/inbox?conversation=12"), not a full URL: the
    # same row is correct whatever domain the tenant reaches the app on.
    link = models.CharField(max_length=500, blank=True)

    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "read_at"])]

    def __str__(self):
        return f"{self.kind} for {self.user_id}: {self.title}"
