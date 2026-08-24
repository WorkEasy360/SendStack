from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.campaigns.models import Campaign
from apps.contacts.models import Contact
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount
from apps.whatsapp.providers.base import SendResult

from . import services
from .models import LinkClick, TrackedLink

TRACKING = dict(LINK_TRACKING_ENABLED=True, LINK_TRACKING_BASE_URL="https://msg.example.com")


def _account(tenant, user):
    account = WhatsAppAccount(
        tenant=tenant, user=user, name="Line", phone_number_id="1", status=WhatsAppAccount.Status.CONNECTED,
    )
    account.set_access_token("token")
    account.set_app_secret("secret")
    account.save()
    return account


class Fixture(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug=f"acme-{self._testMethodName}", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username=f"owner-{self._testMethodName}", password="x", tenant=self.tenant, credits=100,
        )
        self.account = _account(self.tenant, self.user)
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Priya", phone="+15551230001",
        )


@override_settings(**TRACKING)
class RewriteTests(Fixture):
    def test_replaces_url_and_returns_an_unsaved_link(self):
        text, links = services.rewrite("See https://shop.example.com/sale now", tenant_id=self.tenant.pk, contact=self.contact)

        self.assertEqual(len(links), 1)
        self.assertIsNone(links[0].pk)
        self.assertIn(f"https://msg.example.com/r/{links[0].code}", text)
        self.assertNotIn("shop.example.com", text)

    def test_trailing_punctuation_stays_outside_the_link(self):
        text, links = services.rewrite("Buy at https://shop.example.com/sale.", tenant_id=self.tenant.pk)

        self.assertEqual(links[0].original_url, "https://shop.example.com/sale")
        self.assertTrue(text.endswith("."), text)

    def test_balanced_bracket_is_kept_inside_the_url(self):
        _, links = services.rewrite(
            "https://en.wikipedia.org/wiki/Foo_(bar)", tenant_id=self.tenant.pk,
        )

        self.assertEqual(links[0].original_url, "https://en.wikipedia.org/wiki/Foo_(bar)")

    def test_every_recipient_gets_their_own_code(self):
        _, first = services.rewrite("https://a.example.com", tenant_id=self.tenant.pk)
        _, second = services.rewrite("https://a.example.com", tenant_id=self.tenant.pk)

        self.assertNotEqual(first[0].code, second[0].code)

    def test_utm_is_added_to_the_target_but_not_the_stored_original(self):
        _, links = services.rewrite(
            "https://shop.example.com/sale", tenant_id=self.tenant.pk, utm={"utm_source": "whatsapp"},
        )

        self.assertEqual(links[0].original_url, "https://shop.example.com/sale")
        self.assertIn("utm_source=whatsapp", links[0].target_url)

    def test_utm_never_overwrites_one_the_tenant_set(self):
        _, links = services.rewrite(
            "https://shop.example.com/sale?utm_source=newsletter", tenant_id=self.tenant.pk,
            utm={"utm_source": "whatsapp", "utm_medium": "broadcast"},
        )

        self.assertIn("utm_source=newsletter", links[0].target_url)
        self.assertNotIn("utm_source=whatsapp", links[0].target_url)
        self.assertIn("utm_medium=broadcast", links[0].target_url)

    def test_our_own_short_links_are_left_alone(self):
        text, links = services.rewrite("https://msg.example.com/r/abc123", tenant_id=self.tenant.pk)

        self.assertEqual(links, [])
        self.assertEqual(text, "https://msg.example.com/r/abc123")

    def test_text_without_a_url_is_untouched(self):
        text, links = services.rewrite("No links here at all", tenant_id=self.tenant.pk)

        self.assertEqual(text, "No links here at all")
        self.assertEqual(links, [])

    @override_settings(LINK_TRACKING_ENABLED=False)
    def test_disabled_leaves_the_url_exactly_as_typed(self):
        text, links = services.rewrite("Go to https://shop.example.com", tenant_id=self.tenant.pk)

        self.assertEqual(text, "Go to https://shop.example.com")
        self.assertEqual(links, [])

    @override_settings(LINK_TRACKING_BASE_URL="", MEDIA_PUBLIC_BASE_URL="")
    def test_no_public_url_means_no_rewrite(self):
        """Rewriting without somewhere to point would replace a working link with a broken one."""
        text, links = services.rewrite("Go to https://shop.example.com", tenant_id=self.tenant.pk)

        self.assertEqual(text, "Go to https://shop.example.com")
        self.assertEqual(links, [])


