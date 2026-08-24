from django.db import migrations


def attach_tenantless_users_to_platform(apps, schema_editor):
    """Re-run 0002's backfill for accounts created after it (e.g. `createsuperuser`).

    A tenant-less user 500s on every tenant-scoped write; accounts.UserManager
    now prevents new ones, this catches the ones already in the database.
    """
    Tenant = apps.get_model("tenants", "Tenant")
    User = apps.get_model("accounts", "User")
    platform, _ = Tenant.objects.get_or_create(
        slug="multisender-platform",
        defaults={"name": "MultiSender Platform", "kind": "PLATFORM"},
    )
    User.objects.filter(tenant__isnull=True).update(tenant=platform)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0007_user_deleted_at_user_is_deleted")]

    operations = [migrations.RunPython(attach_tenantless_users_to_platform, migrations.RunPython.noop)]
