from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("accounts", "0004_impersonationsession")]

    operations = [
        migrations.RenameIndex(
            model_name="impersonationsession",
            old_name="accounts_im_original_369ec8_idx",
            new_name="accounts_im_origina_a98e75_idx",
        )
    ]
