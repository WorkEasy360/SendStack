"""
Which SendStack workspace a WorkEasy360 organization signs in to.

A WorkEasy360 ID token names an organization (``organization_id``, a UUID
that is the same under every issuer name). SendStack's own unit of isolation
is the ``Tenant``. This table is the one place the two meet: one
organization, one workspace, and never the other way round.

The people themselves are linked by the connector on ``(issuer, sub)``
(``workeasy360_sso.FederatedIdentity``); this table says nothing about them.
"""

from django.db import models


class OrganizationLink(models.Model):
    class Origin(models.TextChoices):
        FIRST_SIGN_IN = "first_sign_in", "Created by the organization's first sign-in"
        OPERATOR = "operator", "Linked by an operator"

    #: The WorkEasy360 organization. Unique: one organization is one workspace.
    organization_id = models.UUIDField(unique=True)
    #: Unique: one workspace answers to one organization. PROTECT, so deleting
    #: the workspace cannot silently orphan the organization's sign-ins.
    tenant = models.OneToOneField(
        "tenants.Tenant", on_delete=models.PROTECT, related_name="workeasy360_link"
    )
    origin = models.CharField(max_length=32, choices=Origin.choices)
    #: Who linked it: "first sign-in" or the operator's own words (--actor).
    linked_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "WorkEasy360 organization"
        verbose_name_plural = "WorkEasy360 organizations"

    def __str__(self):
        return f"{self.organization_id} -> tenant {self.tenant_id}"
