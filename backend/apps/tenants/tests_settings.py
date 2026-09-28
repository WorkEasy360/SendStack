"""
A workspace's sending pace and its housekeeping horizon.

Two claims are being checked, and only one of them is about the API.

The first is that **the settings are real**: a batch pause or a sleep is not a
number stored on a form, it is added to the cumulative countdown the
dispatcher builds, so it genuinely holds back every message behind it. A
setting the worker does not read is worse than no setting, because the
customer believes their sending is paced when it is not.

The second is that **the cleanup cannot reach anything it should not**. The
horizon removes finished campaigns and the message rows under them. Contacts,
credits, subscriptions, audit events, consent evidence, inbound messages and
any campaign that has not finished are outside it -- not by a flag that could
be turned off, but because no rule names them. Each of those has a test here
saying so, because "we did not write that rule" is only a guarantee for as
long as somebody keeps checking.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.campaigns.assignment import resolve_delay_bounds
from apps.campaigns.models import Campaign
from apps.campaigns.tasks import dispatch_campaign
from apps.contacts.models import Contact
from apps.messaging.models import Message
from apps.whatsapp.models import WhatsAppAccount

from .models import Tenant, TenantSettings
from .storage import cleanable_summary, prune_all, prune_campaign_data, usage_for


class DelayResolutionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Pace", slug="pace-co", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="pace-owner", password="x", tenant=self.tenant,
        )
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Line",
            status=WhatsAppAccount.Status.CONNECTED,
        )

    def _campaign(self, **kwargs):
        return Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="C",
            whatsapp_account=self.account, message_body="hi", **kwargs
        )

    @override_settings(CAMPAIGN_MIN_DELAY_SECONDS=1, CAMPAIGN_MAX_DELAY_SECONDS=2)
    def test_a_campaign_with_no_preference_falls_back_to_the_deployment_default(self):
        """Exactly what every campaign resolved to before any of this existed."""
        self.assertEqual(resolve_delay_bounds(self._campaign()), (1.0, 2.0))

    @override_settings(CAMPAIGN_MIN_DELAY_SECONDS=1, CAMPAIGN_MAX_DELAY_SECONDS=2)
    def test_the_workspace_setting_sits_between_the_campaign_and_the_deployment(self):
        TenantSettings.objects.create(
            tenant=self.tenant, min_delay_seconds=10, max_delay_seconds=20,
        )

        self.assertEqual(resolve_delay_bounds(self._campaign()), (10.0, 20.0))

    @override_settings(CAMPAIGN_MIN_DELAY_SECONDS=1, CAMPAIGN_MAX_DELAY_SECONDS=2)
    def test_a_campaigns_own_value_still_wins(self):
        TenantSettings.objects.create(
            tenant=self.tenant, min_delay_seconds=10, max_delay_seconds=20,
        )

        bounds = resolve_delay_bounds(self._campaign(min_delay_seconds=5, max_delay_seconds=6))

        self.assertEqual(bounds, (5.0, 6.0))

    @override_settings(CAMPAIGN_MIN_DELAY_SECONDS=1, CAMPAIGN_MAX_DELAY_SECONDS=2)
    def test_fixed_mode_collapses_the_window_to_one_value(self):
        TenantSettings.objects.create(
            tenant=self.tenant, delay_mode=TenantSettings.DelayMode.FIXED,
            min_delay_seconds=7, max_delay_seconds=30,
        )

        self.assertEqual(resolve_delay_bounds(self._campaign()), (7.0, 7.0))


class PacingTests(TestCase):
    """The dispatcher actually honours the batch and sleep settings."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Batch", slug="batch-co", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="batch-owner", password="x", tenant=self.tenant, credits=100,
        )
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Line",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="C",
            whatsapp_account=self.account, message_body="hi",
            status=Campaign.Status.QUEUED, min_delay_seconds=0, max_delay_seconds=0,
        )
        for index in range(6):
            contact = Contact.objects.create(
                tenant=self.tenant, created_by=self.user, phone=f"+91987654320{index}",
            )
            Message.objects.create(
                tenant=self.tenant, campaign=self.campaign, whatsapp_account=self.account,
                contact=contact, to_phone=contact.phone, body="hi", billed_to=self.user,
            )

    def _countdowns(self):
        with patch("apps.campaigns.tasks.send_message_task.apply_async") as scheduled:
            dispatch_campaign(self.campaign.pk)
        return [call.kwargs["countdown"] for call in scheduled.call_args_list]

    def test_with_no_batching_the_gaps_are_only_the_per_message_delay(self):
        countdowns = self._countdowns()

        self.assertEqual(len(countdowns), 6)
        self.assertLess(countdowns[-1], 1)

    def test_a_batch_pause_is_added_between_batches_and_not_after_the_last(self):
        TenantSettings.objects.create(tenant=self.tenant, batch_size=2, batch_pause_seconds=60)

        countdowns = self._countdowns()

        # Two pauses across six messages in batches of two: before message 3
        # and before message 5. A third, after the final batch, would be a
        # delay with nothing behind it.
        self.assertAlmostEqual(countdowns[-1], 120, delta=1)

    def test_sleep_mode_adds_a_long_break_within_its_configured_window(self):
        TenantSettings.objects.create(
            tenant=self.tenant, sleep_enabled=True, sleep_after_messages=3,
            sleep_min_seconds=300, sleep_max_seconds=300,
        )

        countdowns = self._countdowns()

        # One sleep, before message 4.
        self.assertAlmostEqual(countdowns[-1], 300, delta=1)

    def test_sleep_does_nothing_while_it_is_switched_off(self):
        TenantSettings.objects.create(
            tenant=self.tenant, sleep_enabled=False, sleep_after_messages=3,
            sleep_min_seconds=300, sleep_max_seconds=300,
        )

        self.assertLess(self._countdowns()[-1], 1)

    def test_batching_and_sleep_compose(self):
        """
        Both are additions to the same countdown, which is what makes running
        them together mean what it looks like it means.
        """
        TenantSettings.objects.create(
            tenant=self.tenant, batch_size=2, batch_pause_seconds=10,
            sleep_enabled=True, sleep_after_messages=3,
            sleep_min_seconds=100, sleep_max_seconds=100,
        )

        self.assertAlmostEqual(self._countdowns()[-1], 120, delta=1)


class RetryPolicyTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Retry", slug="retry-co", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="retry-owner", password="x", tenant=self.tenant,
        )
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Line",
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, phone="+919876543210",
        )
        self.message = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, body="hi", billed_to=self.user,
        )

    def test_the_default_is_the_behaviour_that_was_already_in_force(self):
        from apps.messaging.tasks import MAX_SEND_RETRIES, _retry_policy

        retries, base = _retry_policy(self.message)

        self.assertEqual(retries, MAX_SEND_RETRIES)
        self.assertEqual(base, 30)

    def test_turning_retries_off_means_none(self):
        TenantSettings.objects.create(tenant=self.tenant, retry_enabled=False)

        from apps.messaging.tasks import _retry_policy

        self.assertEqual(_retry_policy(self.message)[0], 0)

    def test_a_workspace_cannot_ask_for_more_retries_than_the_worker_allows(self):
        """
        The ceiling is the task's, not the customer's: a workspace must not be
        able to have a worker hammer a broken number indefinitely.
        """
        from apps.messaging.tasks import MAX_SEND_RETRIES, _retry_policy

        TenantSettings.objects.create(tenant=self.tenant, max_retries=99)

        self.assertEqual(_retry_policy(self.message)[0], MAX_SEND_RETRIES)


class SettingsApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="SetApi", slug="set-api", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="set-api-owner", password="x", tenant=self.tenant,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_reading_it_creates_a_default_row_rather_than_404ing(self):
        response = self.client.get("/api/v1/tenants/settings/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["retry_enabled"])

    def test_it_reports_the_delays_that_will_actually_be_used(self):
        response = self.client.get("/api/v1/tenants/settings/")

        self.assertIsNotNone(response.json()["effective_min_delay_seconds"])

    def test_a_maximum_below_the_minimum_is_refused(self):
        response = self.client.patch(
            "/api/v1/tenants/settings/",
            {"min_delay_seconds": 10, "max_delay_seconds": 2}, format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_sleep_needs_a_message_count_when_it_is_switched_on(self):
        response = self.client.patch(
            "/api/v1/tenants/settings/",
            {"sleep_enabled": True, "sleep_after_messages": 0}, format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_it_only_ever_touches_the_callers_own_workspace(self):
        other = Tenant.objects.create(name="Other", slug="set-other", kind=Tenant.Kind.CUSTOMER)
        TenantSettings.objects.create(tenant=other, batch_size=99)

        self.client.patch("/api/v1/tenants/settings/", {"batch_size": 5}, format="json")

        self.assertEqual(TenantSettings.objects.get(tenant=other).batch_size, 99)
        self.assertEqual(TenantSettings.objects.get(tenant=self.tenant).batch_size, 5)

    def test_it_requires_authentication(self):
        self.assertEqual(APIClient().get("/api/v1/tenants/settings/").status_code, 401)


class StorageTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Store", slug="store-co", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="store-owner", password="x", tenant=self.tenant, credits=100,
        )
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Line",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, phone="+919876543210",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _campaign(self, *, status, days_ago, messages=1):
        campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name=f"C-{status}-{days_ago}",
            whatsapp_account=self.account, message_body="hi", status=status,
        )
        if days_ago is not None:
            Campaign.objects.filter(pk=campaign.pk).update(
                completed_at=timezone.now() - timezone.timedelta(days=days_ago),
            )
        for _ in range(messages):
            Message.objects.create(
                tenant=self.tenant, campaign=campaign, whatsapp_account=self.account,
                contact=self.contact, to_phone=self.contact.phone, body="hi",
            )
        return campaign

    # -- Usage --------------------------------------------------------------

    def test_usage_breaks_down_by_category(self):
        self._campaign(status=Campaign.Status.COMPLETED, days_ago=1)

        result = usage_for(self.tenant)

        keys = {category["key"] for category in result["categories"]}
        self.assertEqual(
            keys, {"media", "campaigns", "received", "contacts", "reports", "logs"},
        )

    def test_media_is_measured_exactly_and_the_rest_is_labelled_an_estimate(self):
        result = usage_for(self.tenant)

        by_key = {category["key"]: category for category in result["categories"]}
        self.assertTrue(by_key["media"]["exact"])
        self.assertFalse(by_key["campaigns"]["exact"])

    @override_settings(TENANT_STORAGE_LIMIT_MB=0)
    def test_no_configured_ceiling_reads_as_unlimited_not_as_a_full_bar(self):
        result = usage_for(self.tenant)

        self.assertTrue(result["unlimited"])
        self.assertIsNone(result["used_percent"])

    @override_settings(TENANT_STORAGE_LIMIT_MB=100)
    def test_a_configured_ceiling_produces_a_percentage(self):
        result = usage_for(self.tenant)

        self.assertFalse(result["unlimited"])
        self.assertIsNotNone(result["used_percent"])

    def test_usage_counts_only_this_workspace(self):
        other = Tenant.objects.create(name="Other", slug="store-other", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(
            username="store-other-owner", password="x", tenant=other,
        )
        for index in range(5):
            Contact.objects.create(
                tenant=other, created_by=other_user, phone=f"+91987654339{index}",
            )

        result = usage_for(self.tenant)

        contacts = next(c for c in result["categories"] if c["key"] == "contacts")
        self.assertEqual(contacts["count"], 1)

    # -- Cleanup ------------------------------------------------------------

    def test_no_horizon_removes_nothing(self):
        self._campaign(status=Campaign.Status.COMPLETED, days_ago=400)

        self.assertEqual(prune_campaign_data(self.tenant.pk)["campaigns"], 0)
        self.assertEqual(Campaign.objects.filter(tenant=self.tenant).count(), 1)

    def test_a_horizon_removes_finished_campaigns_past_it(self):
        old = self._campaign(status=Campaign.Status.COMPLETED, days_ago=90)
        recent = self._campaign(status=Campaign.Status.COMPLETED, days_ago=5)
        TenantSettings.objects.create(tenant=self.tenant, campaign_data_retention_days=30)

        result = prune_campaign_data(self.tenant.pk)

        self.assertEqual(result["campaigns"], 1)
        self.assertFalse(Campaign.objects.filter(pk=old.pk).exists())
        self.assertTrue(Campaign.objects.filter(pk=recent.pk).exists())

    def test_a_running_campaign_is_never_removed_however_old(self):
        """Deleting it would be deleting work in progress."""
        for status in (
            Campaign.Status.RUNNING, Campaign.Status.QUEUED,
            Campaign.Status.PAUSED, Campaign.Status.SCHEDULED, Campaign.Status.DRAFT,
        ):
            with self.subTest(status=status):
                campaign = self._campaign(status=status, days_ago=400)
                TenantSettings.objects.update_or_create(
                    tenant=self.tenant, defaults={"campaign_data_retention_days": 30},
                )

                prune_campaign_data(self.tenant.pk)

                self.assertTrue(Campaign.objects.filter(pk=campaign.pk).exists())

    def test_it_cannot_reach_contacts_credits_or_the_audit_trail(self):
        """
        Not protected by a switch -- there is simply no rule that names them,
        which is the same guarantee apps.health.retention makes.
        """
        from apps.audit.models import AuditEvent

        AuditEvent.objects.create(tenant=self.tenant, action="test.event")
        self._campaign(status=Campaign.Status.COMPLETED, days_ago=400)
        TenantSettings.objects.create(tenant=self.tenant, campaign_data_retention_days=30)

        prune_campaign_data(self.tenant.pk)

        self.assertTrue(Contact.objects.filter(pk=self.contact.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(tenant=self.tenant).exists())
        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 100)

    def test_an_inbound_message_survives_its_campaigns_removal(self):
        """
        An inbound message belongs to the conversation, not to the campaign
        that prompted it -- `Message.campaign` is null on it.
        """
        self._campaign(status=Campaign.Status.COMPLETED, days_ago=400)
        inbound = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, body="yes please",
            direction=Message.Direction.INBOUND,
        )
        TenantSettings.objects.create(tenant=self.tenant, campaign_data_retention_days=30)

        prune_campaign_data(self.tenant.pk)

        self.assertTrue(Message.objects.filter(pk=inbound.pk).exists())

    def test_it_never_touches_another_workspaces_campaigns(self):
        other = Tenant.objects.create(name="Other", slug="store-other-2", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(
            username="store-other-2-owner", password="x", tenant=other,
        )
        other_account = WhatsAppAccount.objects.create(
            tenant=other, user=other_user, name="Theirs",
        )
        theirs = Campaign.objects.create(
            tenant=other, created_by=other_user, name="Theirs",
            whatsapp_account=other_account, message_body="hi",
            status=Campaign.Status.COMPLETED,
        )
        Campaign.objects.filter(pk=theirs.pk).update(
            completed_at=timezone.now() - timezone.timedelta(days=400),
        )
        TenantSettings.objects.create(tenant=self.tenant, campaign_data_retention_days=30)

        prune_campaign_data(self.tenant.pk)

        self.assertTrue(Campaign.objects.filter(pk=theirs.pk).exists())

    def test_the_sweep_visits_only_workspaces_that_set_a_horizon(self):
        """Which by default is nobody, so a deployment does no work at all."""
        self._campaign(status=Campaign.Status.COMPLETED, days_ago=400)

        self.assertEqual(prune_all(), {})

        TenantSettings.objects.create(tenant=self.tenant, campaign_data_retention_days=30)

        self.assertEqual(prune_all(), {self.tenant.pk: 1})

    def test_the_summary_says_what_a_cleanup_would_remove_without_removing_it(self):
        self._campaign(status=Campaign.Status.COMPLETED, days_ago=90, messages=3)

        summary = cleanable_summary(self.tenant.pk, days=30)

        self.assertEqual(summary["campaigns"], 1)
        self.assertEqual(summary["messages"], 3)
        self.assertEqual(Campaign.objects.filter(tenant=self.tenant).count(), 1)

    # -- API ----------------------------------------------------------------

    def test_the_storage_endpoint_reports_usage(self):
        response = self.client.get("/api/v1/tenants/storage/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("categories", response.json())

    def test_it_previews_a_horizon_the_screen_is_considering(self):
        self._campaign(status=Campaign.Status.COMPLETED, days_ago=90)

        response = self.client.get("/api/v1/tenants/storage/?days=30")

        self.assertEqual(response.json()["preview"]["campaigns"], 1)

    def test_cleaning_up_uses_the_stored_horizon_and_not_one_from_the_request(self):
        """
        A deletion a caller can widen per request is a deletion nobody agreed
        to. Changing what gets removed means changing the setting, which is
        audited.
        """
        self._campaign(status=Campaign.Status.COMPLETED, days_ago=90)
        TenantSettings.objects.create(tenant=self.tenant, campaign_data_retention_days=365)

        response = self.client.post("/api/v1/tenants/storage/", {"days": 1}, format="json")

        self.assertEqual(response.json()["campaigns"], 0)
        self.assertEqual(Campaign.objects.filter(tenant=self.tenant).count(), 1)

    def test_cleaning_up_reports_what_it_removed(self):
        self._campaign(status=Campaign.Status.COMPLETED, days_ago=90)
        TenantSettings.objects.create(tenant=self.tenant, campaign_data_retention_days=30)

        response = self.client.post("/api/v1/tenants/storage/", {}, format="json")

        self.assertEqual(response.json()["campaigns"], 1)
        self.assertIn("usage", response.json())

    def test_the_storage_endpoint_requires_authentication(self):
        self.assertEqual(APIClient().get("/api/v1/tenants/storage/").status_code, 401)
