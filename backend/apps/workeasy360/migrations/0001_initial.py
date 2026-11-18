import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("tenants", "0005_tenantsettings"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrganizationLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("organization_id", models.UUIDField(unique=True)),
                (
                    "origin",
                    models.CharField(
                        choices=[
                            ("first_sign_in", "Created by the organization's first sign-in"),
                            ("operator", "Linked by an operator"),
                        ],
                        max_length=32,
                    ),
                ),
                ("linked_by", models.CharField(blank=True, max_length=150)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "tenant",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="workeasy360_link",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "verbose_name": "WorkEasy360 organization",
                "verbose_name_plural": "WorkEasy360 organizations",
            },
        ),
    ]
