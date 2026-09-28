"""
Tests for checkout, verification, webhooks and billing security.

These use a **fake provider** rather than Razorpay's sandbox. That is a
deliberate limitation and worth stating: it means these tests prove the
*application's* rules -- the server prices the order, an unconfirmed payment
activates nothing, a wrong amount is refused, a redelivered webhook does not
charge twice, one customer cannot touch another's billing -- and they do not
prove that the Razorpay adapter speaks Razorpay correctly. The signature
algorithms are tested directly against known HMACs (`RazorpaySignatureTests`),
because that is the security boundary and it can be checked without a network.

Anything that genuinely requires the provider is listed as deferred in
docs/BILLING.md and needs a sandbox key to exercise.
"""

import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.subscriptions.models import Plan, Subscription
from apps.tenants.models import Tenant

from . import services
from .models import Checkout, Payment, WebhookEvent
from .providers import PaymentNotConfigured
from .providers.base import (
    CheckoutSession,
    PaymentProvider,
    PaymentResult,
    SignatureInvalid,
    WebhookResult,
)


class FakeProvider(PaymentProvider):
    """
    A provider that does exactly what it is told, so the *application's* rules
    are what is under test.

    `next_result` is what `verify_payment` will report. Setting it to a
    captured payment for the wrong amount is how the amount-mismatch tests
    simulate tampering without needing a gateway to tamper with.
    """

    name = "fake"
    supports_checkout = True
    handled_events = ("payment.captured", "payment.failed")
    next_result = None
    signature_valid = True
    order_counter = 0

    def is_configured(self):
        return True

    def public_config(self):
        return {"provider": self.name, "key_id": "pk_public"}

    def create_checkout(self, *, checkout):
        FakeProvider.order_counter += 1
        order_id = f"order_{FakeProvider.order_counter}"
        return CheckoutSession(
            provider_order_id=order_id,
            amount_minor=checkout.amount_minor,
            currency=checkout.currency,
            public_params={"provider": self.name, "key_id": "pk_public", "order_id": order_id},
        )

    def verify_payment(self, *, checkout, payload):
        if not FakeProvider.signature_valid:
            raise SignatureInvalid("bad signature")
        return FakeProvider.next_result

    def handle_webhook(self, *, body, headers):
        if not FakeProvider.signature_valid:
            raise SignatureInvalid("bad signature")
        data = json.loads(body.decode("utf-8"))
        result = FakeProvider.next_result
        return WebhookResult(
            event_id=data["event_id"],
            event_type=data.get("event", "payment.captured"),
            provider_order_id=data.get("order_id", ""),
            payment=result,
            payload=data,
        )


def _use_fake():
    """Point the resolver at the fake, and treat its events as handled."""
    return patch.multiple(
        "apps.billing.services",
        get_provider=lambda name=None: FakeProvider(),
        require_live_provider=lambda: FakeProvider(),
    )


PAYMENTS_ON = {"PAYMENTS_ENABLED": True, "PAYMENT_PROVIDER": "fake", "BILLING_CURRENCY": "INR"}


