from django.contrib import admin

from .models import Checkout, Coupon, CouponRedemption, Payment, WebhookEvent


@admin.register(Checkout)
class CheckoutAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "plan", "amount_minor", "currency", "status", "created_at")
    list_filter = ("status", "provider", "currency")
    search_fields = ("provider_order_id",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "provider_payment_id", "amount_minor", "currency", "status", "captured_at")
    list_filter = ("status", "provider", "currency")
    search_fields = ("provider_payment_id",)
    readonly_fields = ("created_at",)


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("id", "provider", "event_type", "status", "tenant", "received_at", "processed_at")
    list_filter = ("status", "provider", "event_type")
    search_fields = ("event_id",)
    readonly_fields = ("received_at", "processed_at", "payload")


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    """
    Where a promotion is created, which is the only place it can be.

    `redemptions_used` is a computed column rather than a stored counter on
    purpose. A counter has to be kept in step with three transitions and is
    wrong the first time one of them is missed; counting the rows that
    actually hold a slot cannot drift from what the limit check does, because
    it is the same query.
    """

    list_display = (
        "code", "kind", "value", "owner", "is_active",
        "redemptions_used", "max_redemptions", "starts_at", "expires_at",
    )
    list_filter = ("kind", "is_active", "owner")
    search_fields = ("code", "description")
    filter_horizontal = ("plans",)
    readonly_fields = ("created_at", "updated_at", "redemptions_used")

    @admin.display(description="Redeemed")
    def redemptions_used(self, obj):
        if obj.pk is None:
            return 0
        return obj.redemptions.filter(status__in=CouponRedemption.COUNTED).count()


@admin.register(CouponRedemption)
class CouponRedemptionAdmin(admin.ModelAdmin):
    """
    Read-only. A redemption is evidence of what a customer was charged, and
    editing one would rewrite that after the fact -- the ledger equivalent of
    changing a receipt. Releasing a stuck reservation is
    `expire_stale_checkouts`, which does it through the checkout that owns it.
    """

    list_display = ("coupon", "tenant", "checkout", "status", "discount_minor", "trial_days", "created_at")
    list_filter = ("status", "coupon")
    readonly_fields = (
        "coupon", "tenant", "checkout", "discount_minor", "trial_days",
        "status", "created_at", "settled_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
