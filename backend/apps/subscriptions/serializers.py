from rest_framework import serializers

from .models import Plan, Subscription


class PlanSerializer(serializers.ModelSerializer):
    subscriber_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Plan
        fields = (
            "id", "name", "description", "price_cents", "currency", "interval",
            "max_messages_per_month", "max_contacts", "max_devices", "max_users",
            "features", "is_active", "subscriber_count", "created_at", "updated_at",
        )
        read_only_fields = ("id", "subscriber_count", "created_at", "updated_at")

    def validate_features(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Features must be an object of switches.")
        unknown = set(value) - set(Plan.FEATURES)
        if unknown:
            raise serializers.ValidationError(f"Unknown feature(s): {', '.join(sorted(unknown))}.")
        return {key: bool(flag) for key, flag in value.items()}


class SubscriptionSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source="plan.name", read_only=True)
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)

    class Meta:
        model = Subscription
        fields = (
            "id", "tenant", "tenant_name", "plan", "plan_name", "status",
            "current_period_start", "current_period_end", "cancelled_at", "created_at",
        )
        read_only_fields = ("id", "tenant_name", "plan_name", "cancelled_at", "created_at")
