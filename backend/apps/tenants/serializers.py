import re

from rest_framework import serializers

from .models import BusinessProfile, TenantBranding, TenantSettings

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


class TenantSettingsSerializer(serializers.ModelSerializer):
    """
    One workspace's sending and housekeeping preferences.

    `effective_*` are the delays that will actually be used once the
    deployment defaults are resolved, so the screen can show what a workspace
    that has set nothing will really do rather than two blank fields.
    """

    effective_min_delay_seconds = serializers.SerializerMethodField()
    effective_max_delay_seconds = serializers.SerializerMethodField()

    class Meta:
        model = TenantSettings
        fields = (
            "delay_mode", "min_delay_seconds", "max_delay_seconds",
            "effective_min_delay_seconds", "effective_max_delay_seconds",
            "batch_size", "batch_pause_seconds",
            "sleep_enabled", "sleep_after_messages", "sleep_min_seconds", "sleep_max_seconds",
            "retry_enabled", "max_retries", "retry_delay_seconds",
            "campaign_data_retention_days",
            "updated_at",
        )
        read_only_fields = ("effective_min_delay_seconds", "effective_max_delay_seconds", "updated_at")

    def get_effective_min_delay_seconds(self, obj):
        from apps.campaigns.assignment import resolve_tenant_delay_bounds

        return resolve_tenant_delay_bounds(obj)[0]

    def get_effective_max_delay_seconds(self, obj):
        from apps.campaigns.assignment import resolve_tenant_delay_bounds

        return resolve_tenant_delay_bounds(obj)[1]

    def validate(self, attrs):
        def current(field):
            if field in attrs:
                return attrs[field]
            return getattr(self.instance, field, None)

        minimum, maximum = current("min_delay_seconds"), current("max_delay_seconds")
        if minimum is not None and maximum is not None and maximum < minimum:
            raise serializers.ValidationError(
                {"max_delay_seconds": "The longest gap must be at least as long as the shortest."}
            )

        sleep_min, sleep_max = current("sleep_min_seconds"), current("sleep_max_seconds")
        if sleep_min is not None and sleep_max is not None and sleep_max < sleep_min:
            raise serializers.ValidationError(
                {"sleep_max_seconds": "The longest break must be at least as long as the shortest."}
            )

        if current("sleep_enabled") and not current("sleep_after_messages"):
            raise serializers.ValidationError(
                {"sleep_after_messages": "Say how many messages to send before taking a break."}
            )

        # Capped rather than rejected out of hand: MAX_SEND_RETRIES is the
        # worker's own ceiling, and a workspace asking for more would be
        # configuring something the task cannot honour.
        from apps.messaging.tasks import MAX_SEND_RETRIES

        retries = current("max_retries")
        if retries is not None and retries > MAX_SEND_RETRIES:
            raise serializers.ValidationError(
                {"max_retries": f"At most {MAX_SEND_RETRIES} retries are possible."}
            )

        return attrs
