import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """
    The numbered-list fallback for connections that cannot send real buttons.

    `interaction_type` is blank rather than defaulted to NATIVE_BUTTON on the
    existing rows. Backfilling it would assert that every historic tap was a
    native one, and on a QR-linked number the opposite is true -- those
    messages were sent, delivered, and never rendered a button at all.
    """

    dependencies = [
        ("messaging", "0010_message_template"),
        ("contacts", "0006_backfill_crm_message_stamps"),
        ("inbox", "0002_conversation_awaiting_reply_since_and_more"),
        ("tenants", "0003_businessprofile"),
        ("whatsapp", "0005_sendingpolicy_accounthealthevent_accountsendquota"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="interaction_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("NATIVE_BUTTON", "Tapped a button"),
                    ("NUMBERED_FALLBACK", "Replied with the option number"),
                    ("TEXT_OPTION", "Replied with the option's wording"),
                ],
                default="",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="PendingButtonPrompt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("options", models.JSONField(blank=True, default=list)),
                ("expires_at", models.DateTimeField()),
                ("answered_at", models.DateTimeField(blank=True, null=True)),
                ("resolved_button_id", models.CharField(blank=True, max_length=256)),
                ("reprompt_count", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "contact",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="+", to="contacts.contact",
                    ),
                ),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pending_button_prompts",
                        to="inbox.conversation",
                    ),
                ),
                (
                    "message",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="messaging.message",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pending_button_prompts",
                        to="tenants.tenant",
                    ),
                ),
                (
                    "whatsapp_account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="whatsapp.whatsappaccount",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["conversation", "answered_at", "-created_at"],
                        name="messaging_p_convers_de9b91_idx",
                    ),
                    models.Index(fields=["tenant", "-created_at"], name="messaging_p_tenant__e8449d_idx"),
                ],
            },
        ),
    ]
