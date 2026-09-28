"""
The pre-send preview: does the number a customer is shown match what happens?

The one property that matters here, and the reason the module exists, is that
the preview and the send agree. So the tests are mostly about the split -- how
many are eligible, how many are skipped and for which reason -- under the same
compliance policies the send path runs under.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.campaigns import preview as campaign_preview
from apps.campaigns.models import Campaign
from apps.compliance.models import CompliancePolicy
from apps.contacts.models import Contact
from apps.subscriptions.models import Plan, Subscription
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount

User = get_user_model()


class PreviewTestCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme", kind=Tenant.Kind.CUSTOMER)
        self.plan = Plan.objects.create(
            name="Growth", price_cents=99900, currency="INR", is_public=True,
            max_messages_per_month=10000, max_contacts=5000, max_devices=3, max_users=10,
        )
        Subscription.objects.create(
            tenant=self.tenant, plan=self.plan, status=Subscription.Status.ACTIVE,
        )
        self.user = User.objects.create_user(
            username="acme-owner", password="pw", tenant=self.tenant, role=User.Role.USER,
            credits=100000,
        )
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Line",
            status=WhatsAppAccount.Status.CONNECTED, phone_number="+15000000001",
        )
        self.campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, whatsapp_account=self.account,
            name="Offer", message_body="Hello", audience_type=Campaign.AudienceType.ALL,
        )

    def make_contacts(self, *, subscribed=0, opted_out=0, consent_granted=0, consent_unknown=0):
        rows = []
        serial = Contact.objects.filter(tenant=self.tenant).count()

        def add(count, **kwargs):
            nonlocal serial
            for _ in range(count):
                serial += 1
                rows.append(Contact(
                    tenant=self.tenant, created_by=self.user,
                    name=f"C{serial}", phone=f"+1500{serial:07d}", **kwargs,
                ))

        add(subscribed, subscribed=True)
        add(opted_out, subscribed=False)
        add(consent_granted, subscribed=True, marketing_consent=Contact.MarketingConsent.GRANTED)
        add(consent_unknown, subscribed=True, marketing_consent=Contact.MarketingConsent.UNKNOWN)
        Contact.objects.bulk_create(rows)


class EligibilitySplitTests(PreviewTestCase):
    def test_opted_out_contacts_are_counted_and_excluded(self):
        self.make_contacts(subscribed=10, opted_out=3)
        result = campaign_preview.build(self.campaign)

        self.assertEqual(result["selected"], 13)
        self.assertEqual(result["eligible"], 10)
        opted = next(item for item in result["skipped"] if item["reason"] == "opted_out")
        self.assertEqual(opted["count"], 3)

    def test_without_a_consent_policy_every_subscribed_contact_is_eligible(self):
        self.make_contacts(subscribed=5, consent_unknown=4)
        result = campaign_preview.build(self.campaign)
        self.assertEqual(result["eligible"], 9)
        self.assertEqual(
            [item for item in result["skipped"] if item["reason"] == "consent_missing"], [],
        )

    def test_with_a_consent_policy_unknown_consent_is_skipped(self):
        policy = CompliancePolicy.for_tenant(self.tenant.pk)
        policy.require_marketing_consent = True
        policy.save()

        self.make_contacts(consent_granted=6, consent_unknown=4, opted_out=2)
        result = campaign_preview.build(self.campaign)

        self.assertEqual(result["selected"], 12)
        self.assertEqual(result["eligible"], 6)
        consent = next(item for item in result["skipped"] if item["reason"] == "consent_missing")
        self.assertEqual(consent["count"], 4)
        opted = next(item for item in result["skipped"] if item["reason"] == "opted_out")
        self.assertEqual(opted["count"], 2)

    def test_the_split_always_adds_up(self):
        policy = CompliancePolicy.for_tenant(self.tenant.pk)
        policy.require_marketing_consent = True
        policy.save()
        self.make_contacts(consent_granted=7, consent_unknown=3, opted_out=5)

        result = campaign_preview.build(self.campaign)
        self.assertEqual(result["eligible"] + result["skipped_total"], result["selected"])

    def test_an_empty_audience_reports_a_blocker_rather_than_zero_silently(self):
        result = campaign_preview.build(self.campaign)
        self.assertEqual(result["eligible"], 0)
        self.assertFalse(result["can_send"])
        self.assertTrue(any("No contact" in b for b in result["blockers"]))


class CostTests(PreviewTestCase):
    def test_credits_needed_equals_eligible_recipients(self):
        self.make_contacts(subscribed=42, opted_out=8)
        result = campaign_preview.build(self.campaign)
        self.assertEqual(result["credits"]["needed"], 42)

    def test_skipped_contacts_are_not_charged_for(self):
        self.make_contacts(subscribed=10, opted_out=90)
        result = campaign_preview.build(self.campaign)
        self.assertEqual(result["credits"]["needed"], 10)

    def test_reserved_credits_are_not_available(self):
        """
        A preview that ignored another campaign's reservation would promise a
        send the reservation then refuses.
        """
        self.make_contacts(subscribed=10)
        self.user.credits = 100
        self.user.reserved_credits = 95
        self.user.save()
        self.campaign.refresh_from_db()

        result = campaign_preview.build(self.campaign)
        self.assertEqual(result["credits"]["available"], 5)
        self.assertFalse(result["credits"]["sufficient"])
        self.assertEqual(result["credits"]["shortfall"], 5)
        self.assertFalse(result["can_send"])

    def test_a_send_over_the_plan_allowance_is_reported_as_a_blocker(self):
        self.plan.max_messages_per_month = 5
        self.plan.save()
        self.make_contacts(subscribed=50)

        result = campaign_preview.build(self.campaign)
        self.assertFalse(result["plan"]["within_plan"])
        self.assertFalse(result["can_send"])


class DeviceTests(PreviewTestCase):
    def test_no_connected_device_blocks_the_send(self):
        self.account.status = WhatsAppAccount.Status.DISCONNECTED
        self.account.save()
        self.make_contacts(subscribed=10)

        result = campaign_preview.build(self.campaign)
        self.assertFalse(result["devices"]["ready"])
        self.assertFalse(result["can_send"])
        self.assertTrue(any("connected" in b for b in result["blockers"]))

    def test_a_ready_workspace_can_send(self):
        self.make_contacts(subscribed=10)
        result = campaign_preview.build(self.campaign)
        self.assertTrue(result["can_send"])
        self.assertEqual(result["blockers"], [])


class PreviewIsReadOnlyTests(PreviewTestCase):
    def test_preview_creates_no_messages_and_reserves_no_credits(self):
        from apps.messaging.models import Message

        self.make_contacts(subscribed=25)
        before = self.user.reserved_credits

        campaign_preview.build(self.campaign)

        self.user.refresh_from_db()
        self.assertEqual(self.user.reserved_credits, before)
        self.assertEqual(Message.objects.filter(campaign=self.campaign).count(), 0)


class PreviewApiTests(PreviewTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_endpoint_returns_the_split(self):
        self.make_contacts(subscribed=10, opted_out=2)
        response = self.client.get(
            reverse("campaign-preview", kwargs={"pk": self.campaign.pk}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["selected"], 12)
        self.assertEqual(response.data["eligible"], 10)

    def test_another_tenants_campaign_is_not_previewable(self):
        other = Tenant.objects.create(name="Other", slug="other", kind=Tenant.Kind.CUSTOMER)
        other_user = User.objects.create_user(
            username="other-owner", password="pw", tenant=other, role=User.Role.USER,
        )
        other_account = WhatsAppAccount.objects.create(
            tenant=other, user=other_user, name="Their line",
            status=WhatsAppAccount.Status.CONNECTED, phone_number="+15000009999",
        )
        foreign = Campaign.objects.create(
            tenant=other, created_by=other_user, whatsapp_account=other_account,
            name="Theirs", message_body="Hi", audience_type=Campaign.AudienceType.ALL,
        )
        response = self.client.get(reverse("campaign-preview", kwargs={"pk": foreign.pk}))
        self.assertEqual(response.status_code, 404)
