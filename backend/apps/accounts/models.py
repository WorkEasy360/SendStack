from django.contrib.auth.models import AbstractUser, UserManager as BaseUserManager
from django.conf import settings
from django.db import models


PLATFORM_TENANT_SLUG = "multisender-platform"


class UserManager(BaseUserManager):
    """Keeps ``createsuperuser`` from minting tenant-less accounts.

    Every tenant-scoped endpoint dereferences ``request.user.tenant``, so a
    superuser created without one gets a 500 on the first write. The prompt has
    no tenant question, so anchor new superusers to the platform tenant -- the
    same one apps/accounts/migrations/0002_user_tenant.py backfills onto.
    """

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        from apps.tenants.models import Tenant

        extra_fields.setdefault("role", User.Role.SUPERADMIN)
        if not extra_fields.get("tenant") and not extra_fields.get("tenant_id"):
            extra_fields["tenant"], _ = Tenant.objects.get_or_create(
                slug=PLATFORM_TENANT_SLUG,
                defaults={"name": "MultiSender Platform", "kind": Tenant.Kind.PLATFORM},
            )
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):

    class Role(models.TextChoices):
        SUPERADMIN = "SUPERADMIN", "Super Admin"
        RESELLER = "RESELLER", "Reseller"
        USER = "USER", "User"

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.USER,
    )

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="users",
        null=True,
        blank=True,
        help_text="Tenant that owns this user account.",
    )

    mobile = models.CharField(
        max_length=20,
        blank=True,
        null=True,
    )

    credits = models.PositiveIntegerField(
        default=0
    )

    reserved_credits = models.PositiveIntegerField(
        default=0,
        help_text="Credits held against in-flight campaigns; not spendable again until released or consumed.",
    )

    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    objects = UserManager()

    def __str__(self):
        return f"{self.username} ({self.role})"

    def can_manage(self, other_user):
        """Return whether this user can administer ``other_user``."""
        if not self.is_active or not other_user.is_active:
            return False
        if self.role == self.Role.SUPERADMIN:
            return True
        return (
            self.role == self.Role.RESELLER
            and other_user.role == self.Role.USER
            and self.tenant_id
            and other_user.tenant_id
            and other_user.tenant.parent_id == self.tenant_id
        )


class ImpersonationSession(models.Model):
    """Explicit, time-limited delegation from an administrator to a lower-level user."""

    original_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="started_impersonations"
    )
    impersonated_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="received_impersonations"
    )
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    reason = models.CharField(max_length=500)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    refresh_jti = models.CharField(max_length=255, unique=True)

    class Meta:
        indexes = [models.Index(fields=["original_user", "ended_at"], name="accounts_im_origina_a98e75_idx")]

    @property
    def is_active(self):
        from django.utils import timezone

        return self.ended_at is None and self.expires_at > timezone.now()
