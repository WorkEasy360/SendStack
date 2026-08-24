from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp import health, limits
from apps.whatsapp.models import AccountHealthEvent, AccountSendQuota, SendingPolicy, WhatsAppAccount


def _tenant(slug):
    return Tenant.objects.create(name=slug, slug=slug, kind=Tenant.Kind.CUSTOMER)


def _account(tenant, user, *, qr=True, **overrides):
    defaults = {
        "tenant": tenant, "user": user, "name": "Sales Line",
        "status": WhatsAppAccount.Status.CONNECTED,
    }
    if qr:
        defaults.update({
            "account_type": WhatsAppAccount.AccountType.WHATSAPP,
            "connection_method": WhatsAppAccount.ConnectionMethod.QR,
        })
    else:
        defaults.update({
            "account_type": WhatsAppAccount.AccountType.API,
            "connection_method": WhatsAppAccount.ConnectionMethod.CLOUD_API,
            "phone_number_id": "1234567890",
        })
    defaults.update(overrides)
    account = WhatsAppAccount(**defaults)
    account.save()
    return account


class WarmupTests(TestCase):
    """
    The ramp only protects numbers that need protecting. Applying it to a
    number with months of history would throttle a healthy account to 50
    messages the day the feature shipped, which is why the clock starts from
    the first send rather than from when the policy row appeared.
    """

    def setUp(self):
        self.tenant = _tenant("acme-warmup")
        self.user = get_user_model().objects.create_user(
            username="acme-warmup-owner", password="x", tenant=self.tenant,
        )
        self.account = _account(self.tenant, self.user)

    @override_settings(WHATSAPP_WARMUP_LADDER=[50, 100, 250])
    def test_a_new_number_starts_on_the_first_rung(self):
        policy = limits.policy_for(self.account)
        day, allowance = limits.warmup_state(self.account, policy)

        self.assertEqual((day, allowance), (1, 50))

    @override_settings(WHATSAPP_WARMUP_LADDER=[50, 100, 250])
    def test_it_climbs_a_rung_a_day(self):
        policy = limits.policy_for(self.account)
        policy.warmup_started_on = timezone.localdate() - timedelta(days=2)

        self.assertEqual(limits.warmup_state(self.account, policy), (3, 250))

    @override_settings(WHATSAPP_WARMUP_LADDER=[50, 100, 250])
    def test_a_number_past_the_ladder_is_warm(self):
        policy = limits.policy_for(self.account)
        policy.warmup_started_on = timezone.localdate() - timedelta(days=30)

        self.assertEqual(limits.warmup_state(self.account, policy), (None, None))

    @override_settings(WHATSAPP_WARMUP_LADDER=[50, 100, 250])
    def test_an_established_number_is_not_put_back_on_day_one(self):
        Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, to_phone="+15551110000",
            direction=Message.Direction.OUTBOUND, status=Message.Status.SENT,
            sent_at=timezone.now() - timedelta(days=90),
        )

        policy = limits.policy_for(self.account)

        self.assertEqual(policy.warmup_started_on, timezone.localdate() - timedelta(days=90))
        self.assertEqual(limits.warmup_state(self.account, policy), (None, None))

    @override_settings(WHATSAPP_WARMUP_LADDER=[50, 100, 250])
    def test_a_cloud_api_number_never_ramps(self):
        # Meta meters these itself, in tiers tied to the business's quality
        # rating. A second invented ramp on top would only throttle a number
        # Meta has already cleared to send more.
        cloud = _account(self.tenant, self.user, qr=False, name="API Line")
        policy = limits.policy_for(cloud)

        self.assertEqual(limits.warmup_state(cloud, policy), (None, None))

    @override_settings(WHATSAPP_WARMUP_LADDER=[50, 100, 250], WHATSAPP_DAILY_SEND_LIMIT=1000)
    def test_the_lower_of_the_ramp_and_a_configured_limit_wins(self):
        policy = limits.policy_for(self.account)
        policy.daily_limit = 800
        policy.save(update_fields=["daily_limit"])

        self.assertEqual(limits.effective_limits(self.account, policy)["daily_limit"], 50)

        policy.daily_limit = 20
        self.assertEqual(limits.effective_limits(self.account, policy)["daily_limit"], 20)

    @override_settings(WHATSAPP_WARMUP_ENABLED=False, WHATSAPP_WARMUP_LADDER=[50])
    def test_the_ramp_can_be_switched_off_deployment_wide(self):
        policy = limits.policy_for(self.account)

        self.assertEqual(limits.warmup_state(self.account, policy), (None, None))


