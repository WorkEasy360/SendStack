from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenants.models import Tenant

from . import services
from .models import Plan, Subscription


def make_plan(owner=None, **overrides):
    defaults = {"name": "Starter", "price_cents": 900, "max_messages_per_month": 100}
    defaults.update(overrides)
    return Plan.objects.create(owner=owner, **defaults)


class PlanTests(TestCase):
    def test_an_unlisted_feature_is_allowed(self):
        """A plan written before a switch existed must not lose the feature."""
        plan = make_plan(features={"ai_agent": False})

        self.assertFalse(plan.allows("ai_agent"))
        self.assertTrue(plan.allows("click_tracking"))

    def test_zero_means_unlimited(self):
        plan = make_plan(max_contacts=0)
        self.assertEqual(plan.limits["contacts"], services.UNLIMITED)


class QuotaTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Customer", kind=Tenant.Kind.CUSTOMER)

    def test_a_tenant_on_no_plan_is_unlimited(self):
        """Switching subscriptions on for one customer must not stop everyone else."""
        allowed, reason = services.check_quota(self.tenant.pk, "messages_per_month")

        self.assertTrue(allowed)
        self.assertEqual(reason, "")
        self.assertIsNone(services.quota_state(self.tenant.pk)["plan"])

    def test_the_column_name_and_the_metric_name_both_work(self):
        """`max_contacts` and `contacts` are the same ceiling asked two ways."""
        plan = make_plan(max_contacts=1)
        Subscription.objects.create(tenant=self.tenant, plan=plan)

        with patch.object(services, "usage", return_value={"contacts": 1}):
            by_column, _ = services.check_quota(self.tenant.pk, "max_contacts")
            by_metric, _ = services.check_quota(self.tenant.pk, "contacts")

        self.assertFalse(by_column)
        self.assertFalse(by_metric)

    def test_a_renewal_gives_the_period_a_real_end_date(self):
        """`renew()` used to clear the only date the period had."""
        plan = make_plan()
        subscription = Subscription.objects.create(tenant=self.tenant, plan=plan)

        subscription.renew()

        self.assertIsNotNone(subscription.current_period_end)
        self.assertGreater(subscription.current_period_end, subscription.current_period_start)

    def test_the_usage_window_rolls_without_anybody_renewing(self):
        """
        Nothing in this product renews on a schedule, so the month has to move
        by itself -- otherwise a customer's first month is their only one.
        """
        plan = make_plan(max_messages_per_month=10)
        subscription = Subscription.objects.create(
            tenant=self.tenant, plan=plan,
            current_period_start=timezone.now() - timezone.timedelta(days=95),
        )

        start, end = subscription.current_period()

        self.assertLessEqual(start, timezone.now())
        self.assertGreater(end, timezone.now())

    def test_a_cancelled_period_does_not_roll(self):
        """Rolling a cancelled subscription's window would be renewing it for free."""
        plan = make_plan()
        ended = timezone.now() - timezone.timedelta(days=2)
        subscription = Subscription.objects.create(
            tenant=self.tenant, plan=plan, status=Subscription.Status.CANCELLED,
            current_period_start=timezone.now() - timezone.timedelta(days=32),
            current_period_end=ended,
        )

        _, end = subscription.current_period()

        self.assertEqual(end, ended)
        self.assertEqual(subscription.access_state(), Subscription.EXPIRED)

    def test_a_limit_that_is_not_reached_allows_the_work(self):
        plan = make_plan(max_messages_per_month=10)
        Subscription.objects.create(tenant=self.tenant, plan=plan)

        with patch.object(services, "usage", return_value={"messages_per_month": 3}):
            allowed, _ = services.check_quota(self.tenant.pk, "messages_per_month")

        self.assertTrue(allowed)

    def test_a_reached_limit_refuses_and_names_the_plan(self):
        plan = make_plan(name="Starter", max_messages_per_month=10)
        Subscription.objects.create(tenant=self.tenant, plan=plan)

        with patch.object(services, "usage", return_value={"messages_per_month": 10}):
            allowed, reason = services.check_quota(self.tenant.pk, "messages_per_month")

        self.assertFalse(allowed)
        self.assertIn("Starter", reason)

    def test_a_cancellation_keeps_working_until_the_period_ends(self):
        """They paid for the month; cancelling does not take the rest of it away."""
        plan = make_plan(max_messages_per_month=0)
        Subscription.objects.create(
            tenant=self.tenant, plan=plan, status=Subscription.Status.CANCELLED,
            current_period_end=timezone.now() + timezone.timedelta(days=5),
        )

        allowed, _ = services.check_quota(self.tenant.pk, "messages_per_month")
        self.assertTrue(allowed)

    def test_a_cancelled_subscription_stops_once_the_period_has_ended(self):
        plan = make_plan(max_messages_per_month=0)
        Subscription.objects.create(
            tenant=self.tenant, plan=plan, status=Subscription.Status.CANCELLED,
            current_period_start=timezone.now() - timezone.timedelta(days=60),
            current_period_end=timezone.now() - timezone.timedelta(days=1),
        )

        allowed, reason = services.check_quota(self.tenant.pk, "messages_per_month")

        self.assertFalse(allowed)
        self.assertIn("cancelled", reason.lower())

    def test_a_trial_is_treated_as_live(self):
        plan = make_plan(max_messages_per_month=0)
        Subscription.objects.create(tenant=self.tenant, plan=plan, status=Subscription.Status.TRIALING)

        allowed, _ = services.check_quota(self.tenant.pk, "messages_per_month")
        self.assertTrue(allowed)

    def test_a_broken_quota_lookup_refuses_the_work(self):
        """
        Fails closed, which is the reversal Phase 2 turns on.

        A lookup that raises cannot say the resource is free, and handing out
        a paid resource on the strength of an exception is handing out the
        product. The refusal is deliberately not phrased as a limit -- nothing
        is known about the limit -- so the customer is told to try again
        rather than to upgrade.
        """
        plan = make_plan(max_contacts=1)
        Subscription.objects.create(tenant=self.tenant, plan=plan)

        with patch.object(services, "usage", side_effect=RuntimeError("database on fire")):
            allowed, reason = services.check_quota(self.tenant.pk, "contacts")

        self.assertFalse(allowed)
        self.assertIn("try again", reason.lower())

    def test_a_broken_feature_lookup_refuses_the_feature(self):
        make_plan(name="Growth")
        with patch.object(services, "get_active_subscription", side_effect=RuntimeError("nope")):
            self.assertFalse(services.feature_enabled(self.tenant.pk, "ai_agent"))

    def test_quota_state_reports_limit_usage_and_remainder(self):
        plan = make_plan(max_messages_per_month=10)
        Subscription.objects.create(tenant=self.tenant, plan=plan)

        with patch.object(services, "usage", return_value={"messages_per_month": 4}):
            state = services.quota_state(self.tenant.pk)

        messages = state["metrics"]["messages_per_month"]
        self.assertEqual(messages["limit"], 10)
        self.assertEqual(messages["used"], 4)
        self.assertEqual(messages["remaining"], 6)
        self.assertFalse(messages["exceeded"])

    def test_features_follow_the_plan(self):
        plan = make_plan(features={"ai_agent": False})
        Subscription.objects.create(tenant=self.tenant, plan=plan)

        self.assertFalse(services.feature_enabled(self.tenant.pk, "ai_agent"))
        self.assertTrue(services.feature_enabled(self.tenant.pk, "workflows"))


