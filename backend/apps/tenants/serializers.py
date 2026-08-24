import re

from rest_framework import serializers

from .models import TenantBranding

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
