import json
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.contacts.models import Contact
from apps.inbox.models import Conversation
from apps.inbox.signals import inbound_message_recorded
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount

from . import events


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
        cache.clear()
        events.reset_client()
        self.tenant = Tenant.objects.create(name="Acme", slug=f"rt-{self._testMethodName}"[:50], kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username=f"rt-{self._testMethodName}"[:140], password="x", tenant=self.tenant, credits=100,
        )
        self.account = _account(self.tenant, self.user)
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Priya", phone="+15557770001",
        )
        self.conversation = Conversation.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)


class PublishTests(Fixture):
    def test_channels_are_per_tenant(self):
        self.assertNotEqual(events.channel(1), events.channel(2))
        self.assertIn("1", events.channel(1))

    @patch("apps.realtime.events._redis")
    def test_publish_serialises_the_event(self, mock_redis):
        client = MagicMock()
        mock_redis.return_value = client

        self.assertTrue(events.publish(self.tenant.pk, "message.received", {"id": 7}))

        channel, payload = client.publish.call_args[0]
        self.assertEqual(channel, events.channel(self.tenant.pk))
        self.assertEqual(json.loads(payload), {"type": "message.received", "data": {"id": 7}})

    @patch("apps.realtime.events._redis", return_value=None)
    def test_publish_is_silent_when_redis_is_unavailable(self, _mock_redis):
        """A live update is a convenience; losing it must cost nothing."""
        self.assertFalse(events.publish(self.tenant.pk, "message.received", {"id": 1}))

    @patch("apps.realtime.events._redis")
    def test_a_broken_redis_never_raises_into_the_caller(self, mock_redis):
        client = MagicMock()
        client.publish.side_effect = RuntimeError("connection reset")
        mock_redis.return_value = client

        self.assertFalse(events.publish(self.tenant.pk, "message.received", {"id": 1}))

    def test_publishing_without_a_tenant_does_nothing(self):
        self.assertFalse(events.publish(None, "message.received", {}))


class ReceiverTests(Fixture):
    @patch("apps.realtime.receivers.events.publish")
    def test_an_inbound_message_is_announced(self, mock_publish):
        message = Message.objects.create(
            tenant=self.tenant, conversation=self.conversation, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, direction=Message.Direction.INBOUND, body="Hello",
            status=Message.Status.DELIVERED,
        )
        mock_publish.reset_mock()

        inbound_message_recorded.send(sender=Message, message=message, is_new_conversation=True)

        published = [call.args[1] for call in mock_publish.call_args_list]
        self.assertIn("message.received", published)

    @patch("apps.realtime.receivers.events.publish")
    def test_an_outbound_message_is_announced_once_created(self, mock_publish):
        Message.objects.create(
            tenant=self.tenant, conversation=self.conversation, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, direction=Message.Direction.OUTBOUND, body="Hi",
            status=Message.Status.QUEUED,
        )

        published = [call.args[1] for call in mock_publish.call_args_list]
        self.assertIn("message.sent", published)

    @patch("apps.realtime.receivers.events.publish")
    def test_a_status_change_is_announced_as_an_update(self, mock_publish):
        message = Message.objects.create(
            tenant=self.tenant, conversation=self.conversation, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, direction=Message.Direction.OUTBOUND, body="Hi",
            status=Message.Status.QUEUED,
        )
        mock_publish.reset_mock()

        message.status = Message.Status.DELIVERED
        message.save(update_fields=["status"])

        published = [call.args[1] for call in mock_publish.call_args_list]
        self.assertIn("message.updated", published)

    @patch("apps.realtime.receivers.events.publish")
    def test_a_message_with_no_conversation_is_not_announced(self, mock_publish):
        """A campaign send belongs to no thread; announcing thousands of them would flood every open inbox."""
        Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, direction=Message.Direction.OUTBOUND, body="Campaign",
            status=Message.Status.QUEUED,
        )

        published = [call.args[1] for call in mock_publish.call_args_list]
        self.assertNotIn("message.sent", published)

    @patch("apps.realtime.receivers.events.publish")
    def test_assigning_a_conversation_is_announced(self, mock_publish):
        self.conversation.assigned_agent = self.user
        self.conversation.save(update_fields=["assigned_agent"])

        published = [call.args[1] for call in mock_publish.call_args_list]
        self.assertIn("conversation.updated", published)