class SendGateQuotaTests(TestCase):
    """The quota only matters if it actually stops a send."""

    def setUp(self):
        from apps.messaging.models import Message

        self.tenant = Tenant.objects.create(name="Customer", kind=Tenant.Kind.CUSTOMER)
        self.message = Message(tenant_id=self.tenant.pk, kind=Message.Kind.MARKETING)

    def test_an_exhausted_plan_blocks_rather_than_holds(self):
        from apps.messaging import gating

        with patch(
            "apps.subscriptions.services.claim_message_allowance",
            return_value=(False, "The Starter plan allows 10."),
        ):
            decision = gating.evaluate(self.message, account=None)

        self.assertEqual(decision.action, gating.BLOCK)
        self.assertIn("Starter", decision.reason)

    def test_a_notice_is_exempt(self):
        """The message telling somebody they are over their limit must still go."""
        from apps.messaging import gating
        from apps.messaging.models import Message

        self.message.kind = Message.Kind.NOTICE

        with patch(
            "apps.subscriptions.services.claim_message_allowance", return_value=(False, "Full."),
        ) as claimed:
            try:
                gating.evaluate(self.message, account=None)
            except Exception:
                pass  # A NOTICE runs on past the quota into the number checks.

        claimed.assert_not_called()
        self.assertEqual(self.message.kind, Message.Kind.NOTICE)


class PlanAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        user_model = get_user_model()

        self.reseller_tenant = Tenant.objects.create(name="Reseller A", kind=Tenant.Kind.RESELLER)
        self.other_tenant = Tenant.objects.create(name="Reseller B", kind=Tenant.Kind.RESELLER)
        self.customer = Tenant.objects.create(
            name="Customer", kind=Tenant.Kind.CUSTOMER, parent=self.reseller_tenant,
        )

        self.reseller = user_model.objects.create_user(
            username="reseller", password="pw", tenant=self.reseller_tenant, role=user_model.Role.RESELLER,
        )
        self.member = user_model.objects.create_user(
            username="member", password="pw", tenant=self.reseller_tenant, role=user_model.Role.USER,
        )

    def test_a_reseller_sees_only_their_own_plans(self):
        make_plan(owner=self.reseller_tenant, name="Mine")
        make_plan(owner=self.other_tenant, name="Theirs")

        self.client.force_authenticate(self.reseller)
        response = self.client.get(reverse("plan-list"))

        names = [row["name"] for row in response.data["results"]]
        self.assertEqual(names, ["Mine"])

    def test_a_created_plan_belongs_to_the_creator(self):
        self.client.force_authenticate(self.reseller)
        response = self.client.post(reverse("plan-list"), {"name": "Growth", "price_cents": 2900}, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Plan.objects.get(name="Growth").owner_id, self.reseller_tenant.pk)

    def test_an_ordinary_user_cannot_create_a_plan(self):
        self.client.force_authenticate(self.member)
        response = self.client.post(reverse("plan-list"), {"name": "Nope"}, format="json")

        self.assertEqual(response.status_code, 403)

    def test_an_unknown_feature_switch_is_refused(self):
        self.client.force_authenticate(self.reseller)
        response = self.client.post(
            reverse("plan-list"), {"name": "Odd", "features": {"time_travel": True}}, format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("features", response.data)

    def test_a_reseller_cannot_subscribe_a_tenant_that_is_not_theirs(self):
        plan = make_plan(owner=self.reseller_tenant)
        stranger = Tenant.objects.create(name="Stranger", kind=Tenant.Kind.CUSTOMER, parent=self.other_tenant)

        self.client.force_authenticate(self.reseller)
        response = self.client.post(
            reverse("subscription-list"), {"tenant": stranger.pk, "plan": plan.pk}, format="json",
        )

        self.assertEqual(response.status_code, 403)

    def test_a_reseller_can_subscribe_their_own_customer(self):
        plan = make_plan(owner=self.reseller_tenant)

        self.client.force_authenticate(self.reseller)
        response = self.client.post(
            reverse("subscription-list"), {"tenant": self.customer.pk, "plan": plan.pk}, format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Subscription.objects.get(tenant=self.customer).plan_id, plan.pk)

    def test_renewing_rolls_the_period_and_resets_the_allowance(self):
        plan = make_plan(owner=self.reseller_tenant)
        subscription = Subscription.objects.create(
            tenant=self.customer, plan=plan,
            current_period_start=timezone.now() - timezone.timedelta(days=45),
            status=Subscription.Status.PAST_DUE,
        )

        self.client.force_authenticate(self.reseller)
        response = self.client.post(reverse("subscription-renew", args=[subscription.pk]))

        subscription.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(subscription.status, Subscription.Status.ACTIVE)
        self.assertGreater(subscription.current_period_start, timezone.now() - timezone.timedelta(minutes=1))

    def test_a_reseller_cannot_read_another_resellers_customer_usage(self):
        stranger = Tenant.objects.create(name="Stranger", kind=Tenant.Kind.CUSTOMER, parent=self.other_tenant)

        self.client.force_authenticate(self.reseller)
        response = self.client.get(reverse("tenant-usage", args=[stranger.pk]))

        self.assertEqual(response.status_code, 404)
