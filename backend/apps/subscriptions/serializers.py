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

    def validate_name(self, value):
        name = (value or "").strip()
        if not name:
            raise serializers.ValidationError("Give the plan a name.")
        return name

    def validate_currency(self, value):
        # Three letters, upper-cased for the caller. The price is rendered
        # with Intl.NumberFormat in the browser, which throws on anything that
        # is not a currency code -- and a plan nobody can render is a plan
        # nobody can sell.
        code = (value or "").strip().upper()
        if len(code) != 3 or not code.isalpha():
            raise serializers.ValidationError("Use a three-letter currency code, like USD.")
        return code

    #: A ceiling nobody could reach is the same as no ceiling and is almost
    #: always a typo -- an extra zero on a form. Refused rather than stored,
    #: because 0 already means unlimited and is right there.
    MAX_LIMIT = 100_000_000
    #: 10 million minor units. Well past any real subscription price, and low
    #: enough that a slipped decimal point is caught.
    MAX_PRICE_CENTS = 1_000_000_000

    def validate(self, attrs):
        # PositiveIntegerField already refuses negatives at the database, and
        # DRF refuses them at the field. What is left to check is the other
        # end and the combinations, which nothing else looks at.
        for field in ("max_messages_per_month", "max_contacts", "max_devices", "max_users"):
            value = attrs.get(field)
            if value is not None and value > self.MAX_LIMIT:
                raise serializers.ValidationError(
                    {field: f"That is higher than any real limit. Use 0 for unlimited."}
                )
        price = attrs.get("price_cents")
        if price is not None and price > self.MAX_PRICE_CENTS:
            raise serializers.ValidationError({"price_cents": "That price looks like a mistake."})
        return attrs


class SubscriptionSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source="plan.name", read_only=True)
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)
    #: Derived, never stored: FULL / GRACE / EXPIRED. `status` says what
    #: somebody set; this says what it currently means.
    access = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = (
            "id", "tenant", "tenant_name", "plan", "plan_name", "status", "access",
            "current_period_start", "current_period_end", "cancelled_at", "created_at",
        )
        read_only_fields = ("id", "tenant_name", "plan_name", "access", "cancelled_at", "created_at")

    def get_access(self, obj):
        return obj.access_state()

    def validate(self, attrs):
        start = attrs.get("current_period_start") or getattr(self.instance, "current_period_start", None)
        end = attrs.get("current_period_end") or getattr(self.instance, "current_period_end", None)
        if start and end and end <= start:
            raise serializers.ValidationError(
                {"current_period_end": "The period has to end after it starts."}
            )
        return attrs
