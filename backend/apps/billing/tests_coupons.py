"""
Coupons: what they are worth, who may use them, and how often.

The tests worth reading here are the ones about *limits under concurrency* and
about *what a client may influence*. A discount system is a place where the
difference between "checked" and "enforced" is money, so most of this file is
attempts to get a discount the rules do not allow.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing import coupons
from apps.billing.models import Checkout, Coupon, CouponRedemption
from apps.subscriptions.models import Plan, Subscription
from apps.tenants.models import Tenant

User = get_user_model()


class CouponTestCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme", kind=Tenant.Kind.CUSTOMER)
        self.other = Tenant.objects.create(name="Other", slug="other", kind=Tenant.Kind.CUSTOMER)
        self.plan = Plan.objects.create(
            name="Growth", price_cents=99900, currency="INR", is_public=True,
            max_messages_per_month=10000, max_contacts=5000, max_devices=2, max_users=5,
        )
        self.premium = Plan.objects.create(
            name="Scale", price_cents=249900, currency="INR", is_public=True,
        )
        self.user = User.objects.create_user(
            username="acme-owner", password="pw", tenant=self.tenant, role=User.Role.USER,
        )

    def coupon(self, **kwargs):
        defaults = {
            "code": "WELCOME20", "kind": Coupon.Kind.PERCENT, "value": 20,
            "max_redemptions": 0, "max_per_tenant": 1, "is_active": True,
        }
        return Coupon.objects.create(**{**defaults, **kwargs})


class DiscountArithmeticTests(CouponTestCase):
    def test_percentage_rounds_down_in_the_customers_favour(self):
        """
        A percentage that does not divide evenly must never charge more than
        the page said. 33% of 99900 is 32967.0 exactly; use a price that does
        not divide to prove the direction of the rounding.
        """
        coupon = self.coupon(kind=Coupon.Kind.PERCENT, value=33)
        # 33% of 1001 = 330.33 -> discount 330, charged 671.
        self.assertEqual(coupon.discount_for(1001), 330)

    def test_fixed_discount_never_makes_the_price_negative(self):
        coupon = self.coupon(code="BIG", kind=Coupon.Kind.FIXED, value=500000, currency="INR")
        self.assertEqual(coupon.discount_for(99900), 99900)

    def test_trial_extension_is_not_a_discount(self):
        coupon = self.coupon(code="EXTRA", kind=Coupon.Kind.TRIAL_EXTENSION, value=14)
        self.assertEqual(coupon.discount_for(99900), 0)


class CouponWindowTests(CouponTestCase):
    def test_expired_coupon_is_refused(self):
        coupon = self.coupon(expires_at=timezone.now() - timedelta(hours=1))
        with self.assertRaises(coupons.CouponError):
            coupons.check(coupon, tenant=self.tenant, plan=self.plan)

    def test_coupon_not_yet_started_is_refused(self):
        coupon = self.coupon(starts_at=timezone.now() + timedelta(days=1))
        with self.assertRaises(coupons.CouponError):
            coupons.check(coupon, tenant=self.tenant, plan=self.plan)

    def test_inactive_coupon_is_refused(self):
        coupon = self.coupon(is_active=False)
        with self.assertRaises(coupons.CouponError):
            coupons.check(coupon, tenant=self.tenant, plan=self.plan)

    def test_refusal_message_is_the_same_whatever_the_reason(self):
        """
        A specific refusal is a code oracle: "that code has expired" confirms a
        guess was a real code. Every failure says the same thing.
        """
        expired = self.coupon(code="EXPIRED", expires_at=timezone.now() - timedelta(hours=1))
        wrong_plan = self.coupon(code="WRONGPLAN")
        wrong_plan.plans.add(self.premium)

        messages = set()
        for coupon in (expired, wrong_plan):
            with self.assertRaises(coupons.CouponError) as ctx:
                coupons.check(coupon, tenant=self.tenant, plan=self.plan)
            messages.add(str(ctx.exception))
        with self.assertRaises(coupons.CouponError) as ctx:
            coupons.resolve(self.tenant, "NOSUCHCODE")
        messages.add(str(ctx.exception))

        self.assertEqual(len(messages), 1, f"Refusals leaked which rule failed: {messages}")


class PlanRestrictionTests(CouponTestCase):
    def test_coupon_restricted_to_another_plan_is_refused(self):
        coupon = self.coupon()
        coupon.plans.add(self.premium)
        with self.assertRaises(coupons.CouponError):
            coupons.check(coupon, tenant=self.tenant, plan=self.plan)

    def test_coupon_with_no_restriction_applies_to_any_plan(self):
        coupon = self.coupon()
        coupons.check(coupon, tenant=self.tenant, plan=self.plan)
        coupons.check(coupon, tenant=self.tenant, plan=self.premium)

    def test_fixed_discount_in_the_wrong_currency_is_refused(self):
        """
        No exchange rates exist in this application, and checkout is the worst
        possible place to start inventing one.
        """
        usd_plan = Plan.objects.create(name="US Growth", price_cents=9900, currency="USD", is_public=True)
        coupon = self.coupon(code="INR200", kind=Coupon.Kind.FIXED, value=20000, currency="INR")
        with self.assertRaises(coupons.CouponError):
            coupons.check(coupon, tenant=self.tenant, plan=usd_plan)


class CouponOwnershipTests(CouponTestCase):
    def test_a_resellers_coupon_is_invisible_to_another_resellers_customer(self):
        reseller = Tenant.objects.create(name="R1", slug="r1", kind=Tenant.Kind.RESELLER)
        other_reseller = Tenant.objects.create(name="R2", slug="r2", kind=Tenant.Kind.RESELLER)
        customer = Tenant.objects.create(
            name="Cust", slug="cust", kind=Tenant.Kind.CUSTOMER, parent=other_reseller,
        )
        self.coupon(code="THEIRS", owner=reseller)

        with self.assertRaises(coupons.CouponError):
            coupons.resolve(customer, "THEIRS")

    def test_two_resellers_may_use_the_same_code_independently(self):
        r1 = Tenant.objects.create(name="R1", slug="r1", kind=Tenant.Kind.RESELLER)
        r2 = Tenant.objects.create(name="R2", slug="r2", kind=Tenant.Kind.RESELLER)
        self.coupon(code="WELCOME", owner=r1, value=10)
        self.coupon(code="WELCOME", owner=r2, value=50)

        c1 = Tenant.objects.create(name="C1", slug="c1", kind=Tenant.Kind.CUSTOMER, parent=r1)
        c2 = Tenant.objects.create(name="C2", slug="c2", kind=Tenant.Kind.CUSTOMER, parent=r2)

        self.assertEqual(coupons.resolve(c1, "WELCOME").value, 10)
        self.assertEqual(coupons.resolve(c2, "WELCOME").value, 50)

    def test_code_matching_is_case_insensitive(self):
        self.coupon(code="WELCOME20")
        self.assertEqual(coupons.resolve(self.tenant, "welcome20").code, "WELCOME20")
        self.assertEqual(coupons.resolve(self.tenant, " Welcome20 ").code, "WELCOME20")


class RedemptionLimitTests(CouponTestCase):
    def _checkout(self, tenant=None, plan=None):
        return Checkout.objects.create(
            tenant=tenant or self.tenant, plan=plan or self.plan, provider="test",
            amount_minor=79920, list_amount_minor=99900, discount_minor=19980, currency="INR",
        )

    def test_per_tenant_limit_blocks_a_second_use(self):
        coupon = self.coupon(max_per_tenant=1)
        coupons.reserve(
            tenant=self.tenant, coupon=coupon, checkout=self._checkout(), discount_minor=19980,
        )
        with self.assertRaises(coupons.CouponError):
            coupons.check(coupon, tenant=self.tenant, plan=self.plan)

    def test_a_reservation_counts_against_the_total_before_any_payment(self):
        """
        The reason reservations exist. Counting only at capture would let
        fifty simultaneous checkouts all pass a limit of one.
        """
        coupon = self.coupon(max_redemptions=1, max_per_tenant=0)
        coupons.reserve(
            tenant=self.tenant, coupon=coupon, checkout=self._checkout(), discount_minor=19980,
        )
        with self.assertRaises(coupons.CouponError):
            coupons.check(coupon, tenant=self.other, plan=self.plan)

    def test_releasing_a_reservation_gives_the_slot_back(self):
        coupon = self.coupon(max_redemptions=1, max_per_tenant=0)
        checkout = self._checkout()
        coupons.reserve(tenant=self.tenant, coupon=coupon, checkout=checkout, discount_minor=19980)
        coupons.release(checkout)
        # The slot is free again for somebody else.
        coupons.check(coupon, tenant=self.other, plan=self.plan)

    def test_release_does_not_undo_a_confirmed_redemption(self):
        """
        A late failure webhook for an earlier attempt must not free a coupon a
        paying customer has already used.
        """
        coupon = self.coupon(max_redemptions=1, max_per_tenant=0)
        checkout = self._checkout()
        coupons.reserve(tenant=self.tenant, coupon=coupon, checkout=checkout, discount_minor=19980)
        coupons.confirm(checkout)

        released = coupons.release(checkout)
        self.assertEqual(released, 0)
        self.assertEqual(
            CouponRedemption.objects.get(checkout=checkout).status,
            CouponRedemption.Status.REDEEMED,
        )

    def test_confirm_is_idempotent(self):
        """The webhook and the browser callback both arrive on nearly every payment."""
        coupon = self.coupon()
        checkout = self._checkout()
        coupons.reserve(tenant=self.tenant, coupon=coupon, checkout=checkout, discount_minor=19980)

        self.assertEqual(coupons.confirm(checkout), 1)
        self.assertEqual(coupons.confirm(checkout), 0)
        self.assertEqual(CouponRedemption.objects.filter(checkout=checkout).count(), 1)

    def test_reserving_twice_for_one_checkout_returns_the_same_reservation(self):
        coupon = self.coupon()
        checkout = self._checkout()
        first = coupons.reserve(
            tenant=self.tenant, coupon=coupon, checkout=checkout, discount_minor=19980,
        )
        second = coupons.reserve(
            tenant=self.tenant, coupon=coupon, checkout=checkout, discount_minor=19980,
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(CouponRedemption.objects.filter(checkout=checkout).count(), 1)


class QuoteTests(CouponTestCase):
    def test_quote_writes_nothing(self):
        """Trying three codes must not burn three redemptions."""
        self.coupon(max_per_tenant=1)
        for _ in range(3):
            coupons.quote(tenant=self.tenant, code="WELCOME20", plan=self.plan)
        self.assertEqual(CouponRedemption.objects.count(), 0)

    def test_quote_reports_the_price_that_will_be_charged(self):
        self.coupon(kind=Coupon.Kind.PERCENT, value=20)
        quote = coupons.quote(tenant=self.tenant, code="WELCOME20", plan=self.plan)
        self.assertEqual(quote["list_amount_minor"], 99900)
        self.assertEqual(quote["discount_minor"], 19980)
        self.assertEqual(quote["amount_minor"], 79920)


class TrialExtensionTests(CouponTestCase):
    def _trial(self, days=14):
        return Subscription.objects.create(
            tenant=self.tenant, plan=self.plan,
            status=Subscription.Status.TRIALING,
            trial_started_at=timezone.now(),
            trial_ends_at=timezone.now() + timedelta(days=days),
        )

    def test_extension_moves_the_end_date_from_where_it_was(self):
        """
        Extended from the existing end, not from now: redeeming on day one of a
        14-day trial gives 14 + n days, not n.
        """
        subscription = self._trial(days=14)
        original = subscription.trial_ends_at
        self.coupon(code="EXTRA", kind=Coupon.Kind.TRIAL_EXTENSION, value=7)

        coupons.redeem_trial_extension(tenant=self.tenant, code="EXTRA")

        subscription.refresh_from_db()
        self.assertAlmostEqual(
            (subscription.trial_ends_at - original).total_seconds(), 7 * 86400, delta=5,
        )

    def test_a_paying_customer_cannot_redeem_a_trial_extension(self):
        Subscription.objects.create(
            tenant=self.tenant, plan=self.plan, status=Subscription.Status.ACTIVE,
        )
        self.coupon(code="EXTRA", kind=Coupon.Kind.TRIAL_EXTENSION, value=7)
        with self.assertRaises(coupons.CouponError):
            coupons.redeem_trial_extension(tenant=self.tenant, code="EXTRA")

    def test_an_expired_trial_cannot_be_resurrected(self):
        Subscription.objects.create(
            tenant=self.tenant, plan=self.plan, status=Subscription.Status.TRIALING,
            trial_ends_at=timezone.now() - timedelta(days=1),
        )
        self.coupon(code="EXTRA", kind=Coupon.Kind.TRIAL_EXTENSION, value=7)
        with self.assertRaises(coupons.CouponError):
            coupons.redeem_trial_extension(tenant=self.tenant, code="EXTRA")

    def test_a_discount_code_cannot_be_used_as_a_trial_extension(self):
        self._trial()
        self.coupon(code="WELCOME20")
        with self.assertRaises(coupons.CouponError):
            coupons.redeem_trial_extension(tenant=self.tenant, code="WELCOME20")

    def test_extension_respects_the_per_tenant_limit(self):
        self._trial()
        self.coupon(code="EXTRA", kind=Coupon.Kind.TRIAL_EXTENSION, value=7, max_per_tenant=1)
        coupons.redeem_trial_extension(tenant=self.tenant, code="EXTRA")
        with self.assertRaises(coupons.CouponError):
            coupons.redeem_trial_extension(tenant=self.tenant, code="EXTRA")


class CouponApiTests(CouponTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_preview_endpoint_returns_the_discount(self):
        self.coupon()
        response = self.client.post(
            reverse("billing-coupon-preview"), {"plan": self.plan.pk, "code": "WELCOME20"}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["amount_minor"], 79920)

    def test_preview_of_an_unknown_code_is_a_400_with_no_detail_about_why(self):
        response = self.client.post(
            reverse("billing-coupon-preview"), {"plan": self.plan.pk, "code": "NOPE"}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "COUPON_INVALID")

    def test_a_client_cannot_preview_a_plan_it_may_not_buy(self):
        private = Plan.objects.create(name="Private", price_cents=1, is_public=False)
        self.coupon()
        response = self.client.post(
            reverse("billing-coupon-preview"), {"plan": private.pk, "code": "WELCOME20"}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_one_tenant_cannot_see_another_tenants_redemptions(self):
        """There is no endpoint that returns a redemption at all; assert that stays true."""
        from django.urls import NoReverseMatch

        with self.assertRaises(NoReverseMatch):
            reverse("couponredemption-list")
