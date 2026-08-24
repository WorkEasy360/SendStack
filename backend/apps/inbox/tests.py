from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.contacts.models import Contact
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount, WhatsAppWebhookEvent
from apps.whatsapp.providers.base import SendResult
from apps.whatsapp.providers.qr_bridge import WhatsAppQRBridgeProvider
from apps.whatsapp.signals import webhook_event_stored

from . import services
from .models import Conversation, ConversationLabel
from .services import InboxError


def _connected_account(tenant, user):
    account = WhatsAppAccount(tenant=tenant, user=user, name="Line", phone_number_id="1", status=WhatsAppAccount.Status.CONNECTED)
    account.set_access_token("token")
    account.set_app_secret("secret")
    account.save()
    return account


def _inbound_event(account, *, from_number="15551234567", body="Hi there", message_id="wamid.in.1", profile_name="Priya"):
    return WhatsAppWebhookEvent.objects.create(
        account=account, event_type=WhatsAppWebhookEvent.EventType.INBOUND_MESSAGE, provider_event_id=message_id,
        payload={"message": {"id": message_id, "from": from_number, "type": "text", "text": {"body": body}}, "profile_name": profile_name},
        related_message_id=message_id,
    )


class RecordInboundMessageTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-inbox", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-inbox-owner", password="x", tenant=self.tenant)
        self.account = _connected_account(self.tenant, self.user)

    def test_creates_contact_conversation_and_message(self):
        event = _inbound_event(self.account)

        message = services.record_inbound_message(event)

        self.assertEqual(message.direction, Message.Direction.INBOUND)
        self.assertEqual(message.body, "Hi there")
        self.assertEqual(message.status, Message.Status.DELIVERED)

        contact = Contact.objects.get(tenant=self.tenant, phone="+15551234567")
        self.assertEqual(contact.name, "Priya")

        conversation = Conversation.objects.get(whatsapp_account=self.account, contact=contact)
        self.assertEqual(conversation.unread_count, 1)
        self.assertIsNotNone(conversation.last_message_at)

    def test_second_message_reuses_conversation_and_increments_unread(self):
        services.record_inbound_message(_inbound_event(self.account, message_id="wamid.in.1"))
        services.record_inbound_message(_inbound_event(self.account, message_id="wamid.in.2", body="Second message"))

        self.assertEqual(Conversation.objects.filter(whatsapp_account=self.account).count(), 1)
        conversation = Conversation.objects.get(whatsapp_account=self.account)
        self.assertEqual(conversation.unread_count, 2)
        self.assertEqual(conversation.messages.count(), 2)

    def test_media_message_uses_caption_as_body(self):
        event = WhatsAppWebhookEvent.objects.create(
            account=self.account, event_type=WhatsAppWebhookEvent.EventType.INBOUND_MESSAGE, provider_event_id="wamid.in.3",
            payload={"message": {"id": "wamid.in.3", "from": "15551234567", "type": "image", "image": {"caption": "Look at this"}}, "profile_name": ""},
            related_message_id="wamid.in.3",
        )

        message = services.record_inbound_message(event)

        self.assertEqual(message.message_type, Message.MessageType.IMAGE)
        self.assertEqual(message.body, "Look at this")


class SignalIntegrationTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-inbox-signal", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-inbox-signal-owner", password="x", tenant=self.tenant)
        self.account = _connected_account(self.tenant, self.user)

    def test_webhook_event_stored_signal_creates_conversation(self):
        event = _inbound_event(self.account)

        webhook_event_stored.send(sender=WhatsAppWebhookEvent, event=event)

        event.refresh_from_db()
        self.assertIsNotNone(event.processed_at)
        self.assertTrue(Conversation.objects.filter(whatsapp_account=self.account).exists())


