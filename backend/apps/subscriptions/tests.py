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

    def test_a_cancelled_subscription_stops_the_work(self):
        plan = make_plan(max_messages_per_month=0)
        Subscription.objects.create(tenant=self.tenant, plan=plan, status=Subscription.Status.CANCELLED)

        allowed, reason = services.check_quota(self.tenant.pk, "messages_per_month")

        self.assertFalse(allowed)
        self.assertIn("cancelled", reason.lower())

    def test_a_trial_is_treated_as_live(self):
        plan = make_plan(max_messages_per_month=0)
        Subscription.objects.create(tenant=self.tenant, plan=plan, status=Subscription.Status.TRIALING)

        allowed, _ = services.check_quota(self.tenant.pk, "messages_per_month")
        self.assertTrue(allowed)

    def test_a_broken_quota_lookup_lets_the_work_through(self):
        """A billing table must never be the reason a customer's messages stop."""
        plan = make_plan(max_messages_per_month=1)
        Subscription.objects.create(tenant=self.tenant, plan=plan)

        with patch.object(services, "usage", side_effect=RuntimeError("database on fire")):
            allowed, _ = services.check_quota(self.tenant.pk, "messages_per_month")

        self.assertTrue(allowed)

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

        with patch("apps.subscriptions.services.check_quota", return_value=(False, "The Starter plan allows 10.")):
            decision = gating.evaluate(self.message, account=None)

        self.assertEqual(decision.action, gating.BLOCK)
        self.assertIn("Starter", decision.reason)

    def test_a_notice_is_exempt(self):
        """The message telling somebody they are over their limit must still go."""
        from apps.messaging import gating
        from apps.messaging.models import Message

        self.message.kind = Message.Kind.NOTICE

        with patch("apps.subscriptions.services.check_quota", return_value=(False, "Full.")) as checked:
            try:
                gating.evaluate(self.message, account=None)
            except Exception:
                pass  # A NOTICE runs on past the quota into the number checks.

        checked.assert_not_called()
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
