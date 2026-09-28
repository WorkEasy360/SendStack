"""
Tests for the durable scheduler.

The requirement these exist to prove is a single sentence: **a scheduled
campaign must still send after Redis has lost everything.** Every test here is
either that sentence or one of the ways it can be undone -- two workers
sending it twice, a cancelled campaign sending anyway, a dead worker stranding
it for ever, an expired subscription sending regardless.

Redis loss is simulated rather than performed: the point of the design is that
the broker holds no scheduling state at all, so "Redis was flushed" is
indistinguishable from "no Celery task was ever published". The tests assert
the stronger property -- the campaign goes out with **nothing but the database
row** -- which is what makes the Redis question moot.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.campaigns.models import Campaign
from apps.campaigns.services import launch
from apps.contacts.models import Contact
from apps.subscriptions.models import Plan, Subscription
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount

from . import services
from .models import ScheduledJob
from .registry import JobPrecondition, UnknownJobKind, handler_for, register


def _account(tenant, user, status=WhatsAppAccount.Status.CONNECTED):
    account = WhatsAppAccount(tenant=tenant, user=user, name="Line", phone_number_id="1", status=status)
    account.set_access_token("token")
    account.set_app_secret("secret")
    account.save()
    return account


class ScheduledJobModelTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Sched Model", slug="sched-model", kind=Tenant.Kind.CUSTOMER)

    def test_only_one_live_job_per_object(self):
        """The partial unique constraint is the duplicate-send protection."""
        from django.db import IntegrityError, transaction

        services.schedule(
            kind=ScheduledJob.Kind.CAMPAIGN_DISPATCH, object_id=1,
            tenant_id=self.tenant.pk, run_at=timezone.now() + timedelta(hours=1),
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ScheduledJob.objects.create(
                tenant=self.tenant, kind=ScheduledJob.Kind.CAMPAIGN_DISPATCH,
                object_id=1, scheduled_for=timezone.now(),
            )

    def test_a_finished_job_does_not_block_a_new_one(self):
        """History is kept; only *live* jobs are exclusive."""
        job = services.schedule(
            kind=ScheduledJob.Kind.CAMPAIGN_DISPATCH, object_id=2,
            tenant_id=self.tenant.pk, run_at=timezone.now(),
        )
        job.status = ScheduledJob.Status.COMPLETED
        job.save(update_fields=["status"])

        again = services.schedule(
            kind=ScheduledJob.Kind.CAMPAIGN_DISPATCH, object_id=2,
            tenant_id=self.tenant.pk, run_at=timezone.now() + timedelta(days=1),
        )
        self.assertNotEqual(job.pk, again.pk)
        self.assertEqual(ScheduledJob.objects.filter(object_id=2).count(), 2)


class SchedulingServiceTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Sched Svc", slug="sched-svc", kind=Tenant.Kind.CUSTOMER)
        self.kind = ScheduledJob.Kind.CAMPAIGN_DISPATCH

    def _schedule(self, object_id=1, run_at=None):
        return services.schedule(
            kind=self.kind, object_id=object_id, tenant_id=self.tenant.pk,
            run_at=run_at or timezone.now() + timedelta(hours=1),
        )

    def test_rescheduling_updates_the_same_row(self):
        """
        The stale-schedule bug that broker ETAs had: an edit that moved Friday's
        campaign but left Tuesday's dispatch armed.
        """
        job = self._schedule()
        later = timezone.now() + timedelta(days=3)
        again = services.schedule(
            kind=self.kind, object_id=1, tenant_id=self.tenant.pk, run_at=later,
        )
        self.assertEqual(job.pk, again.pk)
        self.assertEqual(ScheduledJob.objects.filter(object_id=1).count(), 1)
        self.assertAlmostEqual(again.scheduled_for, later, delta=timedelta(seconds=1))

    def test_rescheduling_a_running_job_is_refused(self):
        job = self._schedule()
        job.status = ScheduledJob.Status.PROCESSING
        job.save(update_fields=["status"])
        with self.assertRaises(services.JobInFlight):
            self._schedule()

    def test_cancel_marks_live_jobs(self):
        job = self._schedule()
        self.assertEqual(services.cancel(kind=self.kind, object_id=1, reason="stopped"), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, ScheduledJob.Status.CANCELLED)

    def test_claim_only_takes_due_jobs(self):
        self._schedule(object_id=1, run_at=timezone.now() - timedelta(minutes=5))
        self._schedule(object_id=2, run_at=timezone.now() + timedelta(hours=5))

        claimed = services.claim_due()
        self.assertEqual(len(claimed), 1)
        self.assertEqual(ScheduledJob.objects.get(pk=claimed[0]).object_id, 1)

    def test_claiming_spends_an_attempt(self):
        """
        Counted on the claim, not on the failure. A worker killed mid-job
        reports nothing, so a budget that only counted reported failures would
        never run out and the job would retry for ever.
        """
        job = self._schedule(run_at=timezone.now() - timedelta(minutes=1))
        services.claim_due()
        job.refresh_from_db()
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.status, ScheduledJob.Status.PROCESSING)

    def test_a_claimed_job_is_not_claimed_again(self):
        """One worker, one job. The second poll finds nothing."""
        self._schedule(run_at=timezone.now() - timedelta(minutes=1))
        first = services.claim_due()
        second = services.claim_due()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_stale_lease_is_reclaimed(self):
        """A deploy mid-job is a delay, not a lost campaign."""
        job = self._schedule(run_at=timezone.now() - timedelta(hours=2))
        services.claim_due()
        job.refresh_from_db()
        job.locked_at = timezone.now() - timedelta(seconds=services.lease_seconds() + 60)
        job.save(update_fields=["locked_at"])

        result = services.reclaim_stale()
        job.refresh_from_db()
        self.assertEqual(result["requeued"], 1)
        self.assertEqual(job.status, ScheduledJob.Status.PENDING)

    def test_reclaim_buries_a_job_out_of_attempts(self):
        job = self._schedule(run_at=timezone.now() - timedelta(hours=2))
        job.attempts = job.max_attempts
        job.status = ScheduledJob.Status.PROCESSING
        job.locked_at = timezone.now() - timedelta(seconds=services.lease_seconds() + 60)
        job.save(update_fields=["attempts", "status", "locked_at"])

        result = services.reclaim_stale()
        job.refresh_from_db()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(job.status, ScheduledJob.Status.FAILED)

    def test_a_fresh_lease_is_left_alone(self):
        self._schedule(run_at=timezone.now() - timedelta(minutes=1))
        services.claim_due()
        self.assertEqual(services.reclaim_stale(), {"requeued": 0, "failed": 0})

    def test_overdue_count_sees_a_stopped_scheduler(self):
        self._schedule(object_id=9, run_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(services.overdue_count(), 1)
        services.claim_due()
        self.assertEqual(services.overdue_count(), 0)


class JobExecutionTests(TestCase):
    """`execute` and the handler contract, with a handler of the tests' own."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Sched Exec", slug="sched-exec", kind=Tenant.Kind.CUSTOMER)
        self.kind = ScheduledJob.Kind.CAMPAIGN_DISPATCH

    def _claimed_job(self, object_id=1):
        services.schedule(
            kind=self.kind, object_id=object_id, tenant_id=self.tenant.pk,
            run_at=timezone.now() - timedelta(minutes=1),
        )
        return ScheduledJob.objects.get(pk=services.claim_due()[0])

    def test_a_successful_handler_completes_the_job(self):
        job = self._claimed_job()
        with patch.object(services, "handler_for", return_value=lambda _job: None):
            self.assertEqual(services.execute(job.pk), ScheduledJob.Status.COMPLETED)
        job.refresh_from_db()
        self.assertIsNotNone(job.executed_at)

    def test_a_failing_handler_goes_back_to_pending_while_attempts_remain(self):
        job = self._claimed_job()

        def boom(_job):
            raise RuntimeError("provider down")

        with patch.object(services, "handler_for", return_value=boom):
            self.assertEqual(services.execute(job.pk), ScheduledJob.Status.PENDING)
        job.refresh_from_db()
        self.assertIn("provider down", job.last_error)
        self.assertIsNone(job.locked_at)

    def test_a_failing_handler_fails_the_job_once_attempts_are_spent(self):
        job = self._claimed_job()
        job.attempts = job.max_attempts
        job.save(update_fields=["attempts"])

        def boom(_job):
            raise RuntimeError("still down")

        with patch.object(services, "handler_for", return_value=boom):
            self.assertEqual(services.execute(job.pk), ScheduledJob.Status.FAILED)

    def test_a_precondition_completes_rather_than_failing(self):
        """
        The world moved on. Retrying would look again and decide the same,
        so it is finished -- with the reason where somebody can read it.
        """
        job = self._claimed_job()

        def moved_on(_job):
            raise JobPrecondition("The campaign was cancelled.")

        with patch.object(services, "handler_for", return_value=moved_on):
            self.assertEqual(services.execute(job.pk), ScheduledJob.Status.COMPLETED)
        job.refresh_from_db()
        self.assertEqual(job.last_error, "The campaign was cancelled.")

    def test_a_cancelled_job_is_not_run(self):
        """
        The second delivery of a task Celery retried, and the campaign the
        customer stopped in between.
        """
        job = self._claimed_job()
        job.status = ScheduledJob.Status.CANCELLED
        job.save(update_fields=["status"])

        called = []
        with patch.object(services, "handler_for", return_value=lambda j: called.append(j)):
            services.execute(job.pk)
        self.assertEqual(called, [])

    def test_an_unroutable_kind_fails_loudly(self):
        job = self._claimed_job()
        with patch.object(services, "handler_for", side_effect=UnknownJobKind("nope")):
            self.assertEqual(services.execute(job.pk), ScheduledJob.Status.FAILED)

    def test_a_vanished_job_is_not_an_error(self):
        self.assertEqual(services.execute(999999), ScheduledJob.Status.CANCELLED)

    def test_the_campaign_handler_is_registered(self):
        self.assertTrue(callable(handler_for(ScheduledJob.Kind.CAMPAIGN_DISPATCH)))


