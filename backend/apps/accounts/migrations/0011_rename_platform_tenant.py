from django.db import migrations


OLD_NAME = "MultiSender Platform"
NEW_NAME = "SendStack Platform"


def _rename(apps, old, new):
    """Rename the platform tenant on databases created before the rebrand.

    0002 and 0008 only set the name via `get_or_create` defaults, so a
    database that already ran them keeps the old display name forever. The
    slug is deliberately untouched -- it is the lookup key every one of those
    backfills matches on, and changing it would mint a second platform tenant
    and orphan the users attached to the first.
    """
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(slug="multisender-platform", name=old).update(name=new)


def forwards(apps, schema_editor):
    _rename(apps, OLD_NAME, NEW_NAME)


def backwards(apps, schema_editor):
    _rename(apps, NEW_NAME, OLD_NAME)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0010_user_marketing_opt_in"),
        ("tenants", "0004_accountdeletionrequest"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
