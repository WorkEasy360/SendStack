"""
The business metrics: are they what the definitions say they are?

Most of this file is about the *definitions* rather than about arithmetic,
because the arithmetic is easy and the definitions are what get argued about
later. A trial must not appear in MRR. A yearly plan must contribute a twelfth.
A quiet workspace must not be counted as churn. Each of those is a decision
somebody could reasonably have made the other way, so each one gets a test
that will fail loudly if it is silently changed.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.models import Checkout, Payment
from apps.reports import business
from apps.subscriptions.models import Plan, Subscription
from apps.tenants.models import Tenant

User = get_user_model()


class BusinessTestCase(TestCase):
    def setUp(self):
        self.monthly = Plan.objects.create(
            name="Monthly", price_cents=100000, currency="INR",
            interval=Plan.Interval.MONTHLY, is_public=True,
        )
        self.yearly = Plan.objects.create(
            name="Yearly", price_cents=1200000, currency="INR",
            interval=Plan.Interval.YEARLY, is_public=True,
        )
        self.free = Plan.objects.create(name="Free", price_cents=0, currency="INR")

    def tenant(self, slug):
        return Tenant.objects.create(name=slug, slug=slug, kind=Tenant.Kind.CUSTOMER)

    def subscribe(self, slug, plan, status=Subscription.Status.ACTIVE, **kwargs):
        return Subscription.objects.create(
            tenant=self.tenant(slug), plan=plan, status=status, **kwargs,
        )


class MrrTests(BusinessTestCase):
    def test_a_monthly_plan_contributes_its_price(self):
        self.subscribe("a", self.monthly)
        self.assertEqual(business.mrr()["mrr_minor"], 100000)

    def test_a_yearly_plan_contributes_one_twelfth(self):
        self.subscribe("a", self.yearly)
        self.assertEqual(business.mrr()["mrr_minor"], 100000)

    def test_a_trial_contributes_nothing(self):
        """The most common way a SaaS dashboard flatters itself."""
        self.subscribe(
            "a", self.monthly, status=Subscription.Status.TRIALING,
            trial_ends_at=timezone.now() + timedelta(days=7),
        )
        result = business.mrr()
        self.assertEqual(result["mrr_minor"], 0)
        self.assertEqual(result["paying_customers"], 0)
        self.assertEqual(result["trialing_customers"], 1)

    def test_a_cancelled_subscription_inside_its_paid_period_still_counts(self):
        """They paid for the month and are still being served."""
        self.subscribe(
            "a", self.monthly, status=Subscription.Status.CANCELLED,
            current_period_end=timezone.now() + timedelta(days=10),
            cancelled_at=timezone.now(),
        )
        self.assertEqual(business.mrr()["mrr_minor"], 100000)

    def test_a_cancelled_subscription_past_its_period_does_not(self):
        self.subscribe(
            "a", self.monthly, status=Subscription.Status.CANCELLED,
            current_period_start=timezone.now() - timedelta(days=60),
            current_period_end=timezone.now() - timedelta(days=1),
            cancelled_at=timezone.now() - timedelta(days=30),
        )
        self.assertEqual(business.mrr()["mrr_minor"], 0)

    def test_an_expired_subscription_does_not_count(self):
        self.subscribe("a", self.monthly, status=Subscription.Status.EXPIRED)
        self.assertEqual(business.mrr()["mrr_minor"], 0)

    def test_a_free_plan_is_not_a_paying_customer(self):
        self.subscribe("a", self.free)
        result = business.mrr()
        self.assertEqual(result["mrr_minor"], 0)
        self.assertEqual(result["paying_customers"], 0)

    def test_yearly_prices_do_not_lose_minor_units_when_summed(self):
        """
        Integer division per subscription would lose up to a minor unit each
        time. 100 subscriptions on a plan priced so it does not divide evenly
        must still sum exactly.
        """
        odd = Plan.objects.create(
            name="Odd", price_cents=100, currency="INR", interval=Plan.Interval.YEARLY,
        )
        for i in range(12):
            self.subscribe(f"odd{i}", odd)
        # 12 x (100/12) = 100 exactly. Integer division would give 12 x 8 = 96.
        self.assertEqual(business.mrr()["mrr_minor"], 100)

    def test_arr_is_twelve_times_mrr(self):
        self.subscribe("a", self.monthly)
        result = business.mrr()
        self.assertEqual(result["arr_minor"], result["mrr_minor"] * 12)

    def test_arpu_divides_only_by_paying_customers(self):
        self.subscribe("a", self.monthly)
        self.subscribe("b", self.monthly)
        self.subscribe(
            "c", self.monthly, status=Subscription.Status.TRIALING,
            trial_ends_at=timezone.now() + timedelta(days=7),
        )
        self.assertEqual(business.mrr()["arpu_minor"], 100000)


class ChurnTests(BusinessTestCase):
    def test_churn_counts_cancellations_not_inactivity(self):
        now = timezone.now()
        since = now - timedelta(days=30)

        # Cancelled inside the window: churn.
        self.subscribe("a", self.monthly, status=Subscription.Status.CANCELLED, cancelled_at=now - timedelta(days=2))
        # Active but silent: not churn.
        self.subscribe("b", self.monthly)

        movements = business.revenue_movements(since=since, until=now)
        self.assertEqual(movements["cancellations"], 1)
        self.assertEqual(movements["churned"], 1)

    def test_churn_rate_is_null_rather_than_zero_when_there_is_no_base(self):
        """0% churn on a platform with no customers is a false reassurance."""
        now = timezone.now()
        movements = business.revenue_movements(since=now - timedelta(days=30), until=now)
        self.assertIsNone(movements["churn_rate"])

    def test_collected_revenue_is_separate_from_mrr(self):
        tenant = self.tenant("payer")
        checkout = Checkout.objects.create(
            tenant=tenant, plan=self.yearly, provider="test",
            amount_minor=1200000, currency="INR",
        )
        Payment.objects.create(
            checkout=checkout, tenant=tenant, provider="test", provider_payment_id="p1",
            amount_minor=1200000, currency="INR", status=Payment.Status.CAPTURED,
            captured_at=timezone.now(),
        )
        now = timezone.now()
        movements = business.revenue_movements(since=now - timedelta(days=30), until=now)
        # The full year lands in cash; MRR would show a twelfth.
        self.assertEqual(movements["revenue_collected_minor"], 1200000)


class ActivationTests(BusinessTestCase):
    def test_activation_is_cohorted_by_creation_date(self):
        from apps.onboarding.models import OnboardingState

        now = timezone.now()
        recent = self.tenant("recent")
        OnboardingState.objects.create(tenant=recent, activated_at=now)

        result = business.activation(since=now - timedelta(days=30), until=now + timedelta(minutes=1))
        self.assertEqual(result["signups"], 1)
        self.assertEqual(result["activated"], 1)
        self.assertEqual(result["activation_rate"], 100.0)

    def test_activation_rate_is_null_with_no_signups(self):
        now = timezone.now()
        result = business.activation(since=now - timedelta(days=1), until=now)
        self.assertIsNone(result["activation_rate"])


class RetentionTests(BusinessTestCase):
    def test_active_means_sent_not_signed_in(self):
        from apps.messaging.models import Message
        from apps.whatsapp.models import WhatsAppAccount

        tenant = self.tenant("sender")
        user = User.objects.create_user(username="u", password="pw", tenant=tenant)
        account = WhatsAppAccount.objects.create(
            tenant=tenant, user=user, name="L",
            status=WhatsAppAccount.Status.CONNECTED, phone_number="+15000000001",
        )
        Message.objects.create(
            tenant=tenant, whatsapp_account=account, body="x", status=Message.Status.SENT,
        )
        # A workspace that exists and never sent.
        self.tenant("lurker")

        now = timezone.now()
        result = business.retention(since=now - timedelta(days=7), until=now + timedelta(minutes=1))
        self.assertEqual(result["active_customers"], 1)


class HealthSignalTests(BusinessTestCase):
    def test_a_device_with_no_workspace_does_not_break_the_report(self):
        """
        `WhatsAppAccount.tenant` is nullable -- it is backfilled from `user`,
        and a row can predate that. A real deployment had one, and it took the
        whole health endpoint down with a 500 that no unit test had provoked
        because every fixture set a tenant.
        """
        from apps.whatsapp.models import WhatsAppAccount

        tenant = self.tenant("acme")
        user = User.objects.create_user(username="u", password="pw", tenant=tenant)
        account = WhatsAppAccount.objects.create(
            tenant=tenant, user=user, name="Orphan",
            status=WhatsAppAccount.Status.DISCONNECTED, phone_number="+15000000002",
        )
        # `save()` backfills the tenant from the user, so the legacy shape has
        # to be written past it -- which is exactly how the real row got there:
        # it predates the backfill.
        WhatsAppAccount.objects.filter(pk=account.pk).update(tenant=None)

        result = business.health_signals()
        self.assertEqual(result["signals"]["device_disconnected"], [])

    def test_a_disconnected_device_with_a_workspace_is_listed(self):
        from apps.whatsapp.models import WhatsAppAccount

        tenant = self.tenant("acme")
        user = User.objects.create_user(username="u", password="pw", tenant=tenant)
        WhatsAppAccount.objects.create(
            tenant=tenant, user=user, name="Sales line",
            status=WhatsAppAccount.Status.DISCONNECTED, phone_number="+15000000003",
        )

        result = business.health_signals()
        rows = result["signals"]["device_disconnected"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device"], "Sales line")

    def test_every_signal_group_is_present_even_when_empty(self):
        """The dashboard renders a heading per group; a missing key is a crash."""
        result = business.health_signals()
        for key in (
            "never_activated", "gone_quiet", "device_disconnected",
            "trial_ending", "repeated_failures",
        ):
            self.assertIn(key, result["signals"])
            self.assertIn(key, result["counts"])


class ReconciliationTests(BusinessTestCase):
    def test_a_captured_payment_with_no_subscription_is_flagged(self):
        tenant = self.tenant("orphan")
        checkout = Checkout.objects.create(
            tenant=tenant, plan=self.monthly, provider="test",
            amount_minor=100000, currency="INR",
        )
        Payment.objects.create(
            checkout=checkout, tenant=tenant, provider="test", provider_payment_id="p9",
            amount_minor=100000, currency="INR", status=Payment.Status.CAPTURED,
            captured_at=timezone.now(),
        )
        now = timezone.now()
        result = business.reconciliation(since=now - timedelta(days=1), until=now + timedelta(minutes=1))
        self.assertFalse(result["clean"])
        self.assertEqual(len(result["captured_without_active_subscription"]), 1)

    def test_a_checkout_marked_paid_without_a_payment_is_flagged(self):
        tenant = self.tenant("ghost")
        Checkout.objects.create(
            tenant=tenant, plan=self.monthly, provider="test",
            amount_minor=100000, currency="INR", status=Checkout.Status.PAID,
        )
        now = timezone.now()
        result = business.reconciliation(since=now - timedelta(days=1), until=now + timedelta(minutes=1))
        self.assertEqual(len(result["marked_paid_without_captured_payment"]), 1)

    def test_a_healthy_deployment_reconciles_clean(self):
        now = timezone.now()
        result = business.reconciliation(since=now - timedelta(days=1), until=now)
        self.assertTrue(result["clean"])


class AccessTests(BusinessTestCase):
    def setUp(self):
        super().setUp()
        self.tenant_obj = self.tenant("acme")
        self.customer = User.objects.create_user(
            username="cust", password="pw", tenant=self.tenant_obj, role=User.Role.USER,
        )
        self.reseller = User.objects.create_user(
            username="res", password="pw", tenant=self.tenant_obj, role=User.Role.RESELLER,
        )
        self.staff = User.objects.create_user(
            username="staff", password="pw", tenant=self.tenant_obj, role=User.Role.SUPERADMIN,
        )

    def _get(self, user, name):
        client = APIClient()
        client.force_authenticate(user)
        return client.get(reverse(name))

    def test_a_customer_gets_404_not_403(self):
        """403 would confirm a platform-wide revenue report lives at this path."""
        for name in (
            "report-business", "report-business-revenue", "report-business-retention",
            "report-business-activation", "report-business-health", "report-business-reconciliation",
        ):
            response = self._get(self.customer, name)
            self.assertEqual(response.status_code, 404, name)

    def test_a_reseller_cannot_read_the_whole_deployments_revenue(self):
        response = self._get(self.reseller, "report-business-revenue")
        self.assertEqual(response.status_code, 404)

    def test_platform_staff_can(self):
        response = self._get(self.staff, "report-business")
        self.assertEqual(response.status_code, 200)
        self.assertIn("revenue", response.data)
        self.assertIn("definitions", response.data)

    def test_every_metric_ships_with_its_definition(self):
        response = self._get(self.staff, "report-business")
        definitions = response.data["definitions"]
        for key in ("mrr", "churned", "churn_rate", "activation_rate"):
            self.assertIn(key, definitions)
            self.assertTrue(len(definitions[key]) > 20, key)

    def test_the_window_is_clamped(self):
        client = APIClient()
        client.force_authenticate(self.staff)
        response = client.get(reverse("report-business"), {"days": "99999"})
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(response.data["window_days"], 365)

    def test_a_nonsense_window_falls_back_rather_than_erroring(self):
        client = APIClient()
        client.force_authenticate(self.staff)
        response = client.get(reverse("report-business"), {"days": "banana"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["window_days"], 30)
