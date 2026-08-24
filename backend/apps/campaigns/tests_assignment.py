"""
Coverage for multi-account campaign assignment and per-message random delays.

Appended as a separate module so the pre-existing apps/campaigns/tests.py
keeps its own scope; both run under `manage.py test`.
"""

import itertools
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.contacts.models import Contact
from apps.messaging.models import Message
from apps.messaging.tasks import send_message_task
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount
from apps.whatsapp.providers.base import SendResult

from . import services
from .assignment import assign_accounts, eligible_accounts, next_delay, resolve_delay_bounds
from .models import Campaign
from .services import CampaignError
from .tasks import dispatch_campaign


def _sequential_send_results():
    """A send_text_message stub handing back a unique provider id each call."""
    counter = itertools.count()

    def _send(*args, **kwargs):
        return SendResult(success=True, provider_message_id=f"wamid.{next(counter)}")

    return _send


def _account(tenant, user, name, status=WhatsAppAccount.Status.CONNECTED):
    account = WhatsAppAccount(tenant=tenant, user=user, name=name, phone_number_id="1", status=status)
    account.set_access_token("token")
    account.set_app_secret("secret")
    account.save()
    return account


class _Base(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug=f"acme-{self._testMethodName}", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username=f"owner-{self._testMethodName}", password="x", tenant=self.tenant, credits=10000,
        )

    def _contacts(self, count):
        return [
            Contact.objects.create(
                tenant=self.tenant, created_by=self.user, name=f"C{index}", phone=f"+1555000{index:04d}",
            )
            for index in range(count)
        ]

    def _campaign(self, account, **overrides):
        defaults = dict(
            tenant=self.tenant, created_by=self.user, name="Blast", whatsapp_account=account,
            message_body="Hi", audience_type=Campaign.AudienceType.ALL,
        )
        defaults.update(overrides)
        return Campaign.objects.create(**defaults)


class DelayGenerationTests(_Base):
    @override_settings(CAMPAIGN_MIN_DELAY_SECONDS=1.0, CAMPAIGN_MAX_DELAY_SECONDS=1.0)
    def test_unset_campaign_falls_back_to_deployment_default(self):
        campaign = self._campaign(_account(self.tenant, self.user, "A"))
        self.assertEqual(resolve_delay_bounds(campaign), (1.0, 1.0))

    def test_campaign_overrides_win(self):
        campaign = self._campaign(
            _account(self.tenant, self.user, "A"), min_delay_seconds=2, max_delay_seconds=7,
        )
        self.assertEqual(resolve_delay_bounds(campaign), (2.0, 7.0))

    def test_every_delay_is_inside_the_window(self):
        campaign = self._campaign(
            _account(self.tenant, self.user, "A"), min_delay_seconds=0, max_delay_seconds=5,
        )
        delays = [next_delay(campaign) for _ in range(200)]

        for delay in delays:
            self.assertGreater(delay, 0, "delay must be strictly greater than zero")
            self.assertLessEqual(delay, 5)

    def test_delays_are_independently_generated(self):
        campaign = self._campaign(
            _account(self.tenant, self.user, "A"), min_delay_seconds=1, max_delay_seconds=5,
        )
        delays = [next_delay(campaign) for _ in range(20)]

        # 20 draws from a continuous range collapsing to one value would mean
        # a single delay was computed and reused -- the exact bug this feature
        # exists to prevent.
        self.assertGreater(len(set(delays)), 15, f"delays look reused: {delays}")

    def test_zero_minimum_still_produces_a_positive_delay(self):
        campaign = self._campaign(
            _account(self.tenant, self.user, "A"), min_delay_seconds=0, max_delay_seconds=0,
        )
        self.assertGreater(next_delay(campaign), 0)

    def test_inverted_stored_window_does_not_reverse_the_draw(self):
        campaign = self._campaign(
            _account(self.tenant, self.user, "A"), min_delay_seconds=5, max_delay_seconds=1,
        )
        self.assertEqual(resolve_delay_bounds(campaign), (5.0, 5.0))
        self.assertEqual(next_delay(campaign), 5.0)