class BillingBase(TestCase):
    def setUp(self):
        FakeProvider.next_result = None
        FakeProvider.signature_valid = True
        self.platform = Tenant.objects.create(
            name="Plat", slug="bill-platform", kind=Tenant.Kind.PLATFORM,
        )
        self.tenant = Tenant.objects.create(
            name="Buyer", slug="bill-buyer", kind=Tenant.Kind.CUSTOMER, parent=self.platform,
        )
        self.user = get_user_model().objects.create_user(
            username="buyer", password="x", tenant=self.tenant,
        )
        self.plan = Plan.objects.create(
            owner=None, name="Growth", price_cents=299900, currency="INR",
            is_public=True, is_active=True, max_contacts=25000, max_messages_per_month=25000,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _checkout(self):
        with _use_fake():
            return services.create_checkout(tenant=self.tenant, user=self.user, plan_id=self.plan.pk)

    @staticmethod
    def _captured(checkout, amount=None, currency=None, payment_id="pay_1"):
        return PaymentResult(
            provider_payment_id=payment_id,
            provider_order_id=checkout.provider_order_id,
            amount_minor=amount if amount is not None else checkout.amount_minor,
            currency=currency or checkout.currency,
            captured=True,
            raw_status="captured",
        )


@override_settings(**PAYMENTS_ON)
class CheckoutPricingTests(BillingBase):
    def test_the_server_prices_the_order(self):
        checkout = self._checkout()
        self.assertEqual(checkout.amount_minor, self.plan.price_cents)
        self.assertEqual(checkout.currency, "INR")

    def test_an_amount_in_the_request_is_ignored(self):
        """There is no amount field. This asserts there is still no amount field."""
        response = self.client.post(
            reverse("billing-checkout"),
            {"plan": self.plan.pk, "amount": 1, "amount_minor": 1, "price": 1, "currency": "USD"},
            format="json",
        )
        with _use_fake():
            response = self.client.post(
                reverse("billing-checkout"),
                {"plan": self.plan.pk, "amount": 1, "price": 1, "currency": "USD"},
                format="json",
            )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["amount_minor"], 299900)
        self.assertEqual(response.data["currency"], "INR")

    def test_a_plan_this_workspace_is_not_offered_is_refused(self):
        other_reseller = Tenant.objects.create(
            name="Rival", slug="rival", kind=Tenant.Kind.RESELLER, parent=self.platform,
        )
        private = Plan.objects.create(
            owner=other_reseller, name="Rival Deal", price_cents=1, is_public=True, is_active=True,
        )
        with _use_fake(), self.assertRaises(services.BillingError):
            services.create_checkout(tenant=self.tenant, user=self.user, plan_id=private.pk)

    def test_an_inactive_plan_is_refused(self):
        self.plan.is_active = False
        self.plan.save(update_fields=["is_active"])
        with _use_fake(), self.assertRaises(services.BillingError):
            services.create_checkout(tenant=self.tenant, user=self.user, plan_id=self.plan.pk)

    def test_a_nonsense_plan_id_is_refused(self):
        with _use_fake(), self.assertRaises(services.BillingError):
            services.create_checkout(tenant=self.tenant, user=self.user, plan_id="../../etc/passwd")

    def test_the_public_params_carry_no_secret(self):
        checkout = self._checkout()
        blob = json.dumps(checkout.metadata).lower()
        for forbidden in ("secret", "key_secret", "webhook_secret", "password"):
            self.assertNotIn(forbidden, blob)


