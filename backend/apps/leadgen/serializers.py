from rest_framework import serializers

from apps.whatsapp.models import WhatsAppAccount

from . import services
from .models import ClickToChatLink, LeadEvent


class ClickToChatLinkSerializer(serializers.ModelSerializer):
    short_url = serializers.SerializerMethodField()
    whatsapp_url = serializers.SerializerMethodField()
    prefilled_preview = serializers.SerializerMethodField()
    account_name = serializers.CharField(source="whatsapp_account.name", read_only=True)
    account_phone = serializers.CharField(source="whatsapp_account.phone_number", read_only=True)

    class Meta:
        model = ClickToChatLink
        fields = (
            "id", "name", "code", "whatsapp_account", "account_name", "account_phone",
            "prefilled_message", "prefilled_preview", "include_reference",
            "utm_source", "utm_medium", "utm_campaign", "is_active",
            "click_count", "lead_count", "short_url", "whatsapp_url", "created_at", "updated_at",
        )
        read_only_fields = ("id", "code", "click_count", "lead_count", "created_at", "updated_at")

    def get_short_url(self, obj):
        return services.short_url(obj)

    def get_whatsapp_url(self, obj):
        return services.whatsapp_url(obj)

    def get_prefilled_preview(self, obj):
        """Exactly what the customer will see in their message box, reference and all."""
        return services.prefilled_text(obj)

    def validate_whatsapp_account(self, value):
        if value.tenant_id != self.context["request"].user.tenant_id:
            raise serializers.ValidationError("That WhatsApp number does not belong to your tenant.")
        return value


class LeadEventSerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source="contact.name", read_only=True, default="")
    contact_phone = serializers.CharField(source="contact.phone", read_only=True, default="")
    link_name = serializers.CharField(source="link.name", read_only=True, default="")

    class Meta:
        model = LeadEvent
        fields = (
            "id", "kind", "contact", "contact_name", "contact_phone",
            "link", "link_name", "message", "payload", "created_at",
        )
        read_only_fields = fields