@override_settings(WHATSAPP_WARMUP_ENABLED=False)
class SendSlotTests(TestCase):
    def setUp(self):
        self.tenant = _tenant("acme-slots")
        self.user = get_user_model().objects.create_user(
            username="acme-slots-owner", password="x", tenant=self.tenant,
        )
        self.account = _account(self.tenant, self.user)

    @override_settings(WHATSAPP_DAILY_SEND_LIMIT=3, WHATSAPP_HOURLY_SEND_LIMIT=0)
    def test_slots_run_out_at_the_daily_limit(self):
        for _ in range(3):
            self.assertTrue(limits.reserve_send_slot(self.account).allowed)

        decision = limits.reserve_send_slot(self.account)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.scope, AccountSendQuota.Scope.DAY)
        self.assertGreater(decision.retry_after, 0)
        self.assertIn("daily limit", decision.reason)

    @override_settings(WHATSAPP_DAILY_SEND_LIMIT=0, WHATSAPP_HOURLY_SEND_LIMIT=2)
    def test_the_hourly_limit_is_enforced_separately(self):
        limits.reserve_send_slot(self.account)
        limits.reserve_send_slot(self.account)
        decision = limits.reserve_send_slot(self.account)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.scope, AccountSendQuota.Scope.HOUR)
        self.assertLessEqual(decision.retry_after, 3600)

    @override_settings(WHATSAPP_DAILY_SEND_LIMIT=1, WHATSAPP_HOURLY_SEND_LIMIT=5)
    def test_a_refused_message_does_not_spend_the_other_window(self):
        limits.reserve_send_slot(self.account)
        self.assertFalse(limits.reserve_send_slot(self.account).allowed)

        # The hour window must still show exactly the one message that really
        # went out -- a refusal that half-charged its counters would slowly
        # starve the number of an allowance it never used.
        self.assertEqual(limits.usage(self.account)["hourly_sent"], 1)

    @override_settings(WHATSAPP_DAILY_SEND_LIMIT=0, WHATSAPP_HOURLY_SEND_LIMIT=0)
    def test_zero_means_no_limit(self):
        for _ in range(20):
            self.assertTrue(limits.reserve_send_slot(self.account).allowed)
        self.assertEqual(AccountSendQuota.objects.count(), 0)

    @override_settings(WHATSAPP_DAILY_SEND_LIMIT=5, WHATSAPP_HOURLY_SEND_LIMIT=5)
    def test_yesterdays_counter_does_not_limit_today(self):
        AccountSendQuota.objects.create(
            account=self.account, scope=AccountSendQuota.Scope.DAY,
            window_start=timezone.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1),
            sent_count=5,
        )

        self.assertTrue(limits.reserve_send_slot(self.account).allowed)


