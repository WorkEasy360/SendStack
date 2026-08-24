from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_promote_existing_superusers")]

    operations = [
        migrations.CreateModel(
            name="ImpersonationSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField()),
                ("reason", models.CharField(max_length=500)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.TextField(blank=True)),
                ("refresh_jti", models.CharField(max_length=255, unique=True)),
                ("impersonated_user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="received_impersonations", to=settings.AUTH_USER_MODEL)),
                ("original_user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="started_impersonations", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddIndex(model_name="impersonationsession", index=models.Index(fields=["original_user", "ended_at"], name="accounts_im_original_369ec8_idx")),
    ]