class AccountEligibilityTests(_Base):
    def test_manual_mode_uses_only_the_selected_account(self):
        chosen = _account(self.tenant, self.user, "A")
        _account(self.tenant, self.user, "B")
        campaign = self._campaign(chosen)

        self.assertEqual(eligible_accounts(campaign), [chosen])

    def test_disconnected_accounts_are_never_eligible(self):
        connected = _account(self.tenant, self.user, "A")
        _account(self.tenant, self.user, "B", status=WhatsAppAccount.Status.DISCONNECTED)
        _account(self.tenant, self.user, "C", status=WhatsAppAccount.Status.ERROR)
        _account(self.tenant, self.user, "D", status=WhatsAppAccount.Status.EXPIRED)
        _account(self.tenant, self.user, "E", status=WhatsAppAccount.Status.QR_READY)
        campaign = self._campaign(connected, account_assignment_mode=Campaign.AccountAssignmentMode.AUTO)

        self.assertEqual(eligible_accounts(campaign), [connected])

    def test_other_tenants_accounts_are_never_eligible(self):
        mine = _account(self.tenant, self.user, "Mine")
        other_tenant = Tenant.objects.create(name="Other", slug="other-tenant-x", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="other-x", password="x", tenant=other_tenant)
        _account(other_tenant, other_user, "Theirs")
        campaign = self._campaign(mine, account_assignment_mode=Campaign.AccountAssignmentMode.AUTO)

        self.assertEqual(eligible_accounts(campaign), [mine])


class AutoAssignmentTests(_Base):
    def _auto_campaign(self, accounts):
        return self._campaign(accounts[0], account_assignment_mode=Campaign.AccountAssignmentMode.AUTO)

    def test_single_account_takes_everything(self):
        accounts = [_account(self.tenant, self.user, "A")]
        assigned = assign_accounts(self._auto_campaign(accounts), 10)

        self.assertEqual([a.pk for a in assigned], [accounts[0].pk] * 10)

    def test_two_accounts_split_evenly(self):
        accounts = [_account(self.tenant, self.user, name) for name in ("A", "B")]
        assigned = assign_accounts(self._auto_campaign(accounts), 20)

        tally = {}
        for account in assigned:
            tally[account.pk] = tally.get(account.pk, 0) + 1
        self.assertEqual(sorted(tally.values()), [10, 10])

    def test_five_accounts_split_within_one_message(self):
        accounts = [_account(self.tenant, self.user, name) for name in ("A", "B", "C", "D", "E")]
        assigned = assign_accounts(self._auto_campaign(accounts), 103)

        tally = {}
        for account in assigned:
            tally[account.pk] = tally.get(account.pk, 0) + 1
        self.assertEqual(len(tally), 5)
        self.assertLessEqual(max(tally.values()) - min(tally.values()), 1)

    def test_assignment_is_not_always_the_first_account(self):
        accounts = [_account(self.tenant, self.user, name) for name in ("A", "B", "C")]
        campaign = self._auto_campaign(accounts)

        first_picks = {assign_accounts(campaign, 3)[0].pk for _ in range(40)}
        self.assertGreater(len(first_picks), 1, "assignment always starts from the same account")

    def test_no_eligible_accounts_yields_nothing(self):
        account = _account(self.tenant, self.user, "A", status=WhatsAppAccount.Status.DISCONNECTED)
        campaign = self._campaign(account, account_assignment_mode=Campaign.AccountAssignmentMode.AUTO)

        self.assertEqual(assign_accounts(campaign, 5), [])


class CampaignLaunchAssignmentTests(_Base):
    def test_manual_launch_puts_every_message_on_the_chosen_account(self):
        chosen = _account(self.tenant, self.user, "A")
        _account(self.tenant, self.user, "B")
        self._contacts(6)
        campaign = self._campaign(chosen)

        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            services.launch(campaign)

        accounts_used = set(campaign.messages.values_list("whatsapp_account_id", flat=True))
        self.assertEqual(accounts_used, {chosen.pk})

    def test_auto_launch_spreads_messages_across_accounts(self):
        accounts = [_account(self.tenant, self.user, name) for name in ("A", "B", "C")]
        self._contacts(30)
        campaign = self._campaign(accounts[0], account_assignment_mode=Campaign.AccountAssignmentMode.AUTO)

        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            services.launch(campaign)

        used = set(campaign.messages.values_list("whatsapp_account_id", flat=True))
        self.assertEqual(used, {account.pk for account in accounts})

    def test_auto_launch_requires_at_least_one_connected_account(self):
        account = _account(self.tenant, self.user, "A", status=WhatsAppAccount.Status.DISCONNECTED)
        self._contacts(3)
        campaign = self._campaign(account, account_assignment_mode=Campaign.AccountAssignmentMode.AUTO)

        with self.assertRaises(CampaignError):
            services.launch(campaign)

    def test_manual_launch_still_rejects_a_disconnected_account(self):
        account = _account(self.tenant, self.user, "A", status=WhatsAppAccount.Status.DISCONNECTED)
        self._contacts(3)
        campaign = self._campaign(account)

        with self.assertRaises(CampaignError):
            services.launch(campaign)


