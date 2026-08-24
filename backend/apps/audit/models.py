from django.conf import settings
from django.db import models


class AuditEvent(models.Model):
    """Append-only record of security-sensitive application activity."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_events", 
    )
    acting_as = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="impersonated_audit_events",
    )
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.PROTECT, related_name="audit_events")
    action = models.CharField(max_length=100)
    object_type = models.CharField(max_length=100, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"], name="audit_audit_tenant__eab6aa_idx"),
            models.Index(fields=["actor", "-created_at"], name="audit_audit_actor_i_fb4253_idx"),
            models.Index(fields=["action", "-created_at"], name="audit_audit_action_9e2202_idx"),
        ]

    def __str__(self):
        return f"{self.action} at {self.created_at:%Y-%m-%d %H:%M:%S}"
