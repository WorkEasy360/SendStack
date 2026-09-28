from django.db import migrations, models
import django.db.models.deletion


def assign_existing_users_to_platform(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    User = apps.get_model("accounts", "User")
    platform, _ = Tenant.objects.get_or_create(
        slug="multisender-platform",
        defaults={"name": "SendStack Platform", "kind": "PLATFORM"},
    )
    User.objects.filter(tenant__isnull=True).update(tenant=platform)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial"), ("tenants", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="tenant",
            field=models.ForeignKey(blank=True, help_text="Tenant that owns this user account.", null=True, on_delete=django.db.models.deletion.PROTECT, related_name="users", to="tenants.tenant"),
        ),
        migrations.RunPython(assign_existing_users_to_platform, migrations.RunPython.noop),
    ]
