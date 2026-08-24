from rest_framework import serializers

from .models import LinkClick, TrackedLink


class LinkClickSerializer(serializers.ModelSerializer):
    class Meta:
        model = LinkClick
        fields = ["id", "clicked_at", "ip_address", "user_agent", "referer"]


class TrackedLinkSerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source="contact.name", read_only=True, default="")
    contact_phone = serializers.CharField(source="contact.phone", read_only=True, default="")
    campaign_name = serializers.CharField(source="campaign.name", read_only=True, default="")

    class Meta:
        model = TrackedLink
        fields = [
            "id", "code", "original_url", "target_url", "campaign", "campaign_name",
            "contact", "contact_name", "contact_phone", "message",
            "click_count", "first_clicked_at", "last_clicked_at", "created_at",
        ]
        read_only_fields = fields
