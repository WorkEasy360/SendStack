"""
The new message types going through the existing campaign engine.

Nothing here tests a new engine, because there is not one. What it tests is
that a list or a poll is an ordinary campaign in every respect that already
worked: one message row per recipient, one credit reserved per row, the
audience filter still excluding people who opted out, account rotation still
choosing the number, and the plan's allowance still counted the same way.

The one genuinely new thing is the pre-send **delivery** report, and the rule
it embodies: an unsupported content type is never a blocker. Every kind this
platform accepts is deliverable on every connection, because the one that
cannot draw it sends a numbered list the recipient can still answer. Refusing
the send would be a worse outcome than the degradation it avoided -- so the
preview tells the customer what will happen and lets them decide.
"""

from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.contacts.models import Contact
from apps.messaging.models import Message
from apps.templates_app.models import MessageTemplate
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount
from apps.whatsapp.providers.base import SendResult
from apps.whatsapp.tests_capabilities import configured_qr_provider
from core.interactive import clean_list_content, clean_poll_content

from . import preview as campaign_preview
from .models import Campaign
from .services import launch

LIST_CONTENT = clean_list_content({
    "button": "View services",
    "sections": [{"rows": [{"title": "Web development"}, {"title": "AI automation"}]}],
})

POLL_CONTENT = clean_poll_content({
    "question": "Which service?",
    "options": ["Web development", "AI automation"],
})


def _sent(*args, **kwargs):
    """
    A successful send with a fresh provider id each time.

    `Message.provider_message_id` is unique, so a mock returning one constant
    id makes the *second* recipient of any multi-recipient campaign collide --
    which looks like a bug in the code under test rather than in the double.
    """
    return SendResult(success=True, provider_message_id=uuid4().hex)


class CampaignContentTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="CampC", slug="camp-content", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="camp-content-owner", password="x", tenant=self.tenant, credits=100,
        )
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Cloud",
            connection_method=WhatsAppAccount.ConnectionMethod.CLOUD_API,
            status=WhatsAppAccount.Status.CONNECTED,
        )
        for index in range(3):
            Contact.objects.create(
                tenant=self.tenant, created_by=self.user, name=f"C{index}",
                phone=f"+91987654321{index}", custom_fields={"order_id": f"ORD{index}"},
            )

    def _campaign(self, **kwargs):
        defaults = {
            "tenant": self.tenant, "created_by": self.user, "name": "Camp",
            "whatsapp_account": self.account, "message_body": "Please choose.",
            "append_opt_out": False, "track_links": False,
        }
        return Campaign.objects.create(**{**defaults, **kwargs})

    def test_a_list_campaign_builds_one_message_per_recipient(self):
        campaign = self._campaign(list_content=LIST_CONTENT)

        launch(campaign)

        campaign.refresh_from_db()
        self.assertEqual(campaign.total_recipients, 3)
        self.assertEqual(campaign.messages.count(), 3)
        self.assertTrue(all(message.list_content for message in campaign.messages.all()))

    def test_a_poll_campaign_needs_no_body(self):
        campaign = self._campaign(message_body="", poll_content=POLL_CONTENT)

        with patch(
            "apps.messaging.services.ConnectionManager.send_text_message", side_effect=_sent,
        ):
            launch(campaign)

        campaign.refresh_from_db()
        self.assertEqual(campaign.total_recipients, 3)
        self.assertEqual(campaign.messages.count(), 3)
        self.assertTrue(all(message.poll_content for message in campaign.messages.all()))

    def test_a_campaign_with_nothing_at_all_is_still_refused(self):
        from .services import CampaignError

        campaign = self._campaign(message_body="")

        with self.assertRaises(CampaignError):
            launch(campaign)

    def test_it_charges_one_credit_per_recipient_exactly_as_a_text_campaign_does(self):
        campaign = self._campaign(list_content=LIST_CONTENT)

        with patch(
            "apps.messaging.services.ConnectionManager.send_list_message", side_effect=_sent,
        ):
            launch(campaign)

        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 97)
        self.assertEqual(self.user.reserved_credits, 0)

    def test_insufficient_credits_stop_a_poll_campaign_like_any_other(self):
        from .services import CampaignError

        self.user.credits = 1
        self.user.save(update_fields=["credits"])
        campaign = self._campaign(message_body="", poll_content=POLL_CONTENT)

        with self.assertRaises(CampaignError):
            launch(campaign)

        self.assertEqual(Message.objects.filter(campaign=campaign).count(), 0)

    def test_an_unsubscribed_contact_never_gets_a_list_message(self):
        """The audience filter is the send path's own; content type changes nothing."""
        Contact.objects.filter(phone="+919876543210").update(subscribed=False)
        campaign = self._campaign(list_content=LIST_CONTENT)

        launch(campaign)

        self.assertEqual(campaign.messages.count(), 2)
        self.assertFalse(campaign.messages.filter(to_phone="+919876543210").exists())

    def test_variables_inside_a_menu_row_are_personalized_per_recipient(self):
        campaign = self._campaign(
            list_content=clean_list_content({
                "sections": [{"rows": [{"id": "reorder", "title": "Reorder {{order_id}}"}]}],
            }),
        )

        launch(campaign)

        titles = {
            message.list_content["sections"][0]["rows"][0]["title"]
            for message in campaign.messages.all()
        }
        self.assertEqual(titles, {"Reorder ORD0", "Reorder ORD1", "Reorder ORD2"})

    def test_a_row_id_is_never_personalized(self):
        """It is what automation matches on; a per-recipient id points at nothing."""
        campaign = self._campaign(
            list_content=clean_list_content({
                "sections": [{"rows": [{"id": "reorder", "title": "Reorder {{order_id}}"}]}],
            }),
        )

        launch(campaign)

        ids = {
            message.list_content["sections"][0]["rows"][0]["id"]
            for message in campaign.messages.all()
        }
        self.assertEqual(ids, {"reorder"})

    def test_a_template_supplies_the_content_when_one_is_selected(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant, created_by=self.user, name="Menu",
            body="Please choose.", list_content=LIST_CONTENT,
        )
        campaign = self._campaign(message_body="", template=template)

        launch(campaign)

        self.assertTrue(all(message.list_content for message in campaign.messages.all()))

    def test_an_opt_out_button_is_not_added_to_a_list_message(self):
        """
        A menu and a button set are different kinds of message. The opt-out
        falls back to the footer line, which is what keeps a way out on the
        message rather than losing it to a content-type choice.
        """
        from apps.compliance.models import CompliancePolicy

        CompliancePolicy.objects.create(
            tenant=self.tenant, opt_out_style=CompliancePolicy.OptOutStyle.BUTTON,
        )
        campaign = self._campaign(list_content=LIST_CONTENT, append_opt_out=True)

        launch(campaign)

        message = campaign.messages.first()
        self.assertEqual(message.buttons, [])
        self.assertIn("STOP", message.body.upper())


class CampaignDeliveryPreviewTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="CampD", slug="camp-delivery", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="camp-delivery-owner", password="x", tenant=self.tenant, credits=100,
        )
        self.qr = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="QR",
            connection_method=WhatsAppAccount.ConnectionMethod.QR,
            status=WhatsAppAccount.Status.CONNECTED,
        )
        Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Aman", phone="+919876543210",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _campaign(self, **kwargs):
        defaults = {
            "tenant": self.tenant, "created_by": self.user, "name": "Camp",
            "whatsapp_account": self.qr, "message_body": "Please choose.",
        }
        return Campaign.objects.create(**{**defaults, **kwargs})

    def test_a_text_campaign_reports_native_delivery_and_no_fallback(self):
        result = campaign_preview.build(self._campaign())["delivery"]

        self.assertEqual(result["content_kind"], "TEXT")
        self.assertTrue(result["native"])
        self.assertEqual(result["as_sent"], "")

    def test_a_list_on_a_qr_only_workspace_names_the_numbers_that_will_degrade(self):
        campaign = self._campaign(list_content=LIST_CONTENT)

        with configured_qr_provider():
            result = campaign_preview.build(campaign)["delivery"]

        self.assertEqual(result["content_kind"], "LIST")
        self.assertFalse(result["native"])
        self.assertEqual([row["name"] for row in result["degraded_accounts"]], ["QR"])
        self.assertIn("1. Web development", result["as_sent"])

    def test_degradation_is_never_a_blocker(self):
        """
        The rule. Every content kind is deliverable on every connection, so a
        preview that refused the send would be refusing something that works.
        """
        campaign = self._campaign(list_content=LIST_CONTENT)

        with configured_qr_provider():
            result = campaign_preview.build(campaign)

        self.assertTrue(result["can_send"])
        self.assertEqual(result["blockers"], [])

    def test_a_poll_is_native_on_this_workspaces_qr_number(self):
        campaign = self._campaign(message_body="", poll_content=POLL_CONTENT)

        with configured_qr_provider():
            result = campaign_preview.build(campaign)["delivery"]

        self.assertTrue(result["native"])

    def test_the_preview_endpoint_carries_the_delivery_section(self):
        campaign = self._campaign(list_content=LIST_CONTENT)

        response = self.client.get(f"/api/v1/campaigns/campaigns/{campaign.pk}/preview/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("delivery", response.json())

    def test_the_preview_writes_nothing(self):
        campaign = self._campaign(list_content=LIST_CONTENT)

        campaign_preview.build(campaign)

        self.assertEqual(Message.objects.filter(campaign=campaign).count(), 0)
        self.user.refresh_from_db()
        self.assertEqual(self.user.reserved_credits, 0)


class CampaignContentApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="CampApi", slug="camp-api-content", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="camp-api-content-owner", password="x", tenant=self.tenant, credits=100,
        )
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Cloud",
            connection_method=WhatsAppAccount.ConnectionMethod.CLOUD_API,
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _post(self, **overrides):
        payload = {
            "name": "Camp", "whatsapp_account": self.account.pk,
            "message_body": "Please choose.", "audience_type": "ALL",
        }
        payload.update(overrides)
        return self.client.post("/api/v1/campaigns/campaigns/", payload, format="json")

    def test_a_list_campaign_can_be_created_through_the_api(self):
        response = self._post(list_content=LIST_CONTENT)

        self.assertEqual(response.status_code, 201, response.json())

    def test_a_poll_campaign_needs_no_body_through_the_api(self):
        response = self._post(message_body="", poll_content=POLL_CONTENT)

        self.assertEqual(response.status_code, 201, response.json())

    def test_a_campaign_cannot_carry_two_interactive_kinds(self):
        response = self._post(list_content=LIST_CONTENT, poll_content=POLL_CONTENT)

        self.assertEqual(response.status_code, 400)

    def test_a_campaign_with_no_content_at_all_is_refused(self):
        response = self._post(message_body="")

        self.assertEqual(response.status_code, 400)

    def test_a_malformed_poll_is_refused_on_save(self):
        response = self._post(message_body="", poll_content={"question": "Q", "options": ["only one"]})

        self.assertEqual(response.status_code, 400)
        self.assertIn("at least", str(response.json()))
