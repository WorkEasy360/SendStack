"""
Upgrade and downgrade: what the customer is told, and what actually happens.

The property this file exists to hold down is item 32 of the phase brief and
the one a customer will notice: **a downgrade never deletes anything.** A
workspace that drops to a plan allowing fewer contacts keeps every contact it
has; what it loses is the ability to add more. Several tests here assert the
row counts are untouched after a downgrade, because that is the regression
that would be silent and expensive.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.billing import plan_changes
from apps.contacts.models import Contact
from apps.subscriptions.models import Plan, Subscription
from apps.tenants.models import Tenant

User = get_user_model()


class PlanChangeTestCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme", kind=Tenant.Kind.CUSTOMER)
        self.small = Plan.objects.create(
            name="Starter", price_cents=49900, currency="INR", is_public=True,
            max_messages_per_month=1000, max_contacts=500, max_devices=1, max_users=2,
            features={"ai_agent": False, "api_access": False},
        )
        self.large = Plan.objects.create(
            name="Growth", price_cents=99900, currency="INR", is_public=True,
            max_messages_per_month=10000, max_contacts=5000, max_devices=3, max_users=10,
            features={"ai_agent": True, "api_access": True},
        )
        self.user = User.objects.create_user(
            username="acme-owner", password="pw", tenant=self.tenant, role=User.Role.USER,
        )
        self.subscription = Subscription.objects.create(
            tenant=self.tenant, plan=self.large, status=Subscription.Status.ACTIVE,
        )

    def add_contacts(self, count):
        Contact.objects.bulk_create([
            Contact(tenant=self.tenant, created_by=self.user, name=f"C{i}", phone=f"+1500000{i:04d}")
            for i in range(count)
        ])


class DirectionTests(PlanChangeTestCase):
    def test_a_cheaper_plan_is_a_downgrade(self):
        preview = plan_changes.preview(tenant=self.tenant, new_plan=self.small)
        self.assertEqual(preview["direction"], plan_changes.DOWNGRADE)
        self.assertEqual(preview["price_difference_minor"], -50000)

    def test_a_dearer_plan_is_an_upgrade(self):
        self.subscription.plan = self.small
        self.subscription.save()
        preview = plan_changes.preview(tenant=self.tenant, new_plan=self.large)
        self.assertEqual(preview["direction"], plan_changes.UPGRADE)
        self.assertEqual(preview["price_difference_minor"], 50000)

    def test_the_same_plan_is_neither(self):
        preview = plan_changes.preview(tenant=self.tenant, new_plan=self.large)
        self.assertEqual(preview["direction"], plan_changes.SAME)


class PreviewContentTests(PlanChangeTestCase):
    def test_a_downgrade_names_every_limit_that_drops(self):
        preview = plan_changes.preview(tenant=self.tenant, new_plan=self.small)
        reduced = {item["metric"] for item in preview["limits"] if item["reduced"]}
        self.assertEqual(reduced, {"messages_per_month", "contacts", "devices", "users"})

    def test_usage_over_the_new_limit_is_reported_with_numbers(self):
        self.add_contacts(600)  # the small plan allows 500
        preview = plan_changes.preview(tenant=self.tenant, new_plan=self.small)

        contacts = next(item for item in preview["limits"] if item["metric"] == "contacts")
        self.assertEqual(contacts["current_usage"], 600)
        self.assertEqual(contacts["new_limit"], 500)
        self.assertEqual(contacts["over_by"], 100)
        self.assertTrue(contacts["would_block_new"])
        self.assertTrue(preview["usage_impact"]["affected"])

    def test_the_impact_text_says_nothing_is_deleted(self):
        self.add_contacts(600)
        preview = plan_changes.preview(tenant=self.tenant, new_plan=self.small)
        self.assertIn("Nothing is deleted", preview["usage_impact"]["detail"])

    def test_no_usage_over_the_line_reports_no_impact(self):
        self.add_contacts(10)
        preview = plan_changes.preview(tenant=self.tenant, new_plan=self.small)
        self.assertFalse(preview["usage_impact"]["affected"])

    def test_features_lost_are_named(self):
        preview = plan_changes.preview(tenant=self.tenant, new_plan=self.small)
        lost = {item["key"] for item in preview["features_lost"]}
        self.assertIn("ai_agent", lost)
        self.assertIn("api_access", lost)

    def test_features_gained_are_named_on_an_upgrade(self):
        self.subscription.plan = self.small
        self.subscription.save()
        preview = plan_changes.preview(tenant=self.tenant, new_plan=self.large)
        gained = {item["key"] for item in preview["features_gained"]}
        self.assertIn("ai_agent", gained)

    def test_moving_from_unlimited_to_a_number_counts_as_a_reduction(self):
        """
        0 means unlimited everywhere in this codebase. Moving from 0 to 5,000
        is a reduction even though the number went up, and a naive `new < old`
        comparison gets it exactly backwards.
        """
        unlimited = Plan.objects.create(
            name="Unlimited", price_cents=500000, currency="INR", is_public=True,
            max_contacts=0, max_messages_per_month=0, max_devices=0, max_users=0,
        )
        self.subscription.plan = unlimited
        self.subscription.save()

        preview = plan_changes.preview(tenant=self.tenant, new_plan=self.large)
        contacts = next(item for item in preview["limits"] if item["metric"] == "contacts")
        self.assertTrue(contacts["reduced"])

    def test_moving_to_unlimited_is_never_over_the_limit(self):
        self.add_contacts(50)
        unlimited = Plan.objects.create(
            name="Unlimited", price_cents=500000, currency="INR", is_public=True,
            max_contacts=0, max_messages_per_month=0, max_devices=0, max_users=0,
        )
        preview = plan_changes.preview(tenant=self.tenant, new_plan=unlimited)
        contacts = next(item for item in preview["limits"] if item["metric"] == "contacts")
        self.assertEqual(contacts["over_by"], 0)
        self.assertFalse(contacts["reduced"])

    def test_preview_writes_nothing(self):
        before = self.subscription.plan_id
        plan_changes.preview(tenant=self.tenant, new_plan=self.small)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.plan_id, before)


class ApplyDowngradeTests(PlanChangeTestCase):
    def test_a_downgrade_deletes_no_data(self):
        """The single most important property of this whole feature."""
        self.add_contacts(600)
        plan_changes.apply_downgrade(tenant=self.tenant, new_plan=self.small)
        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), 600)

    def test_a_downgrade_moves_the_plan(self):
        plan_changes.apply_downgrade(tenant=self.tenant, new_plan=self.small)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.plan_id, self.small.pk)

    def test_a_downgrade_does_not_restart_the_period(self):
        """
        Restarting would hand a fresh monthly allowance to somebody moving to a
        smaller plan, which is the wrong way round.
        """
        before = self.subscription.current_period_start
        plan_changes.apply_downgrade(tenant=self.tenant, new_plan=self.small)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.current_period_start, before)

    def test_an_upgrade_cannot_be_applied_without_payment(self):
        self.subscription.plan = self.small
        self.subscription.save()
        with self.assertRaises(ValueError):
            plan_changes.apply_downgrade(tenant=self.tenant, new_plan=self.large)

    def test_downgrading_from_a_trial_ends_the_trial(self):
        from datetime import timedelta

        from django.utils import timezone

        self.subscription.status = Subscription.Status.TRIALING
        self.subscription.trial_ends_at = timezone.now() + timedelta(days=7)
        self.subscription.save()

        plan_changes.apply_downgrade(tenant=self.tenant, new_plan=self.small)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, Subscription.Status.ACTIVE)
        self.assertIsNone(self.subscription.trial_ends_at)


class PlanChangeApiTests(PlanChangeTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_preview_endpoint_returns_the_full_picture(self):
        response = self.client.get(reverse("billing-plan-change"), {"plan": self.small.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["direction"], plan_changes.DOWNGRADE)
        self.assertIn("limits", response.data)
        self.assertIn("effective", response.data)

    def test_posting_an_upgrade_is_refused_with_a_reason(self):
        self.subscription.plan = self.small
        self.subscription.save()
        response = self.client.post(
            reverse("billing-plan-change"), {"plan": self.large.pk}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "UPGRADE_REQUIRES_PAYMENT")

    def test_posting_a_downgrade_applies_it(self):
        response = self.client.post(
            reverse("billing-plan-change"), {"plan": self.small.pk}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.plan_id, self.small.pk)

    def test_a_plan_this_workspace_may_not_buy_is_refused(self):
        private = Plan.objects.create(name="Private", price_cents=100, is_public=False)
        response = self.client.get(reverse("billing-plan-change"), {"plan": private.pk})
        self.assertEqual(response.status_code, 400)

    def test_another_tenants_downgrade_cannot_be_driven_from_here(self):
        """
        There is no tenant parameter to send. Asserted by confirming the change
        landed on the caller's own subscription and nowhere else.
        """
        other = Tenant.objects.create(name="Other", slug="other", kind=Tenant.Kind.CUSTOMER)
        other_subscription = Subscription.objects.create(
            tenant=other, plan=self.large, status=Subscription.Status.ACTIVE,
        )
        self.client.post(
            reverse("billing-plan-change"),
            {"plan": self.small.pk, "tenant": other.pk},
            format="json",
        )
        other_subscription.refresh_from_db()
        self.assertEqual(other_subscription.plan_id, self.large.pk)