class DispatchDelayTests(_Base):
    def test_dispatch_records_a_distinct_delay_per_message(self):
        account = _account(self.tenant, self.user, "A")
        self._contacts(20)
        campaign = self._campaign(account, min_delay_seconds=1, max_delay_seconds=5)

        with patch("apps.campaigns.tasks.send_message_task.apply_async") as send:
            services.launch(campaign)

        delays = list(campaign.messages.values_list("assigned_delay_seconds", flat=True))
        self.assertEqual(len(delays), 20)
        for delay in delays:
            self.assertIsNotNone(delay)
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, 5)
            self.assertGreaterEqual(delay, 1)
        self.assertGreater(len(set(delays)), 15, f"delays look reused: {delays}")

        # Countdowns must be strictly increasing: each message waits its own
        # delay *after* the previous one, rather than all firing at once.
        countdowns = [call.kwargs["countdown"] for call in send.call_args_list]
        self.assertEqual(len(countdowns), 20)
        self.assertEqual(countdowns, sorted(countdowns))
        self.assertTrue(all(b > a for a, b in zip(countdowns, countdowns[1:])))

    def test_countdown_gaps_match_the_generated_delays(self):
        account = _account(self.tenant, self.user, "A")
        self._contacts(10)
        campaign = self._campaign(account, min_delay_seconds=1, max_delay_seconds=5)

        with patch("apps.campaigns.tasks.send_message_task.apply_async") as send:
            services.launch(campaign)

        ordered = list(campaign.messages.order_by("id").values_list("assigned_delay_seconds", flat=True))
        countdowns = [call.kwargs["countdown"] for call in send.call_args_list]
        gaps = [countdowns[0]] + [b - a for a, b in zip(countdowns, countdowns[1:])]

        for expected, actual in zip(ordered, gaps):
            self.assertAlmostEqual(expected, actual, places=6)

    @override_settings(CAMPAIGN_MIN_DELAY_SECONDS=1.0, CAMPAIGN_MAX_DELAY_SECONDS=1.0)
    def test_legacy_campaign_keeps_its_fixed_interval(self):
        account = _account(self.tenant, self.user, "A")
        self._contacts(5)
        campaign = self._campaign(account)  # no per-campaign delays, as before this feature

        with patch("apps.campaigns.tasks.send_message_task.apply_async") as send:
            services.launch(campaign)

        countdowns = [call.kwargs["countdown"] for call in send.call_args_list]
        self.assertEqual([round(value, 6) for value in countdowns], [1.0, 2.0, 3.0, 4.0, 5.0])