class QRBridgeIngestionTests(TestCase):
    """
    End-to-end over the app boundary the inbound pipeline actually broke on:
    a payload shaped the way the QR bridge really posts it has to survive
    provider parsing and land as a Conversation. Both apps looked correct in
    isolation; only the seam between them was wrong.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-inbox-bridge", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-inbox-bridge-owner", password="x", tenant=self.tenant)
        self.account = _connected_account(self.tenant, self.user)
        self.provider = WhatsAppQRBridgeProvider(
            base_url="https://bridge.test", api_key="test-key", webhook_secret="test-webhook-secret",
        )

    def _ingest(self, bridge_message):
        parsed = self.provider.parse_webhook_payload({"messages": [bridge_message]})[0]
        event = WhatsAppWebhookEvent.objects.create(
            account=self.account, event_type=WhatsAppWebhookEvent.EventType.INBOUND_MESSAGE,
            provider_event_id=parsed.provider_event_id, payload=parsed.raw,
        )
        return services.record_inbound_message(event)

    def test_bridge_text_message_becomes_a_conversation(self):
        message = self._ingest(
            {"id": "3EB0", "from": "15551234567", "type": "text", "body": "Hello", "push_name": "Priya"}
        )

        self.assertIsNotNone(message)
        self.assertEqual(message.body, "Hello")
        self.assertEqual(message.direction, Message.Direction.INBOUND)
        self.assertEqual(message.message_type, Message.MessageType.TEXT)
        self.assertEqual(message.contact.phone, "+15551234567")
        self.assertEqual(message.contact.name, "Priya")
        self.assertTrue(message.is_new_conversation)
        # unread_count is bumped with a queryset update, so re-read it.
        self.assertEqual(Conversation.objects.get(pk=message.conversation_id).unread_count, 1)

    def test_bridge_media_message_keeps_its_caption(self):
        message = self._ingest({"id": "3EB1", "from": "15551234567", "type": "image", "body": "my receipt"})

        self.assertEqual(message.message_type, Message.MessageType.IMAGE)
        self.assertEqual(message.body, "my receipt")


class ConversationServiceTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-inbox-svc", kind=Tenant.Kind.CUSTOMER)
        self.other_tenant = Tenant.objects.create(name="Globex", slug="globex-inbox-svc", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-inbox-svc-owner", password="x", tenant=self.tenant, credits=10)
        self.outsider = get_user_model().objects.create_user(username="globex-inbox-svc-owner", password="x", tenant=self.other_tenant)
        self.account = _connected_account(self.tenant, self.user)
        self.message = services.record_inbound_message(_inbound_event(self.account))
        self.conversation = Conversation.objects.get(whatsapp_account=self.account)

    def test_mark_read_clears_unread_and_flips_message_status(self):
        services.mark_read(self.conversation)

        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.unread_count, 0)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, Message.Status.READ)

    def test_assign_agent_rejects_agent_from_another_tenant(self):
        with self.assertRaises(InboxError):
            services.assign_agent(self.conversation, self.outsider)

    def test_close_and_reopen(self):
        services.close(self.conversation)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.status, Conversation.Status.CLOSED)

        services.reopen(self.conversation)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.status, Conversation.Status.OPEN)

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_reply_reserves_credit_and_sends(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.out.1")

        message = services.send_reply(self.conversation, self.user, body="Thanks for reaching out!")

        self.assertEqual(message.direction, Message.Direction.OUTBOUND)
        self.assertEqual(message.billed_to_id, self.user.pk)
        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.SENT)
        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 9)

    def test_reply_fails_without_enough_credits(self):
        self.user.credits = 0
        self.user.save(update_fields=["credits"])

        with self.assertRaises(InboxError):
            services.send_reply(self.conversation, self.user, body="Hi")

    def test_reply_rejects_disconnected_account(self):
        self.account.status = WhatsAppAccount.Status.DISCONNECTED
        self.account.save(update_fields=["status"])

        with self.assertRaises(InboxError):
            services.send_reply(self.conversation, self.user, body="Hi")


class ConversationApiTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Acme", slug="acme-inbox-api", kind=Tenant.Kind.CUSTOMER)
        self.tenant_b = Tenant.objects.create(name="Globex", slug="globex-inbox-api", kind=Tenant.Kind.CUSTOMER)
        self.user_a = get_user_model().objects.create_user(username="acme-inbox-api-owner", password="x", tenant=self.tenant_a, credits=10)
        self.user_b = get_user_model().objects.create_user(username="globex-inbox-api-owner", password="x", tenant=self.tenant_b)
        self.account_a = _connected_account(self.tenant_a, self.user_a)
        self.account_b = _connected_account(self.tenant_b, self.user_b)
        services.record_inbound_message(_inbound_event(self.account_a, message_id="wamid.a"))
        services.record_inbound_message(_inbound_event(self.account_b, message_id="wamid.b", from_number="15559990000"))
        self.conversation_a = Conversation.objects.get(whatsapp_account=self.account_a)
        self.conversation_b = Conversation.objects.get(whatsapp_account=self.account_b)

        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_list_only_returns_own_tenant_conversations(self):
        response = self.client.get("/api/v1/inbox/conversations/")

        ids = {c["id"] for c in response.json()["results"]}
        self.assertEqual(ids, {self.conversation_a.pk})

    def test_cannot_access_another_tenants_conversation(self):
        response = self.client.get(f"/api/v1/inbox/conversations/{self.conversation_b.pk}/")

        self.assertEqual(response.status_code, 404)

    def test_mark_read_endpoint(self):
        response = self.client.post(f"/api/v1/inbox/conversations/{self.conversation_a.pk}/mark-read/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["unread_count"], 0)

    def test_notes_create_and_list(self):
        create = self.client.post(f"/api/v1/inbox/conversations/{self.conversation_a.pk}/notes/", {"body": "Called them back"}, format="json")
        self.assertEqual(create.status_code, 201, create.content)

        listed = self.client.get(f"/api/v1/inbox/conversations/{self.conversation_a.pk}/notes/")
        self.assertEqual(len(listed.json()), 1)
        self.assertEqual(listed.json()[0]["body"], "Called them back")

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_reply_endpoint(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.out.2")

        response = self.client.post(f"/api/v1/inbox/conversations/{self.conversation_a.pk}/reply/", {"body": "Hello!"}, format="json")

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["status"], "SENT")

    def test_delete_removes_conversation_and_its_messages(self):
        response = self.client.delete(f"/api/v1/inbox/conversations/{self.conversation_a.pk}/")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Conversation.objects.filter(pk=self.conversation_a.pk).exists())
        self.assertFalse(Message.objects.filter(conversation_id=self.conversation_a.pk).exists())
        # The contact outlives the thread, so a new inbound message starts a fresh one.
        self.assertTrue(Contact.objects.filter(tenant=self.tenant_a, phone="+15551234567").exists())

    def test_cannot_delete_another_tenants_conversation(self):
        response = self.client.delete(f"/api/v1/inbox/conversations/{self.conversation_b.pk}/")

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Conversation.objects.filter(pk=self.conversation_b.pk).exists())

    def test_labels_are_tenant_scoped(self):
        foreign_label = ConversationLabel.objects.create(tenant=self.tenant_b, name="VIP")

        response = self.client.patch(
            f"/api/v1/inbox/conversations/{self.conversation_a.pk}/", {"labels": [foreign_label.pk]}, format="json",
        )

        self.assertEqual(response.status_code, 400)


class ReplyWithSeveralAttachmentsTests(TestCase):
    """Composing a reply with more than one file, and bulk thread actions."""

    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Acme", slug="acme-inbox-multi", kind=Tenant.Kind.CUSTOMER)
        self.tenant_b = Tenant.objects.create(name="Globex", slug="globex-inbox-multi", kind=Tenant.Kind.CUSTOMER)
        self.user_a = get_user_model().objects.create_user(
            username="acme-inbox-multi-owner", password="x", tenant=self.tenant_a, credits=10
        )
        self.user_b = get_user_model().objects.create_user(
            username="globex-inbox-multi-owner", password="x", tenant=self.tenant_b
        )
        self.account_a = _connected_account(self.tenant_a, self.user_a)
        self.account_b = _connected_account(self.tenant_b, self.user_b)
        services.record_inbound_message(_inbound_event(self.account_a, message_id="wamid.multi.a"))
        services.record_inbound_message(
            _inbound_event(self.account_b, message_id="wamid.multi.b", from_number="15559990000")
        )
        self.conversation_a = Conversation.objects.get(whatsapp_account=self.account_a)
        self.conversation_b = Conversation.objects.get(whatsapp_account=self.account_b)

        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    @patch("apps.messaging.services.ConnectionManager.send_media_message")
    def test_reply_accepts_several_attachments(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.multi")

        response = self.client.post(
            f"/api/v1/inbox/conversations/{self.conversation_a.pk}/reply/",
            {
                "media_items": [
                    {"url": "https://cdn.example.com/a.png", "type": "IMAGE"},
                    {"url": "https://cdn.example.com/b.pdf", "type": "DOCUMENT", "filename": "b.pdf"},
                ],
                "body": "",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(len(body["media_items"]), 2)
        # The first attachment still describes the message for every older reader.
        self.assertEqual(body["message_type"], "IMAGE")
        self.assertEqual(body["media_url"], "https://cdn.example.com/a.png")
        self.assertEqual(mock_send.call_count, 2)

    def test_a_reply_with_neither_text_nor_attachments_is_refused(self):
        response = self.client.post(
            f"/api/v1/inbox/conversations/{self.conversation_a.pk}/reply/", {"body": ""}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_bulk_close_and_reopen(self):
        close = self.client.post(
            "/api/v1/inbox/conversations/bulk/",
            {"action": "close", "ids": [self.conversation_a.pk]},
            format="json",
        )

        self.assertEqual(close.status_code, 200, close.content)
        self.conversation_a.refresh_from_db()
        self.assertEqual(self.conversation_a.status, Conversation.Status.CLOSED)

        self.client.post(
            "/api/v1/inbox/conversations/bulk/",
            {"action": "reopen", "ids": [self.conversation_a.pk]},
            format="json",
        )
        self.conversation_a.refresh_from_db()
        self.assertEqual(self.conversation_a.status, Conversation.Status.OPEN)

    def test_bulk_cannot_reach_another_tenants_conversation(self):
        response = self.client.post(
            "/api/v1/inbox/conversations/bulk/",
            {"action": "delete", "ids": [self.conversation_b.pk]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertTrue(Conversation.objects.filter(pk=self.conversation_b.pk).exists())


class ButtonReplyIngestionTests(TestCase):
    """
    Three provider shapes mean "the contact tapped an option". All three land
    as one inbound message whose body is the label they saw and whose
    replied_button_id is what automation matches on.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-inbox-buttons", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="acme-inbox-buttons-owner", password="x", tenant=self.tenant
        )
        self.account = _connected_account(self.tenant, self.user)

    def _record(self, message_obj):
        event = WhatsAppWebhookEvent.objects.create(
            account=self.account, event_type=WhatsAppWebhookEvent.EventType.INBOUND_MESSAGE,
            provider_event_id=message_obj["id"],
            payload={"message": message_obj, "profile_name": "Priya"},
            related_message_id=message_obj["id"],
        )
        return services.record_inbound_message(event)

    def test_an_interactive_button_reply_records_the_button_id_and_its_label(self):
        message = self._record({
            "id": "wamid.b.1", "from": "15551234567", "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": "rsvp_yes", "title": "Yes"}},
        })

        self.assertEqual(message.replied_button_id, "rsvp_yes")
        self.assertEqual(message.body, "Yes")
        self.assertEqual(message.message_type, Message.MessageType.TEXT)

    def test_a_list_reply_is_recorded_the_same_way(self):
        message = self._record({
            "id": "wamid.b.2", "from": "15551234567", "type": "interactive",
            "interactive": {"type": "list_reply", "list_reply": {"id": "plan_pro", "title": "Pro plan"}},
        })

        self.assertEqual(message.replied_button_id, "plan_pro")
        self.assertEqual(message.body, "Pro plan")

    def test_a_message_template_quick_reply_uses_its_payload(self):
        message = self._record({
            "id": "wamid.b.3", "from": "15551234567", "type": "button",
            "button": {"payload": "stop_promos", "text": "Stop"},
        })

        self.assertEqual(message.replied_button_id, "stop_promos")
        self.assertEqual(message.body, "Stop")

    def test_plain_text_still_records_no_button(self):
        message = services.record_inbound_message(_inbound_event(self.account, message_id="wamid.b.4"))

        self.assertEqual(message.replied_button_id, "")