class AutoPauseTests(TestCase):
    def setUp(self):
        self.tenant = _tenant("acme-pause")
        self.user = get_user_model().objects.create_user(
            username="acme-pause-owner", password="x", tenant=self.tenant,
        )
        self.account = _account(self.tenant, self.user)

    def _messages(self, sent=0, failed=0, minutes_ago=1):
        moment = timezone.now() - timedelta(minutes=minutes_ago)
        for _ in range(sent):
            Message.objects.create(
                tenant=self.tenant, whatsapp_account=self.account, to_phone="+15551110001",
                direction=Message.Direction.OUTBOUND, status=Message.Status.SENT, sent_at=moment,
            )
        for _ in range(failed):
            Message.objects.create(
                tenant=self.tenant, whatsapp_account=self.account, to_phone="+15551110002",
                direction=Message.Direction.OUTBOUND, status=Message.Status.FAILED, failed_at=moment,
            )

    @override_settings(WHATSAPP_FAILURE_SAMPLE_MIN=10, WHATSAPP_FAILURE_RATE_THRESHOLD=0.5)
    def test_a_run_of_failures_pauses_the_number(self):
        self._messages(sent=2, failed=10)

        health.note_send_failure(self.account, "connection reset")

        policy = limits.policy_for(self.account)
        self.assertTrue(policy.is_paused)
        self.assertIsNotNone(policy.paused_until)
        self.assertTrue(
            AccountHealthEvent.objects.filter(
                account=self.account, event=AccountHealthEvent.Event.FAILURE_SPIKE,
            ).exists()
        )

    @override_settings(WHATSAPP_FAILURE_SAMPLE_MIN=10, WHATSAPP_FAILURE_RATE_THRESHOLD=0.5)
    def test_a_couple_of_failures_on_a_quiet_number_do_not(self):
        # At low volume a 100% failure rate is one message, which is a
        # coincidence rather than a pattern.
        self._messages(failed=2)

        health.note_send_failure(self.account, "timeout")

        self.assertFalse(limits.policy_for(self.account).is_paused)

    @override_settings(WHATSAPP_FAILURE_SAMPLE_MIN=10, WHATSAPP_FAILURE_RATE_THRESHOLD=0.5)
    def test_healthy_traffic_with_some_failures_does_not_pause(self):
        self._messages(sent=90, failed=10)

        health.note_send_failure(self.account, "timeout")

        self.assertFalse(limits.policy_for(self.account).is_paused)

    @override_settings(WHATSAPP_AUTO_PAUSE_ENABLED=False, WHATSAPP_FAILURE_SAMPLE_MIN=2)
    def test_automatic_pausing_can_be_switched_off(self):
        self._messages(failed=10)

        health.note_send_failure(self.account, "connection reset")

        self.assertFalse(limits.policy_for(self.account).is_paused)

    def test_a_logged_out_number_is_paused_indefinitely(self):
        # This one needs a human: nothing about a logged-out or blocked number
        # improves by waiting, and resuming into a ban is how a temporary
        # restriction becomes a permanent one.
        health.note_send_failure(self.account, "Connection closed: logged out")

        policy = limits.policy_for(self.account)
        self.assertTrue(policy.is_paused)
        self.assertIsNone(policy.paused_until)
        self.assertTrue(
            AccountHealthEvent.objects.filter(
                account=self.account, event=AccountHealthEvent.Event.BAN_SUSPECTED,
                severity=AccountHealthEvent.Severity.CRITICAL,
            ).exists()
        )

    def test_a_timed_pause_lifts_itself(self):
        health.pause_sending(self.account, "Too many failures", minutes=30)
        policy = limits.policy_for(self.account)
        policy.paused_until = timezone.now() - timedelta(minutes=1)
        policy.save(update_fields=["paused_until"])

        paused, remaining = health.pause_state(self.account)

        self.assertFalse(paused)
        self.assertEqual(remaining, 0.0)
        self.assertFalse(SendingPolicy.objects.get(account=self.account).is_paused)

    def test_an_indefinite_pause_does_not(self):
        health.pause_sending(self.account, "Suspected ban", minutes=None)

        paused, remaining = health.pause_state(self.account)

        self.assertTrue(paused)
        self.assertEqual(remaining, 0.0)

    def test_resuming_clears_the_reason_and_records_it(self):
        health.pause_sending(self.account, "Suspected ban", minutes=None)
        health.resume_sending(self.account, detail="Checked by hand.")

        policy = limits.policy_for(self.account)
        self.assertFalse(policy.is_paused)
        self.assertEqual(policy.pause_reason, "")
        self.assertTrue(
            AccountHealthEvent.objects.filter(
                account=self.account, event=AccountHealthEvent.Event.RESUMED,
            ).exists()
        )

    def test_hitting_a_limit_is_recorded_once_not_once_per_message(self):
        decision = limits.SlotDecision(allowed=False, reason="Daily limit reached.", retry_after=3600)

        for _ in range(5):
            health.note_limit_reached(self.account, decision)

        self.assertEqual(
            AccountHealthEvent.objects.filter(
                account=self.account, event=AccountHealthEvent.Event.LIMIT_REACHED,
            ).count(),
            1,
        )


