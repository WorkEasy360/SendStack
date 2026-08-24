from django.db import migrations


def promote_existing_superusers(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(is_superuser=True).exclude(role="SUPERADMIN").update(role="SUPERADMIN")


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_user_tenant")]

    operations = [migrations.RunPython(promote_existing_superusers, migrations.RunPython.noop)]
