from django.db import models
from django.utils.text import slugify


class Tenant(models.Model):
    """An isolated business scope in the MultiSender hierarchy."""

    class Kind(models.TextChoices):
        PLATFORM = "PLATFORM", "Platform"
        RESELLER = "RESELLER", "Reseller"
        CUSTOMER = "CUSTOMER", "Customer"

    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=160, unique=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="children", null=True, blank=True
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["parent", "name"], name="unique_tenant_name_per_parent"),
        ]
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def is_descendant_of(self, tenant):
        current = self.parent
        while current is not None:
            if current.pk == tenant.pk:
                return True
            current = current.parent
        return False

    def __str__(self):
        return self.name


class TenantBranding(models.Model):
    """
    How the product presents itself to one tenant's users.

    Every field may be left blank, and blank means *inherit* rather than
    "empty": a reseller sets their brand once and each customer they create
    below them shows it, without anyone copying values downwards. Resolution
    walks up `Tenant.parent` and falls back to the platform's own defaults, so
    there is always something to render -- see `resolve` below.

    Kept as its own row rather than columns on Tenant because it is read on
    every page load by every user, and written about once in a tenant's life.
    """

    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="branding")

    product_name = models.CharField(max_length=60, blank=True)
    logo_url = models.URLField(blank=True)
    primary_color = models.CharField(
        max_length=7, blank=True, help_text="Hex like #0F766E. The whole accent ramp is derived from it."
    )
    support_email = models.EmailField(blank=True)

    # Where this brand is served. Unique so one host cannot resolve to two
    # brands; blank is stored as NULL precisely so many tenants may have none.
    custom_domain = models.CharField(max_length=255, blank=True, null=True, unique=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # What every chain ends at, when nobody above has set anything.
    DEFAULTS = {
        "product_name": "MultiSender",
        "logo_url": "",
        "primary_color": "#059669",
        "support_email": "",
    }
    INHERITED_FIELDS = ("product_name", "logo_url", "primary_color", "support_email")

    def __str__(self):
        return f"Branding for tenant {self.tenant_id}"

    def save(self, *args, **kwargs):
        # "" and NULL both mean "no custom domain", but only NULL can repeat
        # under a unique constraint.
        if not self.custom_domain:
            self.custom_domain = None
        else:
            self.custom_domain = self.custom_domain.strip().lower()
        super().save(*args, **kwargs)

    @classmethod
    def resolve(cls, tenant) -> dict:
        """
        The branding a tenant's users should actually see.

        Each field is taken from the nearest ancestor that set it, so a
        customer can override their reseller's product name while still
        inheriting their colour. `depth` is bounded rather than trusted: a
        parent cycle introduced by bad data must not hang a page load.
        """
        resolved = dict(cls.DEFAULTS)
        remaining = set(cls.INHERITED_FIELDS)

        current = tenant
        seen = set()
        while current is not None and remaining and current.pk not in seen:
            seen.add(current.pk)
            branding = getattr(current, "branding", None)
            if branding is not None:
                for field in list(remaining):
                    value = getattr(branding, field, "")
                    if value:
                        resolved[field] = value
                        remaining.discard(field)
            current = current.parent

        return resolved