class DurableCampaignTests(TestCase):
    """
    The Phase 3 HIGH risk, closed.

    Every test here runs the campaign **from the database row alone** -- no
    Celery ETA, no broker state, nothing that a Redis flush could have taken.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Durable", slug="durable-campaign", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="durable-owner", password="x", tenant=self.tenant, credits=100,
        )
        self.account = _account(self.tenant, self.user)
        Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Priya", phone="+15559990001", subscribed=True,
        )

    def _campaign(self, when=None):
        return Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="Friday blast",
            whatsapp_account=self.account, message_body="Hello",
            audience_type=Campaign.AudienceType.ALL,
            scheduled_at=when or timezone.now() + timedelta(days=2),
        )

    def test_launching_a_scheduled_campaign_writes_a_durable_row(self):
        campaign = self._campaign()
        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            launch(campaign)

        job = services.live_job(ScheduledJob.Kind.CAMPAIGN_DISPATCH, campaign.pk)
        self.assertIsNotNone(job)
        self.assertEqual(job.status, ScheduledJob.Status.PENDING)
        self.assertEqual(job.tenant_id, self.tenant.pk)

    def test_launching_a_scheduled_campaign_publishes_no_broker_eta(self):
        """
        The regression guard on the whole design. If this ever fails, the
        schedule is back in Redis and the Phase 3 risk is back with it.
        """
        campaign = self._campaign()
        with patch("apps.campaigns.tasks.send_message_task.apply_async"), \
             patch("apps.campaigns.tasks.dispatch_campaign.apply_async") as eta_dispatch:
            launch(campaign)
        eta_dispatch.assert_not_called()

    def test_the_campaign_sends_from_the_database_alone(self):
        """
        Redis lost everything. The row is all that is left, and it is enough.
        """
        campaign = self._campaign()
        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            launch(campaign)

        job = services.live_job(ScheduledJob.Kind.CAMPAIGN_DISPATCH, campaign.pk)
        job.scheduled_for = timezone.now() - timedelta(minutes=1)
        job.save(update_fields=["scheduled_for"])

        with patch("apps.campaigns.tasks.send_message_task.apply_async") as send:
            claimed = services.claim_due()
            self.assertEqual(len(claimed), 1)
            services.execute(claimed[0])

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.RUNNING)
        self.assertTrue(send.called)

    def test_two_workers_produce_one_execution(self):
        """
        The duplicate-send test. Two pollers, one claim, one dispatch.
        """
        campaign = self._campaign()
        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            launch(campaign)
        job = services.live_job(ScheduledJob.Kind.CAMPAIGN_DISPATCH, campaign.pk)
        job.scheduled_for = timezone.now() - timedelta(minutes=1)
        job.save(update_fields=["scheduled_for"])

        first = services.claim_due(worker="worker-a")
        second = services.claim_due(worker="worker-b")
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [], "A second worker must not claim a job another worker holds.")

    def test_re_running_a_dispatch_does_not_send_twice(self):
        """
        **The duplicate-send test that matters** (phase brief section 103).

        At-least-once delivery means the dispatch genuinely can run again --
        the worker is killed after handing messages to the broker and before
        the job row says so, and the reclaimed job runs the whole dispatch
        over. So the second pass really does re-enqueue every message still
        marked QUEUED, and that is *fine*, because the protection is one layer
        down: `messaging.services.attempt_send` claims a message with a
        conditional `UPDATE ... WHERE status = 'QUEUED'`, so only one execution
        can ever win it and the loser sends nothing.

        This runs the whole path twice with a real (mocked-at-the-provider)
        send, and asserts the recipient hears from us exactly once.
        """
        from apps.whatsapp.providers.base import SendResult

        campaign = self._campaign()
        with patch("apps.messaging.services.ConnectionManager.send_text_message") as provider:
            provider.return_value = SendResult(success=True, provider_message_id="wamid.dup")
            launch(campaign)

            job = services.live_job(ScheduledJob.Kind.CAMPAIGN_DISPATCH, campaign.pk)
            job.scheduled_for = timezone.now() - timedelta(minutes=1)
            job.save(update_fields=["scheduled_for"])

            services.execute(services.claim_due()[0])
            self.assertEqual(provider.call_count, 1)

            # The worker died and the job was reclaimed and re-run.
            from apps.campaigns.tasks import dispatch_campaign

            dispatch_campaign(campaign.pk)

        self.assertEqual(
            provider.call_count, 1,
            "A re-run must not deliver the same message to the recipient twice.",
        )

    def test_a_cancelled_campaign_does_not_send(self):
        campaign = self._campaign()
        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            launch(campaign)

        from apps.campaigns.services import cancel

        cancel(campaign)

        job = ScheduledJob.objects.filter(object_id=campaign.pk).first()
        self.assertEqual(job.status, ScheduledJob.Status.CANCELLED)

    def test_a_worker_holding_a_stale_job_for_a_cancelled_campaign_sends_nothing(self):
        """
        Cancellation cannot race the worker: the handler re-reads the
        campaign's status at the moment of use, which is after any cancel.
        """
        campaign = self._campaign()
        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            launch(campaign)
        job = services.live_job(ScheduledJob.Kind.CAMPAIGN_DISPATCH, campaign.pk)
        job.scheduled_for = timezone.now() - timedelta(minutes=1)
        job.save(update_fields=["scheduled_for"])
        claimed = services.claim_due()[0]

        # Cancelled *after* the worker took the job.
        campaign.status = Campaign.Status.CANCELLED
        campaign.save(update_fields=["status"])

        with patch("apps.campaigns.tasks.send_message_task.apply_async") as send:
            services.execute(claimed)
        send.assert_not_called()

    def test_a_suspended_workspace_does_not_send(self):
        campaign = self._campaign()
        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            launch(campaign)
        job = services.live_job(ScheduledJob.Kind.CAMPAIGN_DISPATCH, campaign.pk)
        job.scheduled_for = timezone.now() - timedelta(minutes=1)
        job.save(update_fields=["scheduled_for"])
        claimed = services.claim_due()[0]

        self.tenant.is_active = False
        self.tenant.save(update_fields=["is_active"])

        with patch("apps.campaigns.tasks.send_message_task.apply_async") as send:
            services.execute(claimed)
        send.assert_not_called()
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.PAUSED)

    def test_an_expired_subscription_does_not_send(self):
        """
        The authorization made on Monday is not the authorization that
        applies on Friday.
        """
        plan = Plan.objects.create(name="Durable Plan", max_messages_per_month=1000)
        Subscription.objects.create(
            tenant=self.tenant, plan=plan, status=Subscription.Status.TRIALING,
            trial_started_at=timezone.now() - timedelta(days=20),
            trial_ends_at=timezone.now() - timedelta(days=6),
            current_period_start=timezone.now() - timedelta(days=20),
            current_period_end=timezone.now() - timedelta(days=6),
        )

        campaign = self._campaign()
        campaign.status = Campaign.Status.QUEUED
        campaign.save(update_fields=["status"])
        job = services.schedule(
            kind=ScheduledJob.Kind.CAMPAIGN_DISPATCH, object_id=campaign.pk,
            tenant_id=self.tenant.pk, run_at=timezone.now() - timedelta(minutes=1),
        )

        with patch("apps.campaigns.tasks.send_message_task.apply_async") as send:
            services.execute(services.claim_due()[0])
        send.assert_not_called()
        job.refresh_from_db()
        self.assertIn("subscription", job.last_error.lower())


class SchedulerTaskTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Tick", slug="sched-tick", kind=Tenant.Kind.CUSTOMER)

    def test_the_tick_reclaims_then_claims(self):
        from .tasks import run_due_scheduled_jobs

        services.schedule(
            kind=ScheduledJob.Kind.CAMPAIGN_DISPATCH, object_id=1,
            tenant_id=self.tenant.pk, run_at=timezone.now() - timedelta(minutes=1),
        )
        with patch("apps.scheduling.tasks.execute_scheduled_job.delay") as dispatch:
            result = run_due_scheduled_jobs()

        self.assertEqual(result["claimed"], 1)
        dispatch.assert_called_once()

    def test_the_tick_is_quiet_when_there_is_nothing_due(self):
        from .tasks import run_due_scheduled_jobs

        self.assertEqual(
            run_due_scheduled_jobs(),
            {"claimed": 0, "recovered": 0, "requeued": 0, "failed": 0},
        )


class StalledCampaignRecoveryTests(TestCase):
    """
    The *other* Redis failure, and the one nothing used to notice.

    A scheduled campaign that never started is the risk Phase 3 named. A
    campaign that started, handed its messages to the broker with per-message
    countdowns, and then lost the broker is a different failure: the messages
    are safe -- QUEUED rows in PostgreSQL -- but nothing was ever going to pick
    them up again, and the customer had to notice and press Resume.

    The line these tests defend is the threshold. Re-dispatching a campaign
    that is merely *slow* would compress the pacing its owner chose, so the
    detector looks for silence, not slowness.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Stalled", slug="stalled-co", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="stalled-owner", password="x", tenant=self.tenant, credits=100,
        )
        self.account = _account(self.tenant, self.user)
        self.campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="In flight",
            whatsapp_account=self.account, message_body="Hi", status=Campaign.Status.RUNNING,
        )

    def _message(self, status, updated_ago_seconds, phone="+15551110001"):
        from apps.messaging.models import Message

        message = Message.objects.create(
            tenant=self.tenant, campaign=self.campaign, whatsapp_account=self.account,
            to_phone=phone, body="Hi", status=status,
        )
        # `updated_at` is auto_now, so it has to be forced past the model.
        Message.objects.filter(pk=message.pk).update(
            updated_at=timezone.now() - timedelta(seconds=updated_ago_seconds),
        )
        return message

    def test_a_silent_campaign_with_queued_messages_is_found(self):
        from apps.messaging.models import Message

        self._message(Message.Status.QUEUED, services.stall_threshold_seconds() + 600)
        self.assertIn(self.campaign.pk, services.find_stalled_campaigns())

    def test_a_campaign_that_moved_recently_is_left_alone(self):
        """
        The important negative. A long campaign pacing itself over hours has
        *some* message changing state; rescuing it would compress the delays
        its owner deliberately set.
        """
        from apps.messaging.models import Message

        self._message(Message.Status.QUEUED, services.stall_threshold_seconds() + 600, "+15551110001")
        self._message(Message.Status.SENT, 30, "+15551110002")
        self.assertNotIn(self.campaign.pk, services.find_stalled_campaigns())

    def test_a_campaign_with_nothing_queued_is_left_alone(self):
        from apps.messaging.models import Message

        self._message(Message.Status.SENT, services.stall_threshold_seconds() + 600)
        self.assertEqual(services.find_stalled_campaigns(), [])

    def test_a_finished_campaign_is_never_touched(self):
        from apps.messaging.models import Message

        self._message(Message.Status.QUEUED, services.stall_threshold_seconds() + 600)
        self.campaign.status = Campaign.Status.COMPLETED
        self.campaign.save(update_fields=["status"])
        self.assertEqual(services.find_stalled_campaigns(), [])

    def test_a_paused_campaign_is_never_resumed_behind_the_customers_back(self):
        from apps.messaging.models import Message

        self._message(Message.Status.QUEUED, services.stall_threshold_seconds() + 600)
        self.campaign.status = Campaign.Status.PAUSED
        self.campaign.save(update_fields=["status"])
        self.assertEqual(services.find_stalled_campaigns(), [])

    def test_recovery_is_queued_as_a_durable_job(self):
        from apps.messaging.models import Message

        self._message(Message.Status.QUEUED, services.stall_threshold_seconds() + 600)
        self.assertEqual(services.schedule_stalled_campaign_recovery(), 1)

        job = services.live_job(ScheduledJob.Kind.CAMPAIGN_RESUME, self.campaign.pk)
        self.assertIsNotNone(job)
        self.assertEqual(job.tenant_id, self.tenant.pk)

    def test_a_second_sweep_does_not_queue_a_second_recovery(self):
        from apps.messaging.models import Message

        self._message(Message.Status.QUEUED, services.stall_threshold_seconds() + 600)
        services.schedule_stalled_campaign_recovery()
        self.assertEqual(services.schedule_stalled_campaign_recovery(), 0)
        self.assertEqual(
            ScheduledJob.objects.filter(kind=ScheduledJob.Kind.CAMPAIGN_RESUME).count(), 1,
        )

    def test_running_the_recovery_re_dispatches_what_is_still_queued(self):
        from apps.messaging.models import Message

        self._message(Message.Status.QUEUED, services.stall_threshold_seconds() + 600)
        services.schedule_stalled_campaign_recovery()

        with patch("apps.campaigns.tasks.send_message_task.apply_async") as send:
            services.execute(services.claim_due()[0])
        self.assertEqual(send.call_count, 1)

    def test_recovery_does_not_deliver_a_message_twice(self):
        """
        The same protection as everywhere else: `attempt_send` claims a message
        with a conditional UPDATE, so only one execution can win it.
        """
        from apps.messaging.models import Message
        from apps.whatsapp.providers.base import SendResult

        self._message(Message.Status.QUEUED, services.stall_threshold_seconds() + 600)
        services.schedule_stalled_campaign_recovery()

        with patch("apps.messaging.services.ConnectionManager.send_text_message") as provider:
            provider.return_value = SendResult(success=True, provider_message_id="wamid.recover")
            services.execute(services.claim_due()[0])
            first = provider.call_count

            from apps.campaigns.tasks import dispatch_campaign

            dispatch_campaign(self.campaign.pk)

        self.assertEqual(provider.call_count, first)
        self.assertEqual(first, 1)

    def test_the_tick_reports_what_it_recovered(self):
        from apps.messaging.models import Message

        from .tasks import run_due_scheduled_jobs

        self._message(Message.Status.QUEUED, services.stall_threshold_seconds() + 600)
        with patch("apps.scheduling.tasks.execute_scheduled_job.delay"):
            result = run_due_scheduled_jobs()
        self.assertEqual(result["recovered"], 1)