class TicketTests(Fixture):
    def test_a_ticket_is_issued_to_an_authenticated_user(self):
        response = self.client.post("/api/v1/realtime/ticket/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["ticket"])

    def test_a_ticket_is_required_to_open_the_stream(self):
        response = self.client.get("/api/v1/realtime/stream/")

        self.assertEqual(response.status_code, 401)

    def test_an_unknown_ticket_is_refused(self):
        response = self.client.get("/api/v1/realtime/stream/?ticket=made-up")

        self.assertEqual(response.status_code, 401)

    @patch("apps.realtime.views.events.subscribe", return_value=None)
    def test_a_ticket_cannot_be_used_twice(self, _mock_subscribe):
        ticket = self.client.post("/api/v1/realtime/ticket/").data["ticket"]

        first = self.client.get(f"/api/v1/realtime/stream/?ticket={ticket}")
        b"".join(first.streaming_content)
        second = self.client.get(f"/api/v1/realtime/stream/?ticket={ticket}")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 401)

    @patch("apps.realtime.views.events.subscribe", return_value=None)
    def test_the_stream_says_so_when_live_updates_are_unavailable(self, _mock_subscribe):
        """Silence would leave the client waiting forever instead of falling back to polling."""
        ticket = self.client.post("/api/v1/realtime/ticket/").data["ticket"]

        response = self.client.get(f"/api/v1/realtime/stream/?ticket={ticket}")
        body = b"".join(response.streaming_content).decode()

        self.assertEqual(response["Content-Type"], "text/event-stream")
        self.assertIn('"type": "connected"', body)
        self.assertIn('"type": "unavailable"', body)

    def test_anonymous_callers_get_no_ticket(self):
        response = APIClient().post("/api/v1/realtime/ticket/")

        self.assertIn(response.status_code, (401, 403))


class TypingAndPresenceTests(Fixture):
    @patch("apps.realtime.views.events.publish")
    def test_typing_is_published_and_never_stored(self, mock_publish):
        response = self.client.post(
            "/api/v1/realtime/typing/", {"conversation": self.conversation.pk}, format="json",
        )

        self.assertEqual(response.status_code, 200)
        tenant_id, event_type, data = mock_publish.call_args[0]
        self.assertEqual(event_type, "typing")
        self.assertEqual(data["conversation"], self.conversation.pk)
        self.assertEqual(data["user"], self.user.pk)

    def test_typing_needs_a_conversation(self):
        response = self.client.post("/api/v1/realtime/typing/", {}, format="json")

        self.assertEqual(response.status_code, 400)

    @patch("apps.realtime.events._redis")
    def test_presence_lists_who_is_online(self, mock_redis):
        client = MagicMock()
        client.scan_iter.return_value = [f"sendstack:presence:{self.tenant.pk}:{self.user.pk}".encode()]
        mock_redis.return_value = client

        response = self.client.get("/api/v1/realtime/presence/")

        self.assertEqual(response.data["online_user_ids"], [self.user.pk])

    @patch("apps.realtime.events._redis", return_value=None)
    def test_presence_is_empty_rather_than_broken_without_redis(self, _mock_redis):
        response = self.client.get("/api/v1/realtime/presence/")

        self.assertEqual(response.data["online_user_ids"], [])

    @patch("apps.realtime.views.events.publish", return_value=False)
    def test_status_tells_the_client_to_poll_instead(self, _mock_publish):
        response = self.client.get("/api/v1/realtime/status/")

        self.assertFalse(response.data["available"])
        self.assertEqual(response.data["poll_interval_seconds"], 15)
