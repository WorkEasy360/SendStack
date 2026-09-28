"""
The billing endpoints: the public price list, the customer's own billing page,
checkout, and the provider webhook.

The tenant-scoping rule that runs through all of them: **every billing object
is reached through the caller's own tenant, never by id alone.** There is no
endpoint here that takes a subscription id, a checkout id or a payment id and
looks it up globally. `get_queryset`-style filtering on `request.user.tenant`
is what makes "customer A reads customer B's invoice" impossible rather than
merely unlikely.

The webhook is the exception to everything else in this file: unauthenticated,
because a payment provider holds no credential of ours, and safe for exactly
one reason -- it is signed, and the signature is checked over the raw body
before anything in it is read.
"""

import logging

from django.conf import settings
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.audit.services import log_event
from apps.subscriptions.models import Plan, Subscription, SubscriptionNotice

from . import coupons, plan_changes, services
from .models import Checkout, Payment
from .providers import PaymentError, PaymentNotConfigured, SignatureInvalid, payments_enabled
from .serializers import CheckoutSerializer, PaymentSerializer, PublicPlanSerializer

logger = logging.getLogger("sendstack")


class PricingView(APIView):
    """
    The public price list. Unauthenticated, and the backend is authoritative.

    Every number on the pricing page comes from here -- limits, features,
    price, interval -- so a plan changed in the database changes the marketing
    page without a deploy, and there is no second copy of the limits in React
    to drift out of step with what the send gate actually enforces.

    Only `is_public`, active, platform-owned plans are listed. A reseller's
    plans are their own commercial arrangement and are not published on the
    platform's pricing page.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "pricing"

    def get(self, request):
        from apps.accounts.registration import signup_enabled
        from apps.subscriptions import trials

        plans = Plan.objects.filter(owner__isnull=True, is_active=True, is_public=True).order_by(
            "display_order", "price_cents", "name",
        )
        trial_plan = trials.trial_plan()
        return Response({
            "currency": getattr(settings, "BILLING_CURRENCY", "INR"),
            "signup_enabled": signup_enabled(),
            "payments_enabled": payments_enabled(),
            "trials_enabled": trials.trials_enabled(),
            "trial_days": trials.trial_days() if trials.trials_enabled() else 0,
            "trial_plan": trial_plan.name if trial_plan else None,
            "features": [
                {"key": key, "label": _feature_label(key)} for key in Plan.FEATURES
            ],
            "plans": PublicPlanSerializer(plans, many=True).data,
        })


def _feature_label(key: str) -> str:
    from apps.subscriptions.services import feature_label

    return feature_label(key)


class BillingOverviewView(APIView):
    """This workspace's plan, status, period, usage and what it may buy next."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = request.user.tenant
        data = services.billing_state(tenant)
        data["available_plans"] = PublicPlanSerializer(
            services.purchasable_plans(tenant), many=True,
        ).data
        data["payments"] = PaymentSerializer(
            Payment.objects.filter(tenant=tenant).select_related("checkout__plan")[:20], many=True,
        ).data
        # Said plainly rather than implied by an empty list: this deployment
        # does not generate tax invoices, and a billing page that showed an
        # empty "Invoices" table would read as a bug or, worse, as a claim.
        data["invoices"] = {
            "available": False,
            "detail": (
                "Payment receipts are issued by the payment provider. This workspace does not "
                "generate tax invoices."
            ),
        }
        return Response(data)