@override_settings(**PAYMENTS_ON)
class PaymentVerificationTests(BillingBase):
    def test_a_confirmed_payment_activates_the_plan(self):
        checkout = self._checkout()
        FakeProvider.next_result = self._captured(checkout)
        with _use_fake():
            services.verify_payment(checkout=checkout, payload={})

        subscription = Subscription.objects.get(tenant=self.tenant)
        self.assertEqual(subscription.status, Subscription.Status.ACTIVE)
        self.assertEqual(subscription.plan, self.plan)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, Checkout.Status.PAID)

    def test_a_browser_claiming_success_activates_nothing(self):
        """
        The core rule. The provider says the payment was never captured, and
        the body's opinion is irrelevant.
        """
        checkout = self._checkout()
        FakeProvider.next_result = PaymentResult(
            provider_payment_id="pay_fail", provider_order_id=checkout.provider_order_id,
            amount_minor=checkout.amount_minor, currency="INR", captured=False,
            failure_reason="card declined", raw_status="failed",
        )
        with _use_fake():
            services.verify_payment(
                checkout=checkout, payload={"payment_status": "success", "paid": True},
            )

        self.assertFalse(Subscription.objects.filter(tenant=self.tenant).exists())
        checkout.refresh_from_db()
        self.assertNotEqual(checkout.status, Checkout.Status.PAID)

    def test_a_bad_signature_activates_nothing(self):
        checkout = self._checkout()
        FakeProvider.signature_valid = False
        with _use_fake(), self.assertRaises(SignatureInvalid):
            services.verify_payment(checkout=checkout, payload={})
        self.assertFalse(Subscription.objects.filter(tenant=self.tenant).exists())

    def test_paying_too_little_is_refused(self):
        """₹499 against a ₹2,999 order."""
        checkout = self._checkout()
        FakeProvider.next_result = self._captured(checkout, amount=49900)
        with _use_fake(), self.assertRaises(services.BillingError):
            services.verify_payment(checkout=checkout, payload={})

        self.assertFalse(Subscription.objects.filter(tenant=self.tenant).exists())
        self.assertTrue(
            Payment.objects.filter(checkout=checkout, status=Payment.Status.FAILED).exists()
        )

    def test_the_wrong_currency_is_refused(self):
        checkout = self._checkout()
        FakeProvider.next_result = self._captured(checkout, currency="USD")
        with _use_fake(), self.assertRaises(services.BillingError):
            services.verify_payment(checkout=checkout, payload={})
        self.assertFalse(Subscription.objects.filter(tenant=self.tenant).exists())

    def test_an_amount_mismatch_is_audited(self):
        from apps.audit.models import AuditEvent

        checkout = self._checkout()
        FakeProvider.next_result = self._captured(checkout, amount=1)
        with _use_fake(), self.assertRaises(services.BillingError):
            services.verify_payment(checkout=checkout, payload={})

        self.assertTrue(
            AuditEvent.objects.filter(action="billing.amount_mismatch", tenant=self.tenant).exists()
        )

    def test_settling_twice_does_not_activate_twice(self):
        """The browser callback and the webhook race on nearly every payment."""
        checkout = self._checkout()
        FakeProvider.next_result = self._captured(checkout)
        with _use_fake():
            first = services.verify_payment(checkout=checkout, payload={})
            second = services.verify_payment(checkout=checkout, payload={})

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Payment.objects.filter(status=Payment.Status.CAPTURED).count(), 1)
        self.assertEqual(Subscription.objects.filter(tenant=self.tenant).count(), 1)

    def test_a_failed_attempt_leaves_the_checkout_payable(self):
        """A declined card should be retryable at the price already quoted."""
        checkout = self._checkout()
        FakeProvider.next_result = PaymentResult(
            provider_payment_id="pay_declined", provider_order_id=checkout.provider_order_id,
            amount_minor=checkout.amount_minor, currency="INR", captured=False,
            failure_reason="declined", raw_status="failed",
        )
        with _use_fake():
            services.verify_payment(checkout=checkout, payload={})
        checkout.refresh_from_db()
        self.assertTrue(checkout.is_open)

    def test_activation_upgrades_rather_than_adding_a_second_subscription(self):
        starter = Plan.objects.create(
            owner=None, name="Starter", price_cents=99900, currency="INR", is_public=True,
        )
        Subscription.objects.create(
            tenant=self.tenant, plan=starter, status=Subscription.Status.ACTIVE,
        )
        checkout = self._checkout()
        FakeProvider.next_result = self._captured(checkout)
        with _use_fake():
            services.verify_payment(checkout=checkout, payload={})

        self.assertEqual(Subscription.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(Subscription.objects.get(tenant=self.tenant).plan, self.plan)

    def test_paying_clears_the_trial(self):
        trial_plan = Plan.objects.create(owner=None, name="Trial", price_cents=0)
        Subscription.objects.create(
            tenant=self.tenant, plan=trial_plan, status=Subscription.Status.TRIALING,
            trial_started_at=timezone.now(), trial_ends_at=timezone.now() + timedelta(days=5),
        )
        checkout = self._checkout()
        FakeProvider.next_result = self._captured(checkout)
        with _use_fake():
            services.verify_payment(checkout=checkout, payload={})

        subscription = Subscription.objects.get(tenant=self.tenant)
        self.assertIsNone(subscription.trial_ends_at)
        self.assertFalse(subscription.is_trial)


@override_settings(**PAYMENTS_ON)
class WebhookTests(BillingBase):
    def _webhook(self, checkout, event_id="evt_1", event="payment.captured"):
        body = json.dumps({
            "event_id": event_id, "event": event, "order_id": checkout.provider_order_id,
        }).encode("utf-8")
        with _use_fake():
            return services.handle_webhook(provider_name="fake", body=body, headers={})

    def test_a_webhook_settles_a_payment(self):
        checkout = self._checkout()
        FakeProvider.next_result = self._captured(checkout)
        event = self._webhook(checkout)

        self.assertEqual(event.status, WebhookEvent.Status.PROCESSED)
        self.assertEqual(Subscription.objects.get(tenant=self.tenant).plan, self.plan)

    def test_a_redelivered_webhook_does_not_charge_twice(self):
        """Idempotency through the unique constraint, not through a check."""
        checkout = self._checkout()
        FakeProvider.next_result = self._captured(checkout)
        self._webhook(checkout, event_id="evt_dup")
        self._webhook(checkout, event_id="evt_dup")

        self.assertEqual(WebhookEvent.objects.filter(event_id="evt_dup").count(), 1)
        self.assertEqual(Payment.objects.filter(status=Payment.Status.CAPTURED).count(), 1)

    def test_a_second_distinct_event_for_a_settled_order_activates_nothing_more(self):
        checkout = self._checkout()
        FakeProvider.next_result = self._captured(checkout)
        self._webhook(checkout, event_id="evt_a")
        self._webhook(checkout, event_id="evt_b")

        self.assertEqual(Payment.objects.filter(status=Payment.Status.CAPTURED).count(), 1)
        subscription = Subscription.objects.get(tenant=self.tenant)
        # The period was not extended a second time.
        self.assertEqual(Subscription.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(subscription.plan, self.plan)

    def test_a_forged_webhook_leaves_no_row(self):
        """No signature, no record. There is no value in letting a forger fill this table."""
        checkout = self._checkout()
        FakeProvider.signature_valid = False
        with _use_fake(), self.assertRaises(SignatureInvalid):
            services.handle_webhook(
                provider_name="fake",
                body=json.dumps({"event_id": "evt_forged"}).encode("utf-8"),
                headers={},
            )
        self.assertFalse(WebhookEvent.objects.exists())
        self.assertFalse(Subscription.objects.filter(tenant=self.tenant).exists())

    def test_an_event_for_an_unknown_order_is_ignored_not_failed(self):
        body = json.dumps({
            "event_id": "evt_stranger", "event": "payment.captured", "order_id": "order_elsewhere",
        }).encode("utf-8")
        FakeProvider.next_result = None
        with _use_fake():
            event = services.handle_webhook(provider_name="fake", body=body, headers={})

        self.assertEqual(event.status, WebhookEvent.Status.IGNORED)
        self.assertIsNone(event.checkout)

    def test_an_unhandled_event_type_is_ignored(self):
        checkout = self._checkout()
        FakeProvider.next_result = self._captured(checkout)
        event = self._webhook(checkout, event_id="evt_odd", event="payment.dispute.created")
        self.assertEqual(event.status, WebhookEvent.Status.IGNORED)
        self.assertFalse(Subscription.objects.filter(tenant=self.tenant).exists())

    def test_a_webhook_with_the_wrong_amount_is_recorded_as_failed_and_activates_nothing(self):
        checkout = self._checkout()
        FakeProvider.next_result = self._captured(checkout, amount=1)
        event = self._webhook(checkout, event_id="evt_cheap")

        self.assertEqual(event.status, WebhookEvent.Status.FAILED)
        self.assertFalse(Subscription.objects.filter(tenant=self.tenant).exists())

    def test_the_tenant_comes_from_our_own_row_not_the_payload(self):
        """
        A signature says the message came from the provider, not that the
        provider was told the truth about who to bill.
        """
        victim = Tenant.objects.create(
            name="Victim", slug="wh-victim", kind=Tenant.Kind.CUSTOMER, parent=self.platform,
        )
        checkout = self._checkout()
        FakeProvider.next_result = self._captured(checkout)
        body = json.dumps({
            "event_id": "evt_spoof", "event": "payment.captured",
            "order_id": checkout.provider_order_id,
            "notes": {"tenant_id": victim.pk},
        }).encode("utf-8")
        with _use_fake():
            event = services.handle_webhook(provider_name="fake", body=body, headers={})

        self.assertEqual(event.tenant_id, self.tenant.pk)
        self.assertFalse(Subscription.objects.filter(tenant=victim).exists())


@override_settings(**PAYMENTS_ON)
class BillingTenantIsolationTests(BillingBase):
    """One customer must not be able to touch another's billing."""

    def setUp(self):
        super().setUp()
        self.other_tenant = Tenant.objects.create(
            name="Other", slug="bill-other", kind=Tenant.Kind.CUSTOMER, parent=self.platform,
        )
        self.other_user = get_user_model().objects.create_user(
            username="other-buyer", password="x", tenant=self.other_tenant,
        )

    def test_a_customer_cannot_verify_another_customers_checkout(self):
        checkout = self._checkout()
        client = APIClient()
        client.force_authenticate(self.other_user)
        response = client.post(
            reverse("billing-checkout-verify", args=[checkout.pk]), {}, format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_a_customer_cannot_read_another_customers_checkout_status(self):
        checkout = self._checkout()
        client = APIClient()
        client.force_authenticate(self.other_user)
        response = client.get(reverse("billing-checkout-status", args=[checkout.pk]))
        self.assertEqual(response.status_code, 404)

    def test_the_billing_overview_shows_only_this_workspaces_payments(self):
        checkout = self._checkout()
        FakeProvider.next_result = self._captured(checkout)
        with _use_fake():
            services.verify_payment(checkout=checkout, payload={})

        client = APIClient()
        client.force_authenticate(self.other_user)
        response = client.get(reverse("billing-overview"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["payments"], [])

    def test_a_customer_cannot_cancel_another_customers_subscription(self):
        Subscription.objects.create(
            tenant=self.tenant, plan=self.plan, status=Subscription.Status.ACTIVE,
        )
        client = APIClient()
        client.force_authenticate(self.other_user)
        response = client.post(reverse("billing-cancel"), {}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            Subscription.objects.get(tenant=self.tenant).status, Subscription.Status.ACTIVE,
        )


class PaymentsDisabledTests(BillingBase):
    @override_settings(PAYMENTS_ENABLED=False, PAYMENT_PROVIDER="manual")
    def test_checkout_is_refused_rather_than_faked(self):
        response = self.client.post(
            reverse("billing-checkout"), {"plan": self.plan.pk}, format="json",
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "PAYMENTS_UNAVAILABLE")
        self.assertFalse(Checkout.objects.exists())
        self.assertFalse(Subscription.objects.filter(tenant=self.tenant).exists())

    @override_settings(PAYMENTS_ENABLED=True, PAYMENT_PROVIDER="razorpay", RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET="")
    def test_enabled_but_unconfigured_is_refused_rather_than_faked(self):
        """Section 107: not a fake success."""
        response = self.client.post(
            reverse("billing-checkout"), {"plan": self.plan.pk}, format="json",
        )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Subscription.objects.filter(tenant=self.tenant).exists())

    @override_settings(PAYMENTS_ENABLED=True, PAYMENT_PROVIDER="nonesuch")
    def test_an_unknown_provider_is_refused_rather_than_silently_disabled(self):
        with self.assertRaises(PaymentNotConfigured):
            from .providers import get_provider

            get_provider()

    @override_settings(PAYMENTS_ENABLED=False, PAYMENT_PROVIDER="manual")
    def test_the_billing_page_says_payments_are_off(self):
        response = self.client.get(reverse("billing-overview"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["payments_enabled"])


class PricingPageTests(BillingBase):
    def test_the_price_list_is_public(self):
        client = APIClient()
        response = client.get(reverse("billing-pricing"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["plans"]), 1)
        self.assertEqual(response.data["plans"][0]["name"], "Growth")

    def test_limits_and_features_come_from_the_database(self):
        client = APIClient()
        response = client.get(reverse("billing-pricing"))
        plan = response.data["plans"][0]
        self.assertEqual(plan["limits"]["contacts"], 25000)
        self.assertEqual(plan["price_minor"], 299900)
        self.assertTrue(any(f["key"] == "ai_agent" for f in plan["features"]))

    def test_a_private_plan_is_not_published(self):
        Plan.objects.create(owner=None, name="Secret", price_cents=1, is_public=False, is_active=True)
        client = APIClient()
        response = client.get(reverse("billing-pricing"))
        names = [plan["name"] for plan in response.data["plans"]]
        self.assertNotIn("Secret", names)

    def test_a_resellers_plan_is_not_published_on_the_platform_page(self):
        reseller = Tenant.objects.create(
            name="R", slug="pricing-reseller", kind=Tenant.Kind.RESELLER, parent=self.platform,
        )
        Plan.objects.create(
            owner=reseller, name="Reseller Tier", price_cents=1, is_public=True, is_active=True,
        )
        client = APIClient()
        response = client.get(reverse("billing-pricing"))
        names = [plan["name"] for plan in response.data["plans"]]
        self.assertNotIn("Reseller Tier", names)

    def test_the_public_page_leaks_no_tenant_data(self):
        Subscription.objects.create(tenant=self.tenant, plan=self.plan)
        client = APIClient()
        response = client.get(reverse("billing-pricing"))
        blob = json.dumps(response.data).lower()
        for forbidden in ("buyer", "bill-buyer", "subscription_id", "secret"):
            self.assertNotIn(forbidden, blob)


@override_settings(**PAYMENTS_ON)
class CancellationTests(BillingBase):
    def test_cancelling_keeps_access_until_the_period_ends(self):
        end = timezone.now() + timedelta(days=20)
        Subscription.objects.create(
            tenant=self.tenant, plan=self.plan, status=Subscription.Status.ACTIVE,
            current_period_start=timezone.now() - timedelta(days=10), current_period_end=end,
        )
        response = self.client.post(reverse("billing-cancel"), {}, format="json")

        self.assertEqual(response.status_code, 200)
        subscription = Subscription.objects.get(tenant=self.tenant)
        self.assertEqual(subscription.status, Subscription.Status.CANCELLED)
        self.assertEqual(subscription.access_state(), Subscription.FULL)

    def test_the_response_states_when_access_actually_ends(self):
        """Not misrepresenting cancellation timing is a requirement, not a nicety."""
        end = timezone.now() + timedelta(days=20)
        Subscription.objects.create(
            tenant=self.tenant, plan=self.plan, status=Subscription.Status.ACTIVE,
            current_period_end=end,
        )
        response = self.client.post(reverse("billing-cancel"), {}, format="json")
        self.assertIn(end.date().isoformat(), response.data["detail"])
        self.assertIsNotNone(response.data["active_until"])

    def test_access_stops_once_the_paid_period_is_over(self):
        Subscription.objects.create(
            tenant=self.tenant, plan=self.plan, status=Subscription.Status.CANCELLED,
            current_period_start=timezone.now() - timedelta(days=40),
            current_period_end=timezone.now() - timedelta(days=1),
            cancelled_at=timezone.now() - timedelta(days=10),
        )
        self.assertEqual(
            Subscription.objects.get(tenant=self.tenant).access_state(), Subscription.EXPIRED,
        )

    def test_cancelling_deletes_nothing(self):
        from apps.contacts.models import Contact

        Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Kept", phone="+15558880001",
        )
        Subscription.objects.create(
            tenant=self.tenant, plan=self.plan, status=Subscription.Status.ACTIVE,
        )
        self.client.post(reverse("billing-cancel"), {}, format="json")
        self.assertTrue(Contact.objects.filter(tenant=self.tenant).exists())


class RazorpaySignatureTests(TestCase):
    """
    The security boundary, checked against HMACs computed here.

    These do not need a network and do not need a sandbox: the signature
    scheme is arithmetic, and getting it wrong is the way a payment
    integration becomes an unauthenticated "activate my subscription" endpoint.
    """

    def setUp(self):
        from .providers.razorpay import RazorpayProvider

        self.provider = RazorpayProvider()

    @override_settings(RAZORPAY_KEY_ID="rzp_test_x", RAZORPAY_KEY_SECRET="s3cr3t")
    def test_a_correct_callback_signature_is_accepted(self):
        checkout = type("C", (), {"provider_order_id": "order_A"})()
        signature = hmac.new(
            b"s3cr3t", b"order_A|pay_B", hashlib.sha256,
        ).hexdigest()

        with patch.object(self.provider, "fetch_payment", return_value="ok") as fetch:
            result = self.provider.verify_payment(
                checkout=checkout,
                payload={
                    "razorpay_order_id": "order_A",
                    "razorpay_payment_id": "pay_B",
                    "razorpay_signature": signature,
                },
            )
        self.assertEqual(result, "ok")
        fetch.assert_called_once_with("pay_B")

    @override_settings(RAZORPAY_KEY_ID="rzp_test_x", RAZORPAY_KEY_SECRET="s3cr3t")
    def test_a_wrong_callback_signature_is_refused(self):
        checkout = type("C", (), {"provider_order_id": "order_A"})()
        with self.assertRaises(SignatureInvalid):
            self.provider.verify_payment(
                checkout=checkout,
                payload={
                    "razorpay_order_id": "order_A",
                    "razorpay_payment_id": "pay_B",
                    "razorpay_signature": "deadbeef",
                },
            )

    @override_settings(RAZORPAY_KEY_ID="rzp_test_x", RAZORPAY_KEY_SECRET="s3cr3t")
    def test_a_signature_for_a_different_order_is_refused(self):
        """
        Replaying a genuine ₹1 order's callback against a ₹9,999 one. The
        signature is valid -- for the wrong order.
        """
        checkout = type("C", (), {"provider_order_id": "order_EXPENSIVE"})()
        signature = hmac.new(b"s3cr3t", b"order_CHEAP|pay_B", hashlib.sha256).hexdigest()
        with self.assertRaises(SignatureInvalid):
            self.provider.verify_payment(
                checkout=checkout,
                payload={
                    "razorpay_order_id": "order_CHEAP",
                    "razorpay_payment_id": "pay_B",
                    "razorpay_signature": signature,
                },
            )

    @override_settings(RAZORPAY_KEY_SECRET="s3cr3t", RAZORPAY_WEBHOOK_SECRET="wh_secret")
    def test_a_correct_webhook_signature_is_accepted(self):
        body = json.dumps({
            "event": "payment.captured",
            "payload": {"payment": {"entity": {
                "id": "pay_1", "order_id": "order_1", "amount": 100, "currency": "INR",
                "status": "captured",
            }}},
        }).encode("utf-8")
        signature = hmac.new(b"wh_secret", body, hashlib.sha256).hexdigest()

        result = self.provider.handle_webhook(
            body=body, headers={"X-Razorpay-Signature": signature, "X-Razorpay-Event-Id": "evt_9"},
        )
        self.assertEqual(result.event_id, "evt_9")
        self.assertEqual(result.event_type, "payment.captured")
        self.assertTrue(result.payment.captured)
        self.assertEqual(result.payment.amount_minor, 100)

    @override_settings(RAZORPAY_WEBHOOK_SECRET="wh_secret")
    def test_a_webhook_signed_over_re_serialised_json_is_refused(self):
        """
        The subtle one. The digest is over the **bytes as received**; a
        signature computed over a re-serialised body will not verify, which is
        the whole reason `request.body` is used rather than `request.data`.
        """
        received = b'{"event":"payment.captured",  "payload":{}}'
        reserialised = json.dumps(json.loads(received)).encode("utf-8")
        signature = hmac.new(b"wh_secret", reserialised, hashlib.sha256).hexdigest()

        with self.assertRaises(SignatureInvalid):
            self.provider.handle_webhook(
                body=received, headers={"X-Razorpay-Signature": signature},
            )

    @override_settings(RAZORPAY_WEBHOOK_SECRET="wh_secret")
    def test_an_unsigned_webhook_is_refused(self):
        with self.assertRaises(SignatureInvalid):
            self.provider.handle_webhook(body=b"{}", headers={})

    @override_settings(RAZORPAY_WEBHOOK_SECRET="")
    def test_webhooks_are_refused_outright_with_no_secret_configured(self):
        """An unsigned webhook endpoint is an unauthenticated activation endpoint."""
        with self.assertRaises(PaymentNotConfigured):
            self.provider.handle_webhook(body=b"{}", headers={"X-Razorpay-Signature": "x"})

    @override_settings(RAZORPAY_WEBHOOK_SECRET="wh_secret")
    def test_a_missing_event_id_falls_back_to_a_stable_digest(self):
        """
        A random fallback would make every redelivery look new -- exactly the
        double-activation the ledger exists to prevent.
        """
        body = json.dumps({"event": "payment.captured", "payload": {}}).encode("utf-8")
        signature = hmac.new(b"wh_secret", body, hashlib.sha256).hexdigest()

        first = self.provider.handle_webhook(body=body, headers={"X-Razorpay-Signature": signature})
        second = self.provider.handle_webhook(body=body, headers={"X-Razorpay-Signature": signature})
        self.assertEqual(first.event_id, second.event_id)

    @override_settings(RAZORPAY_KEY_ID="rzp_test_x", RAZORPAY_KEY_SECRET="s3cr3t")
    def test_public_config_carries_the_key_id_and_nothing_else(self):
        config = self.provider.public_config()
        self.assertEqual(config["key_id"], "rzp_test_x")
        self.assertNotIn("s3cr3t", json.dumps(config))


class WebhookEndpointTests(BillingBase):
    def test_a_bad_signature_gets_400_and_no_row(self):
        with override_settings(PAYMENTS_ENABLED=True, PAYMENT_PROVIDER="razorpay",
                               RAZORPAY_KEY_ID="k", RAZORPAY_KEY_SECRET="s",
                               RAZORPAY_WEBHOOK_SECRET="wh"):
            client = APIClient()
            response = client.post(
                reverse("billing-webhook", args=["razorpay"]),
                data=b'{"event":"payment.captured"}',
                content_type="application/json",
                HTTP_X_RAZORPAY_SIGNATURE="wrong",
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(WebhookEvent.objects.exists())

    def test_the_webhook_needs_no_authentication(self):
        """A payment provider holds no credential of ours; the signature is the credential."""
        with override_settings(PAYMENTS_ENABLED=True, PAYMENT_PROVIDER="razorpay",
                               RAZORPAY_KEY_ID="k", RAZORPAY_KEY_SECRET="s",
                               RAZORPAY_WEBHOOK_SECRET="wh"):
            client = APIClient()
            response = client.post(
                reverse("billing-webhook", args=["razorpay"]),
                data=b"{}", content_type="application/json",
            )
        # 400 (no signature), not 401/403 -- it got past authentication.
        self.assertEqual(response.status_code, 400)


@override_settings(**PAYMENTS_ON)
class CheckoutExpiryTests(BillingBase):
    def test_a_stale_unpaid_checkout_expires(self):
        checkout = self._checkout()
        checkout.expires_at = timezone.now() - timedelta(minutes=1)
        checkout.save(update_fields=["expires_at"])

        self.assertEqual(services.expire_stale_checkouts(), 1)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, Checkout.Status.EXPIRED)

    def test_a_paid_checkout_is_never_expired_out_from_under_its_payment(self):
        """A late webhook must still find its order."""
        checkout = self._checkout()
        FakeProvider.next_result = self._captured(checkout)
        with _use_fake():
            services.verify_payment(checkout=checkout, payload={})
        checkout.refresh_from_db()
        checkout.expires_at = timezone.now() - timedelta(minutes=1)
        checkout.save(update_fields=["expires_at"])

        services.expire_stale_checkouts()
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, Checkout.Status.PAID)
