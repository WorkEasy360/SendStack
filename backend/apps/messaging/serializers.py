from rest_framework import serializers

from .models import Message


class MessageSerializer(serializers.ModelSerializer):
    # Denormalised for campaign reporting: the recipients table needs to show
    # which device sent each message without a second request per row.
    whatsapp_account_name = serializers.CharField(source="whatsapp_account.name", read_only=True, default="")
    whatsapp_account_phone = serializers.CharField(source="whatsapp_account.phone_number", read_only=True, default="")
    contact_name = serializers.CharField(source="contact.name", read_only=True, default="")

    class Meta:
        model = Message
        fields = (
            "id", "campaign", "conversation", "template", "whatsapp_account", "whatsapp_account_name", "whatsapp_account_phone",
            "contact", "contact_name", "direction", "is_automated", "to_phone",
            "message_type", "body", "media_url", "media_items", "caption", "filename", "buttons",
            "kind",
            "replied_button_id", "status", "provider_message_id",
            "error_message", "retry_count", "assigned_delay_seconds", "queued_at", "sent_at", "delivered_at",
            "read_at", "failed_at", "updated_at",
        )
        read_only_fields = fields