class AccountHealthTests(_Base):
    def _launched(self, mode=Campaign.AccountAssignmentMode.AUTO, contacts=6):
        self.accounts = [_account(self.tenant, self.user, name) for name in ("A", "B")]
        self._contacts(contacts)
        campaign = self._campaign(self.accounts[0], account_assignment_mode=mode)
        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            services.launch(campaign)
        return campaign

    def test_auto_campaign_moves_work_off_a_disconnected_account(self):
        campaign = self._launched()
        dead, alive = self.accounts
        message = campaign.messages.filter(whatsapp_account=dead).first()
        WhatsAppAccount.objects.filter(pk=dead.pk).update(status=WhatsAppAccount.Status.DISCONNECTED)

        with patch(
            "apps.messaging.services.ConnectionManager.send_text_message",
            return_value=SendResult(success=True, provider_message_id="wamid.1"),
        ):
            send_message_task(message.pk)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.SENT)
        self.assertEqual(message.whatsapp_account_id, alive.pk)

    def test_manual_campaign_never_silently_switches_account(self):
        campaign = self._launched(mode=Campaign.AccountAssignmentMode.MANUAL)
        chosen = self.accounts[0]
        message = campaign.messages.first()
        WhatsAppAccount.objects.filter(pk=chosen.pk).update(status=WhatsAppAccount.Status.DISCONNECTED)

        with patch("apps.messaging.tasks.send_message_task.retry", side_effect=RuntimeError("retried")):
            with self.assertRaises(RuntimeError):
                send_message_task(message.pk)

        message.refresh_from_db()
        self.assertEqual(message.whatsapp_account_id, chosen.pk)
        self.assertEqual(message.status, Message.Status.QUEUED)

    def test_recipient_is_skipped_not_lost_when_nothing_is_connected(self):
        campaign = self._launched()
        WhatsAppAccount.objects.filter(tenant=self.tenant).update(status=WhatsAppAccount.Status.DISCONNECTED)
        message = campaign.messages.first()

        # Exhaust the retry budget first -- the task only gives up on the
        # recipient once a reconnect has had its chances.
        send_message_task.push_request(retries=send_message_task.max_retries)
        try:
            send_message_task(message.pk)
        finally:
            send_message_task.pop_request()

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.SKIPPED)
        self.assertIn("No connected WhatsApp account", message.error_message)
        campaign.refresh_from_db()
        self.assertEqual(campaign.skipped_count, 1)

    def test_skipped_messages_do_not_block_campaign_completion(self):
        campaign = self._launched(contacts=1)
        WhatsAppAccount.objects.filter(tenant=self.tenant).update(status=WhatsAppAccount.Status.DISCONNECTED)
        campaign.status = Campaign.Status.RUNNING
        campaign.save(update_fields=["status"])
        message = campaign.messages.first()

        send_message_task.push_request(retries=send_message_task.max_retries)
        try:
            send_message_task(message.pk)
        finally:
            send_message_task.pop_request()

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.COMPLETED)

    def test_a_claimed_message_is_never_skipped_underneath_its_worker(self):
        campaign = self._launched()
        message = campaign.messages.first()
        Message.objects.filter(pk=message.pk).update(status=Message.Status.SENDING)

        from apps.messaging.services import mark_skipped

        self.assertFalse(mark_skipped(message, "nope"))
        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.SENDING)


