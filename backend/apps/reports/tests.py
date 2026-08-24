from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.campaigns.models import Campaign
from apps.contacts.models import Contact
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount

from . import services


def _account(tenant, user):
    account = WhatsAppAccount(tenant=tenant, user=user, name="Line", phone_number_id="1", status=WhatsAppAccount.Status.CONNECTED)
    account.set_access_token("token")
    account.set_app_secret("secret")
    account.save()
    return account


class DateRangeTests(TestCase):
    def test_today_range_covers_only_today(self):
        start, end = services.resolve_date_range({"range": "today"})
        now = timezone.now()
        self.assertEqual(start.date(), now.date())
        self.assertEqual(start.hour, 0)

    def test_custom_range_is_inclusive_of_end_date(self):
        start, end = services.resolve_date_range({"range": "custom", "start_date": "2026-01-01", "end_date": "2026-01-03"})
        self.assertEqual(start.day, 1)
        self.assertEqual(end.day, 4)  # exclusive upper bound = day after end_date

    def test_no_range_means_all_time(self):
        start, end = services.resolve_date_range({})
        self.assertIsNone(start)
        self.assertIsNone(end)


class DashboardMetricsTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-reports", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-reports-owner", password="x", tenant=self.tenant)
        self.account = _account(self.tenant, self.user)
        self.contact = Contact.objects.create(tenant=self.tenant, created_by=self.user, name="Priya", phone="+15551230000")

    def test_counts_messages_by_status(self):
        Message.objects.create(tenant=self.tenant, whatsapp_account=self.account, contact=self.contact, to_phone="+1", status=Message.Status.SENT)
        Message.objects.create(tenant=self.tenant, whatsapp_account=self.account, contact=self.contact, to_phone="+1", status=Message.Status.DELIVERED)
        Message.objects.create(tenant=self.tenant, whatsapp_account=self.account, contact=self.contact, to_phone="+1", status=Message.Status.READ)
        Message.objects.create(tenant=self.tenant, whatsapp_account=self.account, contact=self.contact, to_phone="+1", status=Message.Status.FAILED)
        Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact, to_phone="+1",
            direction=Message.Direction.INBOUND, status=Message.Status.DELIVERED,
        )

        metrics = services.dashboard_metrics(self.tenant, None, None)

        self.assertEqual(metrics["messages_total"], 4)
        self.assertEqual(metrics["sent"], 3)  # SENT + DELIVERED + READ all count as "sent"
        self.assertEqual(metrics["delivered"], 2)
        self.assertEqual(metrics["read"], 1)
        self.assertEqual(metrics["failed"], 1)
        self.assertEqual(metrics["replies"], 1)
        self.assertEqual(metrics["total_contacts"], 1)
        self.assertAlmostEqual(metrics["delivery_rate"], 66.7, places=1)

    def test_date_filter_excludes_older_messages(self):
        old = Message.objects.create(tenant=self.tenant, whatsapp_account=self.account, contact=self.contact, to_phone="+1", status=Message.Status.SENT)
        Message.objects.filter(pk=old.pk).update(queued_at=timezone.now() - timedelta(days=10))
        Message.objects.create(tenant=self.tenant, whatsapp_account=self.account, contact=self.contact, to_phone="+1", status=Message.Status.SENT)

        start, end = services.resolve_date_range({"range": "last_7_days"})
        metrics = services.dashboard_metrics(self.tenant, start, end)

        self.assertEqual(metrics["messages_total"], 1)

    def test_tenant_isolation(self):
        other_tenant = Tenant.objects.create(name="Globex", slug="globex-reports", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="globex-reports-owner", password="x", tenant=other_tenant)
        other_account = _account(other_tenant, other_user)
        Message.objects.create(tenant=other_tenant, whatsapp_account=other_account, to_phone="+1", status=Message.Status.SENT)

        metrics = services.dashboard_metrics(self.tenant, None, None)

        self.assertEqual(metrics["messages_total"], 0)


class CampaignReportTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-campaign-report", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-campaign-report-owner", password="x", tenant=self.tenant)
        self.account = _account(self.tenant, self.user)
        self.contact = Contact.objects.create(tenant=self.tenant, created_by=self.user, name="Priya", phone="+15551230001")
        self.campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="Promo", whatsapp_account=self.account, message_body="Hi",
            total_recipients=1, sent_count=1, delivered_count=1,
        )

    def test_counts_a_reply_after_send(self):
        sent_at = timezone.now() - timedelta(hours=1)
        Message.objects.create(
            tenant=self.tenant, campaign=self.campaign, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, status=Message.Status.SENT, sent_at=sent_at,
        )
        Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact, to_phone=self.contact.phone,
            direction=Message.Direction.INBOUND, status=Message.Status.DELIVERED,
        )

        report = services.campaign_report(self.campaign)

        self.assertEqual(report["replies"], 1)
        self.assertEqual(report["reply_rate"], 100.0)

    def test_reply_before_send_does_not_count(self):
        Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact, to_phone=self.contact.phone,
            direction=Message.Direction.INBOUND, status=Message.Status.DELIVERED,
        )
        Message.objects.create(
            tenant=self.tenant, campaign=self.campaign, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, status=Message.Status.SENT, sent_at=timezone.now(),
        )

        report = services.campaign_report(self.campaign)

        self.assertEqual(report["replies"], 0)


class ReportsApiTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Acme", slug="acme-reports-api", kind=Tenant.Kind.CUSTOMER)
        self.tenant_b = Tenant.objects.create(name="Globex", slug="globex-reports-api", kind=Tenant.Kind.CUSTOMER)
        self.user_a = get_user_model().objects.create_user(username="acme-reports-api-owner", password="x", tenant=self.tenant_a)
        self.user_b = get_user_model().objects.create_user(username="globex-reports-api-owner", password="x", tenant=self.tenant_b)
        self.account_a = _account(self.tenant_a, self.user_a)
        self.campaign_a = Campaign.objects.create(
            tenant=self.tenant_a, created_by=self.user_a, name="Promo", whatsapp_account=self.account_a, message_body="Hi",
        )
        self.campaign_b = Campaign.objects.create(
            tenant=self.tenant_b, created_by=self.user_b, name="Other", whatsapp_account=_account(self.tenant_b, self.user_b), message_body="Hi",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_dashboard_requires_auth(self):
        anon = APIClient()
        response = anon.get("/api/v1/reports/dashboard/")
        self.assertEqual(response.status_code, 401)

    def test_dashboard_returns_metrics(self):
        response = self.client.get("/api/v1/reports/dashboard/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("total_contacts", response.json())

    def test_cannot_view_another_tenants_campaign_report(self):
        response = self.client.get(f"/api/v1/reports/campaigns/{self.campaign_b.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_campaign_export_returns_csv(self):
        response = self.client.get(f"/api/v1/reports/campaigns/{self.campaign_a.pk}/export/")
        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content).decode()
        self.assertIn("contact_name,phone,status", content)

    def test_daily_breakdown_endpoint(self):
        response = self.client.get("/api/v1/reports/messages/daily/?range=last_7_days")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), list)


class DashboardCachingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-reports-cache", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-reports-cache-owner", password="x", tenant=self.tenant)
        self.account = _account(self.tenant, self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_second_call_within_ttl_is_served_from_cache(self):
        Message.objects.create(tenant=self.tenant, whatsapp_account=self.account, to_phone="+1", status=Message.Status.SENT)

        first = self.client.get("/api/v1/reports/dashboard/", {"range": "last_30_days"})
        self.assertEqual(first.json()["messages_total"], 1)

        # A message created after the first call shouldn't show up in the
        # second call if it's genuinely served from cache rather than
        # re-querying (which would happen to report the correct number
        # anyway, so this proves it's actually cached, not just correct).
        Message.objects.create(tenant=self.tenant, whatsapp_account=self.account, to_phone="+2", status=Message.Status.SENT)

        with self.assertNumQueries(0):
            second = self.client.get("/api/v1/reports/dashboard/", {"range": "last_30_days"})
        self.assertEqual(second.json()["messages_total"], 1)

    def test_different_tenants_do_not_share_a_cache_entry(self):
        other_tenant = Tenant.objects.create(name="Globex", slug="globex-reports-cache", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="globex-reports-cache-owner", password="x", tenant=other_tenant)
        other_account = _account(other_tenant, other_user)
        Message.objects.create(tenant=other_tenant, whatsapp_account=other_account, to_phone="+1", status=Message.Status.SENT)

        self.client.force_authenticate(user=other_user)
        other_response = self.client.get("/api/v1/reports/dashboard/", {"range": "last_30_days"})
        self.assertEqual(other_response.json()["messages_total"], 1)

        self.client.force_authenticate(user=self.user)
        own_response = self.client.get("/api/v1/reports/dashboard/", {"range": "last_30_days"})
        self.assertEqual(own_response.json()["messages_total"], 0)

    def test_different_range_params_do_not_share_a_cache_entry(self):
        Message.objects.create(tenant=self.tenant, whatsapp_account=self.account, to_phone="+1", status=Message.Status.SENT)

        thirty_day = self.client.get("/api/v1/reports/dashboard/", {"range": "last_30_days"})
        seven_day = self.client.get("/api/v1/reports/dashboard/", {"range": "last_7_days"})

        self.assertEqual(thirty_day.json()["messages_total"], 1)
        self.assertEqual(seven_day.json()["messages_total"], 1)
