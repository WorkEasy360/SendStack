from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('whatsapp', '0005_sendingpolicy_accounthealthevent_accountsendquota'),
    ]

    operations = [
        migrations.AddField(
            model_name='whatsappaccount',
            name='archived_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