class CampaignApiTests(_Base):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_auto_campaign_can_be_created_without_naming_an_account(self):
        account = _account(self.tenant, self.user, "A")

        response = self.client.post(
            "/api/v1/campaigns/campaigns/",
            {"name": "Auto", "account_assignment_mode": "AUTO", "message_body": "Hi", "audience_type": "ALL"},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["account_assignment_mode"], "AUTO")
        self.assertEqual(response.data["whatsapp_account"], account.pk)

    def test_manual_campaign_still_requires_an_account(self):
        _account(self.tenant, self.user, "A")

        response = self.client.post(
            "/api/v1/campaigns/campaigns/",
            {"name": "Manual", "account_assignment_mode": "MANUAL", "message_body": "Hi", "audience_type": "ALL"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("whatsapp_account", response.data)

    def test_delay_window_is_validated(self):
        account = _account(self.tenant, self.user, "A")

        response = self.client.post(
            "/api/v1/campaigns/campaigns/",
            {
                "name": "Bad", "whatsapp_account": account.pk, "message_body": "Hi", "audience_type": "ALL",
                "min_delay_seconds": 5, "max_delay_seconds": 2,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("max_delay_seconds", response.data)

    def test_negative_delay_is_rejected(self):
        account = _account(self.tenant, self.user, "A")

        response = self.client.post(
            "/api/v1/campaigns/campaigns/",
            {
                "name": "Bad", "whatsapp_account": account.pk, "message_body": "Hi", "audience_type": "ALL",
                "min_delay_seconds": -1,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_legacy_payload_without_the_new_fields_still_works(self):
        account = _account(self.tenant, self.user, "A")

        response = self.client.post(
            "/api/v1/campaigns/campaigns/",
            {"name": "Legacy", "whatsapp_account": account.pk, "message_body": "Hi", "audience_type": "ALL"},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["account_assignment_mode"], "MANUAL")
        self.assertIsNone(response.data["min_delay_seconds"])

    @override_settings(CAMPAIGN_ACCOUNT_ASSIGNMENT_MODE="AUTO")
    def test_deployment_default_mode_applies_when_unstated(self):
        _account(self.tenant, self.user, "A")

        response = self.client.post(
            "/api/v1/campaigns/campaigns/",
            {"name": "Defaulted", "message_body": "Hi", "audience_type": "ALL"},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["account_assignment_mode"], "AUTO")


class CampaignReportTests(_Base):
    def test_report_breaks_results_down_per_account(self):
        accounts = [_account(self.tenant, self.user, name) for name in ("A", "B")]
        self._contacts(10)
        campaign = self._campaign(accounts[0], account_assignment_mode=Campaign.AccountAssignmentMode.AUTO)
        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            services.launch(campaign)

        from apps.reports.services import campaign_report

        report = campaign_report(campaign)

        self.assertEqual(report["account_assignment_mode"], "AUTO")
        self.assertEqual({row["name"] for row in report["accounts"]}, {"A", "B"})
        self.assertEqual(sum(row["total"] for row in report["accounts"]), 10)
        for row in report["accounts"]:
            self.assertIsNotNone(row["avg_delay_seconds"])


class LifecycleTests(_Base):
    def test_resume_redispatches_only_what_is_left(self):
        account = _account(self.tenant, self.user, "A")
        self._contacts(5)
        campaign = self._campaign(account, min_delay_seconds=1, max_delay_seconds=3)
        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            services.launch(campaign)

        campaign.messages.filter(pk__in=campaign.messages.values_list("pk", flat=True)[:2]).update(
            status=Message.Status.SENT
        )
        services.pause(campaign)
        campaign.refresh_from_db()

        with patch("apps.campaigns.tasks.send_message_task.apply_async") as send:
            services.resume(campaign)

        self.assertEqual(send.call_count, 3)

    def test_worker_restart_redispatch_does_not_resend_completed_messages(self):
        account = _account(self.tenant, self.user, "A")
        self._contacts(4)
        campaign = self._campaign(account)
        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            services.launch(campaign)
        campaign.messages.update(status=Message.Status.SENT)
        campaign.status = Campaign.Status.RUNNING
        campaign.save(update_fields=["status"])

        with patch("apps.campaigns.tasks.send_message_task.apply_async") as send:
            dispatch_campaign(campaign.pk)

        self.assertEqual(send.call_count, 0)


class ScaleTests(_Base):
    """End-to-end runs at the sizes the feature is expected to handle."""

    def test_hundred_recipients_across_five_accounts(self):
        accounts = [_account(self.tenant, self.user, name) for name in ("A", "B", "C", "D", "E")]
        self._contacts(100)
        campaign = self._campaign(
            accounts[0],
            account_assignment_mode=Campaign.AccountAssignmentMode.AUTO,
            min_delay_seconds=1,
            max_delay_seconds=5,
        )

        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            services.launch(campaign)

        rows = list(campaign.messages.values_list("whatsapp_account_id", "assigned_delay_seconds"))
        self.assertEqual(len(rows), 100)

        tally = {}
        for account_id, _ in rows:
            tally[account_id] = tally.get(account_id, 0) + 1
        self.assertEqual(len(tally), 5, "every connected account should have been used")
        self.assertEqual(sorted(tally.values()), [20, 20, 20, 20, 20])

        delays = [delay for _, delay in rows]
        for delay in delays:
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, 5)
            self.assertGreaterEqual(delay, 1)
        self.assertGreater(len(set(delays)), 90, "delays are not independently generated")

    def test_ten_recipients_end_to_end_all_sent(self):
        accounts = [_account(self.tenant, self.user, name) for name in ("A", "B")]
        self._contacts(10)
        campaign = self._campaign(accounts[0], account_assignment_mode=Campaign.AccountAssignmentMode.AUTO)

        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            services.launch(campaign)
        with patch(
            "apps.messaging.services.ConnectionManager.send_text_message",
            side_effect=_sequential_send_results(),
        ):
            for message_id in campaign.messages.order_by("id").values_list("id", flat=True):
                send_message_task(message_id)

        campaign.refresh_from_db()
        self.assertEqual(campaign.sent_count, 10)
        self.assertEqual(campaign.failed_count, 0)
        self.assertEqual(campaign.skipped_count, 0)
        self.assertEqual(campaign.status, Campaign.Status.COMPLETED)

    def test_concurrent_campaigns_keep_their_own_assignment_and_delays(self):
        accounts = [_account(self.tenant, self.user, name) for name in ("A", "B", "C")]
        self._contacts(12)
        auto = self._campaign(
            accounts[0], name="Auto", account_assignment_mode=Campaign.AccountAssignmentMode.AUTO,
            min_delay_seconds=1, max_delay_seconds=2,
        )
        manual = self._campaign(
            accounts[2], name="Manual", account_assignment_mode=Campaign.AccountAssignmentMode.MANUAL,
            min_delay_seconds=4, max_delay_seconds=5,
        )

        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            services.launch(auto)
            services.launch(manual)

        self.assertEqual(len(set(auto.messages.values_list("whatsapp_account_id", flat=True))), 3)
        self.assertEqual(set(manual.messages.values_list("whatsapp_account_id", flat=True)), {accounts[2].pk})

        for delay in auto.messages.values_list("assigned_delay_seconds", flat=True):
            self.assertTrue(1 <= delay <= 2, delay)
        for delay in manual.messages.values_list("assigned_delay_seconds", flat=True):
            self.assertTrue(4 <= delay <= 5, delay)

    def test_account_disconnect_midway_moves_the_remainder_and_loses_nobody(self):
        accounts = [_account(self.tenant, self.user, name) for name in ("A", "B")]
        self._contacts(20)
        campaign = self._campaign(accounts[0], account_assignment_mode=Campaign.AccountAssignmentMode.AUTO)
        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            services.launch(campaign)

        message_ids = list(campaign.messages.order_by("id").values_list("id", flat=True))
        with patch(
            "apps.messaging.services.ConnectionManager.send_text_message",
            side_effect=_sequential_send_results(),
        ):
            for index, message_id in enumerate(message_ids):
                if index == 10:
                    # Device A drops halfway through the run.
                    WhatsAppAccount.objects.filter(pk=accounts[0].pk).update(
                        status=WhatsAppAccount.Status.DISCONNECTED
                    )
                send_message_task(message_id)

        campaign.refresh_from_db()
        self.assertEqual(campaign.sent_count, 20, "every recipient must still be accounted for")
        self.assertEqual(campaign.skipped_count, 0)
        # Everything after the drop must have gone out on the surviving device.
        tail = campaign.messages.order_by("id").values_list("whatsapp_account_id", flat=True)[10:]
        self.assertEqual(set(tail), {accounts[1].pk})

    def test_reconnected_account_is_eligible_again(self):
        accounts = [_account(self.tenant, self.user, name) for name in ("A", "B")]
        campaign = self._campaign(accounts[0], account_assignment_mode=Campaign.AccountAssignmentMode.AUTO)
        WhatsAppAccount.objects.filter(pk=accounts[1].pk).update(status=WhatsAppAccount.Status.DISCONNECTED)
        self.assertEqual(eligible_accounts(campaign), [accounts[0]])

        WhatsAppAccount.objects.filter(pk=accounts[1].pk).update(status=WhatsAppAccount.Status.CONNECTED)
        self.assertEqual({a.pk for a in eligible_accounts(campaign)}, {accounts[0].pk, accounts[1].pk})

    def test_retry_failed_redispatches_with_fresh_delays(self):
        account = _account(self.tenant, self.user, "A")
        self._contacts(5)
        campaign = self._campaign(account, min_delay_seconds=1, max_delay_seconds=5)
        with patch("apps.campaigns.tasks.send_message_task.apply_async"):
            services.launch(campaign)
        campaign.messages.update(status=Message.Status.FAILED, assigned_delay_seconds=None)
        campaign.status = Campaign.Status.COMPLETED
        campaign.save(update_fields=["status"])

        with patch("apps.campaigns.tasks.send_message_task.apply_async") as send:
            services.retry_failed(campaign)

        self.assertEqual(send.call_count, 5)
        for delay in campaign.messages.values_list("assigned_delay_seconds", flat=True):
            self.assertIsNotNone(delay, "retried messages must get a newly generated delay")
            self.assertTrue(1 <= delay <= 5, delay)
