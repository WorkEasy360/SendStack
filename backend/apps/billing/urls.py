"""
Billing routes.

Mounted under the same ``/api/v1/billing/`` prefix as plans, subscriptions and
usage (apps.subscriptions.urls), with disjoint sub-paths -- a customer's
billing is one thing from the outside, whatever it is split into inside.

`pricing/` and `webhook/` are the only unauthenticated routes. The first is
public because a price list behind a login is not a price list; the second is
public because a payment provider has no credential of ours to present, and is
safe because it is signed.
"""

from django.urls import path

from .views import (
    BillingOverviewView,
    CheckoutStatusView,
    CheckoutVerifyView,
    CheckoutView,
    CouponPreviewView,
    PaymentHistoryView,
    PlanChangeView,
    PricingView,
    SubscriptionCancelView,
    TrialExtensionView,
    WebhookView,
)

urlpatterns = [
    path("pricing/", PricingView.as_view(), name="billing-pricing"),
    path("overview/", BillingOverviewView.as_view(), name="billing-overview"),
    path("checkout/", CheckoutView.as_view(), name="billing-checkout"),
    path("checkout/<int:pk>/verify/", CheckoutVerifyView.as_view(), name="billing-checkout-verify"),
    path("checkout/<int:pk>/status/", CheckoutStatusView.as_view(), name="billing-checkout-status"),
    path("cancel/", SubscriptionCancelView.as_view(), name="billing-cancel"),
    # A preview on GET, a downgrade on POST. An upgrade is a payment and goes
    # through checkout/ above -- see PlanChangeView.
    path("plan-change/", PlanChangeView.as_view(), name="billing-plan-change"),
    path("payments/", PaymentHistoryView.as_view(), name="billing-payments"),
    # Quoting a code writes nothing; redeeming a trial extension does. Two
    # paths because they are two different acts, not one with a flag.
    path("coupon/preview/", CouponPreviewView.as_view(), name="billing-coupon-preview"),
    path("coupon/trial-extension/", TrialExtensionView.as_view(), name="billing-trial-extension"),
    path("webhook/<str:provider>/", WebhookView.as_view(), name="billing-webhook"),
]