@override_settings(**TRACKING)
class RedirectTests(Fixture):
    def _link(self, **kwargs):
        return TrackedLink.objects.create(
            tenant=self.tenant, contact=self.contact, code="abc123xyz789",
            original_url="https://shop.example.com/sale",
            target_url="https://shop.example.com/sale?utm_source=whatsapp", **kwargs,
        )

    def test_redirects_to_the_target_and_records_the_click(self):
        link = self._link()

        response = self.client.get("/r/abc123xyz789", HTTP_USER_AGENT="TestAgent/1.0")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], link.target_url)
        link.refresh_from_db()
        self.assertEqual(link.click_count, 1)
        self.assertIsNotNone(link.first_clicked_at)
        self.assertEqual(LinkClick.objects.get(link=link).user_agent, "TestAgent/1.0")

    def test_unknown_code_is_a_404_not_a_redirect_to_nowhere(self):
        self.assertEqual(self.client.get("/r/does-not-exist").status_code, 404)

    def test_second_click_moves_last_clicked_but_not_first(self):
        link = self._link()
        self.client.get("/r/abc123xyz789")
        link.refresh_from_db()
        first = link.first_clicked_at

        self.client.get("/r/abc123xyz789")
        link.refresh_from_db()

        self.assertEqual(link.click_count, 2)
        self.assertEqual(link.first_clicked_at, first)
        self.assertEqual(LinkClick.objects.filter(link=link).count(), 2)

    def test_redirect_needs_no_login(self):
        """The person tapping is the contact, not a user of the platform."""
        self._link()
        self.assertEqual(self.client.get("/r/abc123xyz789").status_code, 302)


@override_settings(**TRACKING)
class CampaignTrackingTests(Fixture):
    def _campaign(self, **kwargs):
        return Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="Diwali Sale", whatsapp_account=self.account,
            message_body="Shop now: https://shop.example.com/diwali",
            audience_type=Campaign.AudienceType.ALL, append_opt_out=False, **kwargs,
        )

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_launch_rewrites_the_body_and_ties_links_to_the_message(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.1")
        campaign = self._campaign()

        from apps.campaigns.services import launch

        launch(campaign)

        message = Message.objects.get(campaign=campaign)
        link = TrackedLink.objects.get(campaign=campaign)
        self.assertEqual(link.message_id, message.pk)
        self.assertEqual(link.contact_id, self.contact.pk)
        self.assertIn(f"/r/{link.code}", message.body)
        self.assertNotIn("shop.example.com", message.body)

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_campaign_name_becomes_the_utm_campaign(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.2")
        campaign = self._campaign()

        from apps.campaigns.services import launch

        launch(campaign)

        self.assertIn("utm_campaign=diwali-sale", TrackedLink.objects.get(campaign=campaign).target_url)

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_track_links_off_leaves_the_body_alone(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.3")
        campaign = self._campaign(track_links=False)

        from apps.campaigns.services import launch

        launch(campaign)

        self.assertFalse(TrackedLink.objects.filter(campaign=campaign).exists())
        self.assertIn("https://shop.example.com/diwali", Message.objects.get(campaign=campaign).body)


@override_settings(**TRACKING)
class TrackedLinkApiTests(Fixture):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="Sale", whatsapp_account=self.account, message_body="x",
        )

    def _link(self, code, clicks=0, contact=None):
        return TrackedLink.objects.create(
            tenant=self.tenant, campaign=self.campaign, contact=contact or self.contact, code=code,
            original_url="https://shop.example.com/sale", target_url="https://shop.example.com/sale",
            click_count=clicks,
        )

    def test_summary_groups_by_the_url_the_tenant_typed(self):
        other = Contact.objects.create(tenant=self.tenant, created_by=self.user, name="B", phone="+15551230002")
        self._link("aaa", clicks=2)
        self._link("bbb", clicks=0, contact=other)

        response = self.client.get(f"/api/v1/tracking/links/summary/?campaign={self.campaign.pk}")

        self.assertEqual(response.status_code, 200)
        row = response.data["links"][0]
        self.assertEqual(row["url"], "https://shop.example.com/sale")
        self.assertEqual(row["recipients"], 2)
        self.assertEqual(row["clicks"], 2)
        self.assertEqual(row["contacts_clicked"], 1)
        self.assertEqual(row["click_rate"], 50.0)

    def test_clickers_returns_a_retargeting_audience(self):
        other = Contact.objects.create(tenant=self.tenant, created_by=self.user, name="B", phone="+15551230003")
        self._link("ccc", clicks=1)
        self._link("ddd", clicks=0, contact=other)

        response = self.client.get(f"/api/v1/tracking/links/clickers/?campaign={self.campaign.pk}")

        self.assertEqual(response.data["contact_ids"], [self.contact.pk])
        self.assertEqual(response.data["count"], 1)

    def test_another_tenants_links_are_invisible(self):
        other_tenant = Tenant.objects.create(name="Other", slug="other-links", kind=Tenant.Kind.CUSTOMER)
        TrackedLink.objects.create(
            tenant=other_tenant, code="zzz", original_url="https://x.example.com", target_url="https://x.example.com",
        )
        self._link("eee")

        response = self.client.get("/api/v1/tracking/links/")

        codes = [row["code"] for row in response.data["results"]]
        self.assertEqual(codes, ["eee"])