class HealthScoreTests(TestCase):
    def setUp(self):
        self.tenant = _tenant("acme-score")
        self.user = get_user_model().objects.create_user(
            username="acme-score-owner", password="x", tenant=self.tenant,
        )
        self.account = _account(self.tenant, self.user)

    def test_a_connected_number_with_no_failures_scores_full_marks(self):
        snapshot = health.health_snapshot(self.account)

        self.assertEqual(snapshot["score"], 100)
        self.assertEqual(snapshot["grade"], "healthy")
        self.assertEqual(snapshot["reasons"], [])

    def test_a_disconnected_number_scores_lower_and_says_why(self):
        self.account.status = WhatsAppAccount.Status.DISCONNECTED
        self.account.save(update_fields=["status"])

        snapshot = health.health_snapshot(self.account)

        self.assertLess(snapshot["score"], 100)
        self.assertTrue(any("not connected" in reason for reason in snapshot["reasons"]))

    def test_failures_drag_the_score_down(self):
        for _ in range(10):
            Message.objects.create(
                tenant=self.tenant, whatsapp_account=self.account, to_phone="+15551110003",
                direction=Message.Direction.OUTBOUND, status=Message.Status.FAILED,
                failed_at=timezone.now(),
            )

        snapshot = health.health_snapshot(self.account)

        self.assertEqual(snapshot["failed_24h"], 10)
        self.assertEqual(snapshot["failure_rate_24h"], 1.0)
        self.assertLessEqual(snapshot["score"], 40)
        self.assertEqual(snapshot["grade"], "at_risk")

    @override_settings(WHATSAPP_DAILY_SEND_LIMIT=100, WHATSAPP_HOURLY_SEND_LIMIT=10, WHATSAPP_WARMUP_ENABLED=False)
    def test_the_snapshot_reports_what_is_left_in_each_window(self):
        limits.reserve_send_slot(self.account)

        snapshot = health.health_snapshot(self.account)

        self.assertEqual(snapshot["daily_sent"], 1)
        self.assertEqual(snapshot["daily_remaining"], 99)
        self.assertEqual(snapshot["hourly_remaining"], 9)

    @override_settings(WHATSAPP_DAILY_SEND_LIMIT=0, WHATSAPP_HOURLY_SEND_LIMIT=0, WHATSAPP_WARMUP_ENABLED=False)
    def test_no_limit_reads_as_no_limit_rather_than_zero_left(self):
        snapshot = health.health_snapshot(self.account)

        self.assertIsNone(snapshot["daily_limit"])
        self.assertIsNone(snapshot["daily_remaining"])

    def test_the_score_never_leaves_its_range(self):
        self.account.status = WhatsAppAccount.Status.ERROR
        self.account.save(update_fields=["status"])
        health.pause_sending(self.account, "Suspected ban", minutes=None)
        for _ in range(10):
            Message.objects.create(
                tenant=self.tenant, whatsapp_account=self.account, to_phone="+15551110004",
                direction=Message.Direction.OUTBOUND, status=Message.Status.FAILED,
                failed_at=timezone.now(),
            )
            health.record_event(self.account, AccountHealthEvent.Event.DISCONNECTED, "dropped")

        snapshot = health.health_snapshot(self.account)

        self.assertGreaterEqual(snapshot["score"], 0)
        self.assertEqual(snapshot["grade"], "at_risk")


class SenderHealthApiTests(TestCase):
    def setUp(self):
        self.tenant = _tenant("acme-health-api")
        self.user = get_user_model().objects.create_user(
            username="acme-health-api-owner", password="x", tenant=self.tenant,
        )
        self.account = _account(self.tenant, self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_the_overview_lists_every_device(self):
        response = self.client.get("/api/v1/whatsapp/accounts/health/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["account_id"], self.account.pk)

    def test_limits_can_be_set_per_number(self):
        response = self.client.patch(
            f"/api/v1/whatsapp/accounts/{self.account.pk}/sending-policy/",
            data={"daily_limit": 250, "hourly_limit": 40},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(limits.policy_for(self.account).daily_limit, 250)

    def test_an_hourly_limit_above_the_daily_one_is_rejected(self):
        response = self.client.patch(
            f"/api/v1/whatsapp/accounts/{self.account.pk}/sending-policy/",
            data={"daily_limit": 100, "hourly_limit": 500},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    def test_pausing_and_resuming_from_the_devices_page(self):
        paused = self.client.post(
            f"/api/v1/whatsapp/accounts/{self.account.pk}/pause-sending/",
            data={"reason": "Checking the number"}, content_type="application/json",
        )
        self.assertEqual(paused.status_code, 200)
        self.assertTrue(paused.json()["is_paused"])

        resumed = self.client.post(f"/api/v1/whatsapp/accounts/{self.account.pk}/resume-sending/")
        self.assertEqual(resumed.status_code, 200)
        self.assertFalse(resumed.json()["is_paused"])

    def test_health_events_are_listed_for_the_tenant(self):
        health.record_event(self.account, AccountHealthEvent.Event.DISCONNECTED, "dropped")

        response = self.client.get("/api/v1/whatsapp/accounts/health-events/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["event"], "DISCONNECTED")

    def test_another_tenants_device_is_not_reachable(self):
        other = _tenant("other-health-api")
        other_user = get_user_model().objects.create_user(
            username="other-health-api-owner", password="x", tenant=other,
        )
        other_account = _account(other, other_user, name="Theirs")

        response = self.client.get(f"/api/v1/whatsapp/accounts/{other_account.pk}/health/")

        self.assertEqual(response.status_code, 404)
