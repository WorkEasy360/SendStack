from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Tenant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=150)),
                ("slug", models.SlugField(max_length=160, unique=True)),
                ("kind", models.CharField(choices=[("PLATFORM", "Platform"), ("RESELLER", "Reseller"), ("CUSTOMER", "Customer")], max_length=16)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("parent", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="children", to="tenants.tenant")),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddConstraint(
            model_name="tenant",
            constraint=models.UniqueConstraint(fields=("parent", "name"), name="unique_tenant_name_per_parent"),
        ),
    ]