class CheckoutView(APIView):
    """
    Start a payment for a plan.

    The body carries a `plan` id and, optionally, a coupon `code`. There is no
    amount field, no currency field, no discount field and no tenant field --
    the server resolves all of them (see services.create_checkout), which is
    what makes "pay ₹1 for the ₹999 plan" not a thing that can be attempted
    rather than a thing that is caught.

    The coupon is a **string to look up**, never a discount to apply. What it
    is worth is decided from the coupon row, and a code that has expired, is
    exhausted or does not apply to this plan fails the request rather than
    being ignored: somebody who typed a code and was charged full price has
    been overcharged as far as they are concerned.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            checkout = services.create_checkout(
                tenant=request.user.tenant,
                user=request.user,
                plan_id=request.data.get("plan"),
                coupon_code=(request.data.get("code") or "").strip(),
            )
        except PaymentNotConfigured as exc:
            # 503 rather than 400: nothing is wrong with what the customer
            # asked for, and "try again later" is the accurate advice.
            return Response(
                {"detail": str(exc), "code": "PAYMENTS_UNAVAILABLE"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except (services.BillingError, PaymentError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(CheckoutSerializer(checkout).data, status=status.HTTP_201_CREATED)


class CheckoutVerifyView(APIView):
    """
    Settle a checkout from the browser's return callback.

    **This endpoint does not trust its body.** What it receives is a claim
    that a payment happened; the provider adapter checks the signature and
    then asks the provider's own API what actually happened, and only that
    answer can activate a plan. A body saying ``payment_status: success``
    activates precisely nothing.

    The checkout is fetched **scoped to the caller's tenant**, so a customer
    cannot present a payment against somebody else's order.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        checkout = Checkout.objects.filter(pk=pk, tenant=request.user.tenant).first()
        if checkout is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            payment = services.verify_payment(checkout=checkout, payload=request.data)
        except SignatureInvalid as exc:
            log_event(
                request=request, action="billing.verification_failed", tenant=checkout.tenant,
                object_type="billing.Checkout", object_id=checkout.pk,
                metadata={"reason": "signature"},
            )
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except (services.BillingError, PaymentError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        checkout.refresh_from_db()
        return Response({
            "payment": PaymentSerializer(payment).data,
            "checkout": CheckoutSerializer(checkout).data,
            "billing": services.billing_state(request.user.tenant),
        })


class CheckoutStatusView(APIView):
    """
    What this checkout's state really is, asked of the server.

    The endpoint behind the "Verifying payment..." screen. A payment return URL
    carrying ``?payment=success`` is a **routing hint and nothing more**; the
    browser asks here, and here reads the database -- and, for a checkout that
    is still pending, goes and asks the provider directly. That last part is
    what closes the window where the money left the customer's account, the
    webhook was lost, and the tab was closed before the callback fired.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        checkout = Checkout.objects.filter(pk=pk, tenant=request.user.tenant).first()
        if checkout is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if checkout.is_open:
            try:
                services.reconcile_checkout(checkout)
                checkout.refresh_from_db()
            except Exception:  # noqa: BLE001 - a polling endpoint must still answer
                logger.exception("Could not reconcile checkout %s.", checkout.pk)

        return Response({
            "checkout": CheckoutSerializer(checkout).data,
            "billing": services.billing_state(request.user.tenant),
        })


class SubscriptionCancelView(APIView):
    """
    Cancel this workspace's own subscription.

    **Effective at the end of the period already paid for**, not immediately,
    and the response says so with a date. Taking access away the moment
    somebody cancels would be taking away something they have paid for; and a
    cancel button whose effect is not stated is the dark pattern this phase is
    explicitly not allowed to ship.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        subscription = Subscription.objects.select_related("plan").filter(
            tenant=request.user.tenant,
        ).first()
        if subscription is None:
            return Response(
                {"detail": "There is no subscription to cancel."}, status=status.HTTP_400_BAD_REQUEST,
            )
        if subscription.status == Subscription.Status.CANCELLED:
            return Response(
                {"detail": "This subscription is already cancelled.", "billing": services.billing_state(request.user.tenant)}
            )

        from django.utils import timezone

        # Read while it is still live, so this is the end of the window they
        # are currently in rather than one interval past a stale anchor.
        _, period_end = subscription.current_period()
        subscription.status = Subscription.Status.CANCELLED
        subscription.cancelled_at = timezone.now()
        subscription.current_period_end = period_end
        subscription.save(update_fields=["status", "cancelled_at", "current_period_end", "updated_at"])

        try:
            from .providers import get_provider

            get_provider().cancel_subscription(subscription=subscription)
        except PaymentNotConfigured:
            pass
        except Exception:  # noqa: BLE001 - the local cancellation stands regardless
            logger.exception("Could not tell the provider about subscription %s's cancellation.", subscription.pk)

        log_event(
            request=request, action="subscription.cancelled", tenant=subscription.tenant,
            object_type="subscription", object_id=subscription.pk,
            metadata={"plan": subscription.plan.name, "until": period_end.isoformat()},
        )
        if SubscriptionNotice.claim(
            subscription, SubscriptionNotice.Kind.SUBSCRIPTION_CANCELLED, period_end.date(),
        ):
            services.notify_workspace(subscription, SubscriptionNotice.Kind.SUBSCRIPTION_CANCELLED)

        return Response({
            "detail": f"Your subscription is cancelled. You keep full access until {period_end.date().isoformat()}.",
            "active_until": period_end,
            "billing": services.billing_state(request.user.tenant),
        })


class WebhookView(APIView):
    """
    Where the payment provider calls back.

    Unauthenticated by construction -- a provider holds no credential of ours
    -- and safe for one reason: the request is signed, and
    `services.handle_webhook` verifies that signature over the **raw body**
    before it parses or acts on a single field.

    Always answers 200 once the signature checks out, including for events it
    ignores and events that fail to settle. A provider that gets a 500 retries
    for hours; the outcome is on the `WebhookEvent` row, which is where an
    operator can actually see it.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, provider):
        try:
            event = services.handle_webhook(
                provider_name=provider,
                # request.body, not request.data: the signature is over the
                # bytes as received. DRF's parsed dict re-serialised would
                # have different whitespace and key order and would never
                # verify.
                body=request.body,
                headers=request.headers,
            )
        except SignatureInvalid:
            # No row, no detail, no hint about what was wrong. A forged
            # request gets nothing to work with.
            logger.warning("Rejected a %s webhook with an invalid signature.", provider)
            return Response({"detail": "Invalid signature."}, status=status.HTTP_400_BAD_REQUEST)
        except PaymentNotConfigured as exc:
            logger.error("A %s webhook arrived but %s", provider, exc)
            return Response({"detail": "Webhooks are not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except PaymentError as exc:
            logger.warning("A %s webhook could not be read: %s", provider, exc)
            return Response({"detail": "Malformed webhook."}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"status": getattr(event, "status", "RECEIVED")})


class CouponPreviewView(APIView):
    """
    What a code is worth against a plan, before anything is bought.

    Called while somebody is typing into the coupon box. It **writes nothing**
    -- no reservation, no redemption -- so trying three codes to find the best
    one does not burn three uses of a coupon limited to one per workspace.

    The numbers it returns are produced by the same function checkout uses, so
    the price shown here is the price charged. It is still not authoritative:
    `create_checkout` re-resolves and re-checks the code from scratch, because
    a preview is a display and a checkout is a decision.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    # A coupon box is a code oracle if it is not rate limited: a fast enough
    # client can walk the keyspace looking for a valid discount. The vague
    # refusal message makes each guess uninformative; this makes guessing slow.
    throttle_scope = "coupon"

    def post(self, request):
        try:
            plan = services.resolve_plan(request.user.tenant, request.data.get("plan"))
        except services.BillingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            quote = coupons.quote(
                tenant=request.user.tenant,
                code=request.data.get("code", ""),
                plan=plan,
            )
        except coupons.CouponError as exc:
            return Response(
                {"detail": str(exc), "code": "COUPON_INVALID"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(quote)


class TrialExtensionView(APIView):
    """
    Redeem a code that adds days to a running trial.

    Separate from checkout because there is no order and no money involved --
    it moves `trial_ends_at`, and access derives from that date rather than
    from any job having run. Refuses for a paying customer and for an expired
    trial; see coupons.redeem_trial_extension for why both matter.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "coupon"

    def post(self, request):
        try:
            result = coupons.redeem_trial_extension(
                tenant=request.user.tenant, code=request.data.get("code", ""),
            )
        except coupons.CouponError as exc:
            return Response(
                {"detail": str(exc), "code": "COUPON_INVALID"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        log_event(
            request=request, action="billing.trial_extended", tenant=request.user.tenant,
            object_type="subscription", object_id=None,
            metadata={"code": result["code"], "days": result["trial_days"]},
        )
        return Response({**result, "billing": services.billing_state(request.user.tenant)})


class PlanChangeView(APIView):
    """
    What moving to another plan would do, and -- for a downgrade -- doing it.

    GET is a preview and writes nothing. It exists because a customer pressing
    Downgrade is entitled to know, in advance and in numbers, which limits drop
    and what happens to the contacts they already hold. See plan_changes.preview.

    POST applies a **downgrade only**. An upgrade is a payment and goes through
    checkout; letting this endpoint move a workspace onto a more expensive plan
    would be handing out the paid product for free, so plan_changes refuses it
    at the service layer as well as here.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            plan = plan_changes.resolve_target(request.user.tenant, request.query_params.get("plan"))
        except services.BillingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(plan_changes.preview(tenant=request.user.tenant, new_plan=plan))

    def post(self, request):
        try:
            plan = plan_changes.resolve_target(request.user.tenant, request.data.get("plan"))
        except services.BillingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # Confirmed against the preview the customer was shown, so a plan whose
        # price changed between the two is not applied silently.
        preview = plan_changes.preview(tenant=request.user.tenant, new_plan=plan)
        if preview["direction"] == plan_changes.UPGRADE:
            return Response(
                {
                    "detail": "That plan costs more than your current one. Start a payment to upgrade.",
                    "code": "UPGRADE_REQUIRES_PAYMENT",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                plan_changes.apply_downgrade(
                    tenant=request.user.tenant, new_plan=plan, actor=request.user,
                )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "changed": True,
            "billing": services.billing_state(request.user.tenant),
        })


class PaymentHistoryView(APIView):
    """
    Every payment this workspace has made, as its own billing history.

    Scoped to the caller's tenant by construction -- there is no id in the URL
    to get wrong. Failed attempts are included, because a customer chasing a
    charge that did not work needs to see that it did not work; what is not
    included is the gateway's decline text, which is often more specific about
    somebody's bank than a product should repeat back to a screen-shared room.
    """

    permission_classes = [IsAuthenticated]

    #: A hard ceiling rather than pagination. A workspace with more than two
    #: hundred payments has a reconciliation question, not a browsing one, and
    #: an unbounded list here is an easy way to make one request expensive.
    MAX = 200

    def get(self, request):
        payments = (
            Payment.objects.filter(tenant=request.user.tenant)
            .select_related("checkout__plan")[: self.MAX]
        )
        return Response({
            "payments": PaymentSerializer(payments, many=True).data,
            "invoices": {
                "available": False,
                "detail": (
                    "Payment receipts are issued by the payment provider. This workspace does "
                    "not generate tax invoices."
                ),
            },
        })
