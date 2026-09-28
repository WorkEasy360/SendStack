"""
What billing objects look like on the wire.

The recurring decision in this file is what to leave out. A billing response
is read by a browser and often by a support screenshot, so the internal
identifiers a payment provider hands us -- the merchant account, the customer
id, the raw gateway status -- are not in it. What a customer needs is what
they are on, what it costs, and what happened; none of that requires exposing
the plumbing.
"""

from rest_framework import serializers

from apps.subscriptions.models import Plan

from .models import Checkout, Payment


class PublicPlanSerializer(serializers.ModelSerializer):
    """
    One plan as the pricing page and the upgrade screen render it.

    `limits` and `features` come from the model, so the comparison table is
    built from the same numbers the entitlement service enforces. A React
    component with the tiers hardcoded would eventually promise a limit the
    send gate does not honour, and the customer would find that out at the
    worst moment.
    """

    limits = serializers.SerializerMethodField()
    features = serializers.SerializerMethodField()
    price_minor = serializers.IntegerField(source="price_cents", read_only=True)

    class Meta:
        model = Plan
        fields = (
            "id", "name", "description", "price_minor", "currency", "interval",
            "display_order", "limits", "features",
        )
        read_only_fields = fields

    def get_limits(self, obj):
        # 0 means unlimited throughout this codebase; passed through as 0 and
        # labelled by the UI, rather than translated to None here -- one
        # convention, everywhere.
        return obj.limits

    def get_features(self, obj):
        from apps.subscriptions.services import feature_label

        return [
            {"key": key, "label": feature_label(key), "included": obj.allows(key)}
            for key in Plan.FEATURES
        ]


class CheckoutSerializer(serializers.ModelSerializer):
    """
    An order in progress.

    `public_params` is passed through because the browser genuinely needs it
    to open the provider's hosted page -- it holds a publishable key id and
    the order id. It is the payment adapter's job to have put nothing secret
    in it (see PaymentProvider.public_config), which is why that is one narrow
    method rather than a habit of forwarding provider responses.
    """

    plan_name = serializers.CharField(source="plan.name", read_only=True)
    public_params = serializers.SerializerMethodField()
    # The code that produced the discount on this order, so a customer can see
    # which promotion they got rather than an unexplained smaller number.
    # Read from the reservation rather than from the coupon table, so a code
    # deleted later still shows on the order it discounted.
    coupon_code = serializers.SerializerMethodField()

    class Meta:
        model = Checkout
        fields = (
            "id", "plan", "plan_name", "provider", "provider_order_id",
            "amount_minor", "list_amount_minor", "discount_minor", "coupon_code",
            "currency", "status", "expires_at", "created_at", "public_params",
        )
        read_only_fields = fields

    def get_public_params(self, obj):
        return (obj.metadata or {}).get("public_params", {})

    def get_coupon_code(self, obj):
        redemption = obj.coupon_redemptions.first()
        return redemption.coupon.code if redemption is not None else ""


class PaymentSerializer(serializers.ModelSerializer):
    """
    One payment, as a customer's own billing history.

    `provider_payment_id` is included on purpose: it is the reference somebody
    quotes to their bank or to support, and it identifies a payment they
    made. `failure_reason` is **not**: a gateway's decline text is often more
    specific about somebody's bank than a product should repeat back to a
    room they might be screen-sharing.
    """

    plan_name = serializers.CharField(source="checkout.plan.name", read_only=True)

    class Meta:
        model = Payment
        fields = (
            "id", "plan_name", "provider", "provider_payment_id",
            "amount_minor", "currency", "status", "captured_at", "created_at",
        )
        read_only_fields = fields
