import re

from rest_framework import serializers

from .models import BusinessProfile, TenantBranding

_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class TenantBrandingSerializer(serializers.ModelSerializer):
    """
    What this tenant has set for itself.

    Blank is a meaningful value here -- it clears an override and hands the
    field back to whoever is above -- so nothing is coerced to a default on
    the way in. `effective` shows what that actually resolves to, which is
    the only way to tell "inherited teal" from "chose teal".
    """

    effective = serializers.SerializerMethodField()

    class Meta:
        model = TenantBranding
        fields = (
            "product_name", "logo_url", "primary_color", "support_email",
            "custom_domain", "effective", "updated_at",
        )
        read_only_fields = ("effective", "updated_at")

    def get_effective(self, obj):
        return TenantBranding.resolve(obj.tenant)

    def validate_primary_color(self, value):
        if value and not _HEX_COLOR.match(value):
            raise serializers.ValidationError("Use a hex colour like #0F766E.")
        return value.lower() if value else value

    def validate_custom_domain(self, value):
        if not value:
            return value
        cleaned = value.strip().lower()
        # A host, not a URL: pasting the address bar is the obvious mistake and
        # would never match the Host header we compare it against.
        cleaned = re.sub(r"^https?://", "", cleaned).split("/")[0]
        if not cleaned or " " in cleaned:
            raise serializers.ValidationError("Use a bare hostname like app.example.com.")
        return cleaned


class BusinessProfileSerializer(serializers.ModelSerializer):
    """
    The business's own account of itself, as the agents read it.

    `preview` is the whole point of exposing this rather than just the fields:
    a user filling these boxes in is really writing part of an AI prompt, and
    the only honest way to show what they are writing is to show the rendered
    text the model will actually be given.
    """

    preview = serializers.SerializerMethodField()
    is_configured = serializers.BooleanField(read_only=True)
    # `display_name` is a method on the model; DRF calls simple callables when
    # it resolves an attribute, so this needs no source of its own.
    display_name = serializers.CharField(read_only=True)

    class Meta:
        model = BusinessProfile
        fields = (
            "trading_name", "description", "products", "services", "locations",
            "contact_details", "policies", "escalation_rules", "languages", "tone",
            "target_customers", "preview", "is_configured", "display_name", "updated_at",
        )
        read_only_fields = ("preview", "is_configured", "display_name", "updated_at")

    def get_preview(self, obj):
        return obj.to_prompt()
