"""
Tests for the trial lifecycle.

The load-bearing one is `test_an_expired_trial_loses_access_without_the_sweep`.
Everything else here is ordinary lifecycle checking; that one is the property
the whole design exists for -- **a trial must not become unlimited premium
access because a Celery job died.** If expiry ever comes to depend on the
sweeper running, that test is what will notice.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.contacts.models import Contact
from apps.tenants.models import Tenant

from . import services, trials
from .models import Plan, Subscription, SubscriptionNotice
from .tasks import sweep_subscription_lifecycle

TRIAL_ON = {"TRIALS_ENABLED": True, "BILLING_TRIAL_PLAN": "Trial", "BILLING_TRIAL_DAYS": 14}


class TrialConfigurationTests(TestCase):
    def setUp(self):
        self.plan = Plan.objects.create(owner=None, name="Trial", price_cents=0, max_contacts=100)

    @override_settings(**TRIAL_ON)
    def test_trials_are_available_when_configured(self):
        self.assertTrue(trials.trials_enabled())
        self.assertEqual(trials.trial_plan(), self.plan)
        self.assertEqual(trials.trial_days(), 14)

    @override_settings(TRIALS_ENABLED=False, BILLING_TRIAL_PLAN="Trial")
    def test_the_switch_turns_them_off(self):
        self.assertFalse(trials.trials_enabled())

    @override_settings(TRIALS_ENABLED=True, BILLING_TRIAL_PLAN="")
    def test_an_unconfigured_plan_means_no_trials(self):
        """
        The safe direction. Falling through to "no plan" would mean UNLIMITED
        (see services.get_current_plan), which is the worst possible default
        for a deployment that forgot to name a trial plan.
        """
        self.assertFalse(trials.trials_enabled())
        self.assertIsNone(trials.trial_plan())

    @override_settings(TRIALS_ENABLED=True, BILLING_TRIAL_PLAN="Does Not Exist")
    def test_a_name_matching_nothing_never_guesses_a_plan(self):
        self.assertIsNone(trials.trial_plan())

    @override_settings(**{**TRIAL_ON, "BILLING_TRIAL_DAYS": 0})
    def test_a_nonsense_duration_falls_back_rather_than_creating_a_zero_day_trial(self):
        self.assertEqual(trials.trial_days(), trials.DEFAULT_TRIAL_DAYS)

    @override_settings(**TRIAL_ON)
    def test_a_resellers_plan_is_never_the_trial_plan(self):
        platform = Tenant.objects.create(name="P", slug="tp-platform", kind=Tenant.Kind.PLATFORM)
        reseller = Tenant.objects.create(
            name="R", slug="tp-reseller", kind=Tenant.Kind.RESELLER, parent=platform,
        )
        Plan.objects.create(owner=reseller, name="Trial", price_cents=0)
        self.assertEqual(trials.trial_plan().owner_id, None)


@override_settings(**TRIAL_ON)
class StartTrialTests(TestCase):
    def setUp(self):
        self.plan = Plan.objects.create(owner=None, name="Trial", price_cents=0, max_contacts=100)
        self.tenant = Tenant.objects.create(name="Trialist", slug="trialist", kind=Tenant.Kind.CUSTOMER)

    def test_a_trial_is_a_subscription_on_a_real_plan(self):
        """No second entitlement path for trials. Phase 2 governs them too."""
        subscription = trials.start_trial(self.tenant)
        self.assertEqual(subscription.status, Subscription.Status.TRIALING)
        self.assertEqual(subscription.plan, self.plan)
        self.assertEqual(services.get_current_plan(self.tenant), self.plan)

    def test_the_trial_period_is_the_trial_length(self):
        subscription = trials.start_trial(self.tenant)
        span = subscription.trial_ends_at - subscription.trial_started_at
        self.assertEqual(span.days, 14)
        # The billing period is the trial, not the plan's month: otherwise the
        # monthly allowance would never reset and never matter.
        self.assertEqual(subscription.current_period_end, subscription.trial_ends_at)

    def test_starting_a_trial_twice_returns_the_first_one(self):
        first = trials.start_trial(self.tenant)
        second = trials.start_trial(self.tenant)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Subscription.objects.filter(tenant=self.tenant).count(), 1)

    def test_an_expired_trial_is_not_silently_renewed_by_signing_up_again(self):
        subscription = trials.start_trial(self.tenant)
        subscription.trial_ends_at = timezone.now() - timedelta(days=1)
        subscription.status = Subscription.Status.EXPIRED
        subscription.save()

        again = trials.start_trial(self.tenant)
        self.assertEqual(again.status, Subscription.Status.EXPIRED)

    @override_settings(BILLING_TRIAL_PLAN="")
    def test_an_unconfigured_deployment_refuses_rather_than_granting_unlimited(self):
        with self.assertRaises(trials.TrialUnavailable):
            trials.start_trial(self.tenant)


@override_settings(**TRIAL_ON)
class TrialExpiryTests(TestCase):
    def setUp(self):
        self.plan = Plan.objects.create(
            owner=None, name="Trial", price_cents=0, max_contacts=100, max_messages_per_month=50,
            features={"ai_agent": True, "workflows": True},
        )
        self.tenant = Tenant.objects.create(name="Expiring", slug="expiring", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="expiring-owner", password="x", tenant=self.tenant,
        )
        self.subscription = trials.start_trial(self.tenant)

    def _run_out(self):
        past = timezone.now() - timedelta(days=1)
        self.subscription.trial_ends_at = past
        self.subscription.current_period_end = past
        self.subscription.save(update_fields=["trial_ends_at", "current_period_end"])
        self.subscription.refresh_from_db()

    def test_a_running_trial_has_full_access(self):
        self.assertEqual(self.subscription.access_state(), Subscription.FULL)
        self.assertTrue(services.feature_enabled(self.tenant, "ai_agent"))

    def test_an_expired_trial_loses_access_without_the_sweep(self):
        """
        **The one that matters.** Expiry is derived from the date, so a dead
        lifecycle job cannot turn a trial into free unlimited access.
        """
        self._run_out()
        # Status untouched -- as if the sweeper never ran.
        self.assertEqual(self.subscription.status, Subscription.Status.TRIALING)
        self.assertEqual(self.subscription.access_state(), Subscription.EXPIRED)

    def test_an_expired_trial_cannot_send(self):
        self._run_out()
        allowed, reason = services.check_quota(self.tenant, "messages_per_month", 1)
        self.assertFalse(allowed)
        self.assertTrue(reason)

    def test_an_expired_trial_loses_plan_features(self):
        self._run_out()
        self.assertFalse(services.feature_enabled(self.tenant, "ai_agent"))

    def test_an_expired_trials_window_stops_moving(self):
        """Rolling it would hand out a fresh monthly allowance for free."""
        self._run_out()
        start, end = self.subscription.current_period()
        self.assertEqual(end, self.subscription.current_period_end)

    def test_expiry_deletes_nothing(self):
        Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Kept", phone="+15557770001",
        )
        self._run_out()
        sweep_subscription_lifecycle()

        self.assertTrue(Contact.objects.filter(tenant=self.tenant).exists())
        self.assertTrue(Tenant.objects.filter(pk=self.tenant.pk).exists())
        self.assertTrue(Subscription.objects.filter(tenant=self.tenant).exists())

    def test_the_sweep_records_the_status(self):
        self._run_out()
        result = sweep_subscription_lifecycle()
        self.subscription.refresh_from_db()
        self.assertEqual(result["expired"], 1)
        self.assertEqual(self.subscription.status, Subscription.Status.EXPIRED)

    def test_the_sweep_is_idempotent(self):
        """It may overlap itself, be retried, or be run by hand mid-run."""
        self._run_out()
        first = sweep_subscription_lifecycle()
        second = sweep_subscription_lifecycle()
        self.assertEqual(first["expired"], 1)
        self.assertEqual(second["expired"], 0)

    def test_the_expiry_notice_is_sent_once(self):
        self._run_out()
        with patch("apps.subscriptions.notices.send_billing_notice") as send:
            sweep_subscription_lifecycle()
            sweep_subscription_lifecycle()
        self.assertEqual(send.call_count, 1)

    def test_a_live_trial_is_left_alone(self):
        result = sweep_subscription_lifecycle()
        self.subscription.refresh_from_db()
        self.assertEqual(result["expired"], 0)
        self.assertEqual(self.subscription.status, Subscription.Status.TRIALING)

    def test_days_remaining_rounds_up(self):
        """Eleven hours left is a day left, not zero days."""
        self.subscription.trial_ends_at = timezone.now() + timedelta(hours=11)
        self.assertEqual(self.subscription.trial_days_remaining(), 1)

    def test_days_remaining_never_goes_negative(self):
        self._run_out()
        self.assertEqual(self.subscription.trial_days_remaining(), 0)

    def test_a_non_trial_subscription_has_no_days_remaining(self):
        self.subscription.status = Subscription.Status.ACTIVE
        self.assertIsNone(self.subscription.trial_days_remaining())


@override_settings(**{**TRIAL_ON, "BILLING_TRIAL_REMINDER_DAYS": 3})
class TrialReminderTests(TestCase):
    def setUp(self):
        Plan.objects.create(owner=None, name="Trial", price_cents=0, max_contacts=100)
        self.tenant = Tenant.objects.create(name="Reminded", slug="reminded", kind=Tenant.Kind.CUSTOMER)
        get_user_model().objects.create_user(
            username="reminded-owner", password="x", tenant=self.tenant, email="owner@example.com",
        )
        self.subscription = trials.start_trial(self.tenant)

    def _ends_in(self, days):
        self.subscription.trial_ends_at = timezone.now() + timedelta(days=days)
        self.subscription.save(update_fields=["trial_ends_at"])

    def test_a_trial_ending_soon_is_warned(self):
        self._ends_in(2)
        with patch("apps.subscriptions.notices.send_billing_notice") as send:
            result = sweep_subscription_lifecycle()
        self.assertEqual(result["warned"], 1)
        send.assert_called_once()

    def test_a_trial_ending_later_is_not_warned(self):
        self._ends_in(10)
        result = sweep_subscription_lifecycle()
        self.assertEqual(result["warned"], 0)

    def test_the_reminder_is_sent_once_however_often_the_sweep_runs(self):
        self._ends_in(2)
        with patch("apps.subscriptions.notices.send_billing_notice") as send:
            sweep_subscription_lifecycle()
            sweep_subscription_lifecycle()
            sweep_subscription_lifecycle()
        self.assertEqual(send.call_count, 1)

    @override_settings(BILLING_TRIAL_REMINDER_DAYS=0)
    def test_reminders_can_be_silenced(self):
        self._ends_in(1)
        result = sweep_subscription_lifecycle()
        self.assertEqual(result["warned"], 0)

    def test_a_mail_failure_does_not_undo_the_expiry(self):
        """The state change is already in force; a mail server outage is not a reason to roll it back."""
        past = timezone.now() - timedelta(days=1)
        self.subscription.trial_ends_at = past
        self.subscription.save(update_fields=["trial_ends_at"])

        with patch(
            "apps.subscriptions.notices.send_billing_notice", side_effect=RuntimeError("smtp down"),
        ):
            result = sweep_subscription_lifecycle()

        self.subscription.refresh_from_db()
        self.assertEqual(result["expired"], 1)
        self.assertEqual(self.subscription.status, Subscription.Status.EXPIRED)


class SubscriptionNoticeTests(TestCase):
    def setUp(self):
        plan = Plan.objects.create(owner=None, name="N", price_cents=100)
        tenant = Tenant.objects.create(name="Noticed", slug="noticed", kind=Tenant.Kind.CUSTOMER)
        self.subscription = Subscription.objects.create(tenant=tenant, plan=plan)

    def test_a_notice_can_be_claimed_once(self):
        kind = SubscriptionNotice.Kind.TRIAL_ENDING
        self.assertTrue(SubscriptionNotice.claim(self.subscription, kind, "2026-01-01"))
        self.assertFalse(SubscriptionNotice.claim(self.subscription, kind, "2026-01-01"))

    def test_a_different_reference_is_a_different_notice(self):
        """A second trial later deserves a second warning."""
        kind = SubscriptionNotice.Kind.TRIAL_ENDING
        self.assertTrue(SubscriptionNotice.claim(self.subscription, kind, "2026-01-01"))
        self.assertTrue(SubscriptionNotice.claim(self.subscription, kind, "2026-06-01"))

    def test_a_duplicate_claim_does_not_poison_the_transaction(self):
        """
        The savepoint. Without it, the IntegrityError would abort the whole
        surrounding transaction and turn a duplicate notice into a broken sweep.
        """
        from django.db import transaction

        kind = SubscriptionNotice.Kind.TRIAL_EXPIRED
        with transaction.atomic():
            SubscriptionNotice.claim(self.subscription, kind, "x")
            SubscriptionNotice.claim(self.subscription, kind, "x")
            # Still usable afterwards -- this is the assertion.
            self.assertEqual(SubscriptionNotice.objects.count(), 1)
