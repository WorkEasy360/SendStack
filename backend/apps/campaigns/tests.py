from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.contacts.models import Contact, ContactGroup
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.templates_app.models import MessageTemplate
from apps.whatsapp.models import WhatsAppAccount
from apps.whatsapp.providers.base import SendResult

from . import services
from .models import Campaign
from .services import CampaignError
from .tasks import dispatch_campaign


def _account(tenant, user, status=WhatsAppAccount.Status.CONNECTED):
    account = WhatsAppAccount(tenant=tenant, user=user, name="Line", phone_number_id="1", status=status)
    account.set_access_token("token")
    account.set_app_secret("secret")
    account.save()
    return account


class AudienceResolutionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-audience", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-audience-owner", password="x", tenant=self.tenant)
        self.account = _account(self.tenant, self.user)

    def test_all_excludes_unsubscribed(self):
        subscribed = Contact.objects.create(tenant=self.tenant, created_by=self.user, name="A", phone="+15551110001", subscribed=True)
        Contact.objects.create(tenant=self.tenant, created_by=self.user, name="B", phone="+15551110002", subscribed=False)
        campaign = Campaign.objects.create(tenant=self.tenant, created_by=self.user, name="C", whatsapp_account=self.account, message_body="Hi", audience_type=Campaign.AudienceType.ALL)

        self.assertEqual(list(services.resolve_audience(campaign)), [subscribed])

    def test_group_audience(self):
        group = ContactGroup.objects.create(tenant=self.tenant, name="Leads")
        member = Contact.objects.create(tenant=self.tenant, created_by=self.user, name="A", phone="+15551110003")
        member.groups.add(group)
        Contact.objects.create(tenant=self.tenant, created_by=self.user, name="B", phone="+15551110004")
        campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="C", whatsapp_account=self.account, message_body="Hi",
            audience_type=Campaign.AudienceType.GROUP, audience_group=group,
        )

        self.assertEqual(list(services.resolve_audience(campaign)), [member])

    def test_contact_ids_audience(self):
        c1 = Contact.objects.create(tenant=self.tenant, created_by=self.user, name="A", phone="+15551110005")
        Contact.objects.create(tenant=self.tenant, created_by=self.user, name="B", phone="+15551110006")
        campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="C", whatsapp_account=self.account, message_body="Hi",
            audience_type=Campaign.AudienceType.CONTACT_IDS, audience_contact_ids=[c1.pk],
        )

        self.assertEqual(list(services.resolve_audience(campaign)), [c1])


class CampaignLaunchTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-launch", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-launch-owner", password="x", tenant=self.tenant, credits=100)
        self.account = _account(self.tenant, self.user)
        self.contact = Contact.objects.create(tenant=self.tenant, created_by=self.user, name="Priya", phone="+15551230000")

    def _campaign(self, **overrides):
        defaults = dict(
            tenant=self.tenant, created_by=self.user, name="Welcome", whatsapp_account=self.account,
            message_body="Hi [Name]!", audience_type=Campaign.AudienceType.ALL,
        )
        defaults.update(overrides)
        return Campaign.objects.create(**defaults)

    def test_launch_rejects_disconnected_account(self):
        disconnected = _account(self.tenant, self.user, status=WhatsAppAccount.Status.DISCONNECTED)
        campaign = self._campaign(whatsapp_account=disconnected)

        with self.assertRaises(CampaignError):
            services.launch(campaign)

    def test_launch_rejects_empty_audience(self):
        self.contact.subscribed = False
        self.contact.save(update_fields=["subscribed"])
        campaign = self._campaign()

        with self.assertRaises(CampaignError):
            services.launch(campaign)

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_launch_creates_personalized_messages_and_completes(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.1")
        campaign = self._campaign()

        services.launch(campaign)

        message = Message.objects.get(campaign=campaign)
        # startswith, not equals: campaigns carry an opt-out line by default
        # (Campaign.append_opt_out), and what this test is about is the
        # personalisation in front of it.
        self.assertTrue(message.body.startswith("Hi Priya!"), message.body)
        self.assertEqual(message.status, Message.Status.SENT)

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.COMPLETED)
        self.assertEqual(campaign.sent_count, 1)
        self.assertEqual(campaign.total_recipients, 1)

    def test_launch_uses_template_when_selected(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant, created_by=self.user, name="Greeting", body="Hello [Name], welcome!",
        )
        campaign = self._campaign(template=template, message_body="")

        with patch("apps.messaging.services.ConnectionManager.send_text_message") as mock_send:
            mock_send.return_value = SendResult(success=True, provider_message_id="wamid.2")
            services.launch(campaign)

        message = Message.objects.get(campaign=campaign)
        self.assertTrue(message.body.startswith("Hello Priya, welcome!"), message.body)

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_every_recipient_is_told_how_to_get_out(self, mock_send):
        # The opt-out is written into the stored message, not added on the way
        # to the provider: if the campaign is ever questioned, the answer has
        # to come from the row that was sent rather than from a setting that
        # may have changed since.
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.3")
        campaign = self._campaign()

        services.launch(campaign)

        self.assertIn("STOP", Message.objects.get(campaign=campaign).body)

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_a_campaign_can_opt_out_of_the_opt_out(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.4")
        campaign = self._campaign(append_opt_out=False)

        services.launch(campaign)

        self.assertEqual(Message.objects.get(campaign=campaign).body, "Hi Priya!")

    def test_launching_from_a_paused_number_says_so_up_front(self):
        from apps.whatsapp.health import pause_sending

        pause_sending(self.account, "Suspected ban", minutes=None)
        campaign = self._campaign()

        with self.assertRaises(CampaignError) as caught:
            services.launch(campaign)

        self.assertIn("paused", str(caught.exception))

    def test_cannot_relaunch_a_running_campaign(self):
        campaign = self._campaign(status=Campaign.Status.RUNNING)

        with self.assertRaises(CampaignError):
            services.launch(campaign)


class CampaignLifecycleTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-lifecycle", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-lifecycle-owner", password="x", tenant=self.tenant, credits=100)
        self.account = _account(self.tenant, self.user)
        self.campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="C", whatsapp_account=self.account,
            message_body="Hi", status=Campaign.Status.RUNNING,
        )

    def test_pause_then_resume(self):
        services.pause(self.campaign)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.PAUSED)

        services.resume(self.campaign)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.RUNNING)

    def test_cannot_pause_a_draft_campaign(self):
        draft = Campaign.objects.create(tenant=self.tenant, created_by=self.user, name="Draft", whatsapp_account=self.account, message_body="Hi")

        with self.assertRaises(CampaignError):
            services.pause(draft)

    def test_cancel_marks_queued_messages_cancelled(self):
        Message.objects.create(tenant=self.tenant, campaign=self.campaign, whatsapp_account=self.account, to_phone="+1", status=Message.Status.QUEUED)

        services.cancel(self.campaign)

        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.CANCELLED)
        self.assertEqual(Message.objects.get(campaign=self.campaign).status, Message.Status.CANCELLED)

    def test_retry_failed_requeues_failed_messages(self):
        failed = Message.objects.create(
            tenant=self.tenant, campaign=self.campaign, whatsapp_account=self.account, to_phone="+1",
            status=Message.Status.FAILED, error_message="boom", retry_count=3,
        )
        self.campaign.status = Campaign.Status.COMPLETED
        self.campaign.save(update_fields=["status"])

        with patch("apps.messaging.services.ConnectionManager.send_text_message") as mock_send:
            mock_send.return_value = SendResult(success=True, provider_message_id="wamid.3")
            services.retry_failed(self.campaign)

        failed.refresh_from_db()
        self.assertEqual(failed.status, Message.Status.SENT)

    def test_retry_failed_raises_when_nothing_to_retry(self):
        with self.assertRaises(CampaignError):
            services.retry_failed(self.campaign)

    @patch("apps.campaigns.tasks.send_message_task.apply_async")
    def test_dispatch_sends_the_audience_in_order(self, mock_apply):
        # Regression test: Message.Meta.ordering is "-queued_at", so an
        # unordered queryset in dispatch_campaign used to schedule the
        # audience backwards -- the last recipient was messaged first and the
        # first waited out the entire campaign interval.
        self.campaign.status = Campaign.Status.QUEUED
        self.campaign.save(update_fields=["status"])
        expected = [
            Message.objects.create(
                tenant=self.tenant, campaign=self.campaign, whatsapp_account=self.account,
                to_phone=f"+1555000{i}", body=f"message {i}", status=Message.Status.QUEUED,
            ).pk
            for i in range(5)
        ]

        dispatch_campaign(self.campaign.pk)

        dispatched = [call.kwargs["args"][0] for call in mock_apply.call_args_list]
        self.assertEqual(dispatched, expected)
        countdowns = [call.kwargs["countdown"] for call in mock_apply.call_args_list]
        self.assertEqual(countdowns, sorted(countdowns))


class CampaignApiTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Acme", slug="acme-campaign-api", kind=Tenant.Kind.CUSTOMER)
        self.tenant_b = Tenant.objects.create(name="Globex", slug="globex-campaign-api", kind=Tenant.Kind.CUSTOMER)
        self.user_a = get_user_model().objects.create_user(username="acme-campaign-api-owner", password="x", tenant=self.tenant_a)
        self.user_b = get_user_model().objects.create_user(username="globex-campaign-api-owner", password="x", tenant=self.tenant_b)
        self.account_a = _account(self.tenant_a, self.user_a)
        self.account_b = _account(self.tenant_b, self.user_b)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_cannot_target_another_tenants_whatsapp_account(self):
        response = self.client.post(
            "/api/v1/campaigns/campaigns/",
            {"name": "Bad", "whatsapp_account": self.account_b.pk, "message_body": "Hi"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("whatsapp_account", response.json())

    def test_finished_campaign_can_be_deleted_along_with_its_messages(self):
        # Message.campaign is CASCADE, so the per-recipient rows go with the
        # campaign -- which is also what frees a WhatsApp device those
        # messages were blocking from deletion.
        campaign = Campaign.objects.create(
            tenant=self.tenant_a, created_by=self.user_a, name="Done", whatsapp_account=self.account_a,
            message_body="Hi", status=Campaign.Status.COMPLETED,
        )
        Message.objects.create(
            tenant=self.tenant_a, campaign=campaign, whatsapp_account=self.account_a,
            to_phone="+15551230011", status=Message.Status.SENT,
        )

        response = self.client.delete(f"/api/v1/campaigns/campaigns/{campaign.pk}/")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Campaign.objects.filter(pk=campaign.pk).exists())
        self.assertFalse(Message.objects.filter(campaign_id=campaign.pk).exists())

    def test_running_campaign_cannot_be_deleted(self):
        # The devices/campaigns UI only offers Delete on the statuses this
        # allows; the rejection is what that gating mirrors.
        campaign = Campaign.objects.create(
            tenant=self.tenant_a, created_by=self.user_a, name="Live", whatsapp_account=self.account_a,
            message_body="Hi", status=Campaign.Status.RUNNING,
        )

        response = self.client.delete(f"/api/v1/campaigns/campaigns/{campaign.pk}/")

        self.assertEqual(response.status_code, 400)
        self.assertTrue(Campaign.objects.filter(pk=campaign.pk).exists())

    def test_estimate_action_returns_recipient_count(self):
        Contact.objects.create(tenant=self.tenant_a, created_by=self.user_a, name="A", phone="+15551230010")
        create = self.client.post(
            "/api/v1/campaigns/campaigns/",
            {"name": "Promo", "whatsapp_account": self.account_a.pk, "message_body": "Hi"},
            format="json",
        )
        self.assertEqual(create.status_code, 201, create.content)

        response = self.client.get(f"/api/v1/campaigns/campaigns/{create.json()['id']}/estimate/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["estimated_recipients"], 1)

    def test_cannot_edit_a_non_draft_campaign(self):
        campaign = Campaign.objects.create(
            tenant=self.tenant_a, created_by=self.user_a, name="Live", whatsapp_account=self.account_a,
            message_body="Hi", status=Campaign.Status.RUNNING,
        )

        response = self.client.patch(f"/api/v1/campaigns/campaigns/{campaign.pk}/", {"name": "Renamed"}, format="json")

        self.assertEqual(response.status_code, 400)


class CampaignMediaItemsTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-campaign-media", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="acme-campaign-media-owner", password="x", tenant=self.tenant, credits=100
        )
        self.account = _account(self.tenant, self.user)
        Contact.objects.create(tenant=self.tenant, created_by=self.user, name="Priya", phone="+15551239001")

    def test_launch_copies_every_attachment_onto_each_message(self):
        items = [
            {"url": "https://cdn.example.com/a.png", "type": "IMAGE", "filename": "", "caption": ""},
            {"url": "https://cdn.example.com/b.png", "type": "IMAGE", "filename": "", "caption": ""},
        ]
        campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="Gallery", whatsapp_account=self.account,
            message_type=Campaign.MessageType.IMAGE, media_url=items[0]["url"], media_items=items,
            audience_type=Campaign.AudienceType.ALL,
        )

        services.launch(campaign)

        message = Message.objects.get(campaign=campaign)
        self.assertEqual([item["url"] for item in message.media_items], [item["url"] for item in items])

    def test_a_campaign_with_only_attachments_and_no_body_can_launch(self):
        campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="Silent", whatsapp_account=self.account,
            message_type=Campaign.MessageType.IMAGE, message_body="",
            media_items=[{"url": "https://cdn.example.com/a.png", "type": "IMAGE", "filename": "", "caption": ""}],
            audience_type=Campaign.AudienceType.ALL,
        )

        services.launch(campaign)

        self.assertEqual(Message.objects.filter(campaign=campaign).count(), 1)

    def test_a_template_with_attachments_is_carried_through(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant, created_by=self.user, name="Flyer", body="Hi [Name]",
            message_type=MessageTemplate.MessageType.IMAGE, media_url="https://cdn.example.com/t.png",
            media_items=[
                {"url": "https://cdn.example.com/t.png", "type": "IMAGE", "filename": "", "caption": ""},
                {"url": "https://cdn.example.com/t2.png", "type": "IMAGE", "filename": "", "caption": ""},
            ],
        )
        campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="From template", whatsapp_account=self.account,
            template=template, audience_type=Campaign.AudienceType.ALL,
        )

        services.launch(campaign)

        message = Message.objects.get(campaign=campaign)
        self.assertEqual(len(message.media_items), 2)

    def test_a_campaign_with_only_the_old_media_url_still_resolves_one_attachment(self):
        campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="Legacy", whatsapp_account=self.account,
            message_type=Campaign.MessageType.IMAGE, media_url="https://cdn.example.com/legacy.png",
            audience_type=Campaign.AudienceType.ALL,
        )

        spec = campaign.effective_message()

        self.assertEqual([item["url"] for item in spec["media_items"]], ["https://cdn.example.com/legacy.png"])


class CampaignBulkActionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-campaign-bulk", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="acme-campaign-bulk-owner", password="x", tenant=self.tenant, credits=100
        )
        self.account = _account(self.tenant, self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _campaign(self, name, status):
        return Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name=name, whatsapp_account=self.account,
            message_body="Hi", status=status,
        )

    def _bulk(self, action, ids):
        return self.client.post("/api/v1/campaigns/campaigns/bulk/", {"action": action, "ids": ids}, format="json")

    def test_deletes_the_finished_ones_and_reports_the_running_one(self):
        done = self._campaign("Done", Campaign.Status.COMPLETED)
        draft = self._campaign("Draft", Campaign.Status.DRAFT)
        running = self._campaign("Running", Campaign.Status.RUNNING)

        response = self._bulk("delete", [done.pk, draft.pk, running.pk])

        # Partial success is the point: the two deletable campaigns go, and the
        # one the status rules refuse is named rather than silently skipped.
        self.assertEqual(response.status_code, 207, response.content)
        body = response.json()
        self.assertEqual((body["succeeded"], body["failed"]), (2, 1))
        self.assertEqual(body["errors"][0]["id"], running.pk)
        self.assertIn("cancel", body["errors"][0]["detail"].lower())
        self.assertEqual(list(Campaign.objects.values_list("pk", flat=True)), [running.pk])

    def test_refusing_every_campaign_is_a_400(self):
        running = self._campaign("Running", Campaign.Status.RUNNING)

        response = self._bulk("delete", [running.pk])

        self.assertEqual(response.status_code, 400)
        self.assertTrue(Campaign.objects.filter(pk=running.pk).exists())

    def test_bulk_cancel_stops_the_ones_that_are_in_flight(self):
        running = self._campaign("Running", Campaign.Status.RUNNING)

        response = self._bulk("cancel", [running.pk])

        self.assertEqual(response.status_code, 200, response.content)
        running.refresh_from_db()
        self.assertEqual(running.status, Campaign.Status.CANCELLED)

    def test_another_tenants_campaign_is_never_touched(self):
        other_tenant = Tenant.objects.create(name="Globex", slug="globex-campaign-bulk", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(
            username="globex-campaign-bulk-owner", password="x", tenant=other_tenant
        )
        theirs = Campaign.objects.create(
            tenant=other_tenant, created_by=other_user, name="Theirs",
            whatsapp_account=_account(other_tenant, other_user), message_body="Hi",
            status=Campaign.Status.COMPLETED,
        )

        response = self._bulk("delete", [theirs.pk])

        self.assertEqual(response.status_code, 400)
        self.assertTrue(Campaign.objects.filter(pk=theirs.pk).exists())
