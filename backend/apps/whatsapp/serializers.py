from rest_framework import serializers

from .models import AccountHealthEvent, SendingPolicy, WhatsAppAccount

# account_type -> the only connection_method it may use. Keeps "Normal
# WhatsApp"/"WhatsApp Business App" on QR device-linking and "WhatsApp API"
# on Cloud API credentials -- the frontend wizard only ever sends a matching
# pair, but this is re-validated server-side since it drives which provider
# handles the account.
_ALLOWED_CONNECTION_METHOD = {
    WhatsAppAccount.AccountType.WHATSAPP: WhatsAppAccount.ConnectionMethod.QR,
    WhatsAppAccount.AccountType.BUSINESS: WhatsAppAccount.ConnectionMethod.QR,
    WhatsAppAccount.AccountType.API: WhatsAppAccount.ConnectionMethod.CLOUD_API,
}


def _ACTIVE_CAMPAIGN_STATUSES(campaign_model):
    """Statuses that mean a campaign still has messages left to send."""
    return [
        campaign_model.Status.SCHEDULED,
        campaign_model.Status.QUEUED,
        campaign_model.Status.RUNNING,
        campaign_model.Status.PAUSED,
    ]


class WhatsAppAccountSerializer(serializers.ModelSerializer):
    access_token = serializers.CharField(write_only=True, required=False, allow_blank=True)
    app_secret = serializers.CharField(write_only=True, required=False, allow_blank=True)
    has_access_token = serializers.SerializerMethodField()
    # Campaign usage: how many campaigns are currently mid-flight on this
    # account, so the devices page can warn before disconnecting/deleting one
    # that is actively sending. Reads an annotation added by the viewset
    # (see get_queryset) and falls back to a direct count for any other
    # caller that serialises a plain instance.
    active_campaign_count = serializers.SerializerMethodField()

    class Meta:
        model = WhatsAppAccount
        fields = [
            "id",
            "name",
            "phone_number",
            "account_type",
            "connection_method",
            "provider",
            "status",
            "error_message",
            "phone_number_id",
            "whatsapp_business_account_id",
            "webhook_verify_token",
            "access_token",
            "app_secret",
            "has_access_token",
            "provider_account_id",
            "active_campaign_count",
            "last_connected_at",
            "last_seen_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "phone_number",
            "provider",
            "status",
            "error_message",
            "provider_account_id",
            "active_campaign_count",
            "last_connected_at",
            "last_seen_at",
            "created_at",
            "updated_at",
        ]

    def get_has_access_token(self, obj):
        return bool(obj.access_token_encrypted)

    def get_active_campaign_count(self, obj):
        annotated = getattr(obj, "active_campaign_count", None)
        if annotated is not None:
            return annotated
        from apps.campaigns.models import Campaign

        return obj.campaigns.filter(status__in=_ACTIVE_CAMPAIGN_STATUSES(Campaign)).count()

    def validate(self, attrs):
        account_type = attrs.get("account_type", getattr(self.instance, "account_type", None))
        connection_method = attrs.get("connection_method", getattr(self.instance, "connection_method", None))

        expected_method = _ALLOWED_CONNECTION_METHOD.get(account_type)
        if expected_method is not None and connection_method != expected_method:
            raise serializers.ValidationError(
                {
                    "connection_method": (
                        f"Account type '{account_type}' must use connection_method='{expected_method}'."
                    )
                }
            )

        if connection_method == WhatsAppAccount.ConnectionMethod.CLOUD_API and self.instance is None:
            phone_number_id = attrs.get("phone_number_id")
            access_token = attrs.get("access_token")
            app_secret = attrs.get("app_secret")
            missing = [
                field
                for field, value in [
                    ("phone_number_id", phone_number_id),
                    ("access_token", access_token),
                    ("app_secret", app_secret),
                ]
                if not value
            ]
            if missing:
                raise serializers.ValidationError(
                    {field: "This field is required to connect via WhatsApp API." for field in missing}
                )

        return attrs

    def create(self, validated_data):
        access_token = validated_data.pop("access_token", None)
        app_secret = validated_data.pop("app_secret", None)
        instance = WhatsAppAccount(**validated_data)
        if access_token:
            instance.set_access_token(access_token)
        if app_secret:
            instance.set_app_secret(app_secret)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        access_token = validated_data.pop("access_token", None)
        app_secret = validated_data.pop("app_secret", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if access_token:
            instance.set_access_token(access_token)
        if app_secret:
            instance.set_app_secret(app_secret)
        instance.save()
        return instance


class SendingPolicySerializer(serializers.ModelSerializer):
    """
    The knobs a customer may turn on one number.

    `is_paused` is writable so the devices page can pause and resume a number,
    but the surrounding fields (why, since when, until when) are not: those are
    written by whatever made the decision, and letting a form overwrite them
    would leave a pause whose stated reason is not the real one.
    """

    class Meta:
        model = SendingPolicy
        fields = [
            "daily_limit", "hourly_limit", "warmup_enabled", "warmup_started_on",
            "auto_pause_enabled", "is_paused", "paused_at", "paused_until", "pause_reason",
            "updated_at",
        ]
        read_only_fields = ["warmup_started_on", "paused_at", "paused_until", "pause_reason", "updated_at"]

    def validate_daily_limit(self, value):
        if value is not None and value < 1:
            raise serializers.ValidationError("Leave this blank for no limit, or set at least 1.")
        return value

    def validate_hourly_limit(self, value):
        if value is not None and value < 1:
            raise serializers.ValidationError("Leave this blank for no limit, or set at least 1.")
        return value

    def validate(self, attrs):
        daily = attrs.get("daily_limit", getattr(self.instance, "daily_limit", None))
        hourly = attrs.get("hourly_limit", getattr(self.instance, "hourly_limit", None))
        if daily is not None and hourly is not None and hourly > daily:
            raise serializers.ValidationError({
                "hourly_limit": "An hourly limit above the daily one can never be reached.",
            })
        return attrs


class AccountHealthEventSerializer(serializers.ModelSerializer):
    account_name = serializers.CharField(source="account.name", read_only=True)

    class Meta:
        model = AccountHealthEvent
        fields = ["id", "account", "account_name", "event", "severity", "detail", "created_at"]
        read_only_fields = fields
