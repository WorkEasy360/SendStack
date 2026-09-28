from unittest.mock import ANY, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.campaigns.models import Campaign
from apps.contacts.models import Contact
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount, WhatsAppWebhookEvent
from apps.whatsapp.providers.base import SendResult
from apps.whatsapp.signals import webhook_event_stored

from .models import Message
from .services import attempt_send, refresh_campaign_counters
from .tasks import send_message_task


def _connected_account(tenant, user):
    account = WhatsAppAccount(tenant=tenant, user=user, name="Line", phone_number_id="1", status=WhatsAppAccount.Status.CONNECTED)
    account.set_access_token("token")
    account.set_app_secret("secret")
    account.save()
    return account


class AttemptSendTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-msg", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-msg-owner", password="x", tenant=self.tenant)
        self.account = _connected_account(self.tenant, self.user)
        self.message = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, to_phone="+15551234567", body="Hi!",
        )

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_success_marks_sent(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.1")

        result = attempt_send(self.message)

        self.assertTrue(result.success)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, Message.Status.SENT)
        self.assertEqual(self.message.provider_message_id, "wamid.1")

    def test_returns_none_when_not_queued(self):
        self.message.status = Message.Status.SENT
        self.message.save(update_fields=["status"])

        self.assertIsNone(attempt_send(self.message))


class SendMessageTaskTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-task", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-task-owner", password="x", tenant=self.tenant)
        self.account = _connected_account(self.tenant, self.user)
        self.message = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, to_phone="+15551234567", body="Hi!",
        )

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_failure_with_retries_remaining_requeues_the_message(self, mock_send):
        mock_send.return_value = SendResult(success=False, error="temporary provider error")

        send_message_task.push_request(retries=0)
        try:
            with self.assertRaises(Exception):
                send_message_task.run(self.message.pk)
        finally:
            send_message_task.pop_request()

        self.message.refresh_from_db()
        self.assertEqual(self.message.status, Message.Status.QUEUED)
        self.assertEqual(self.message.retry_count, 1)

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_fails_permanently_once_retries_are_exhausted(self, mock_send):
        mock_send.return_value = SendResult(success=False, error="temporary provider error")

        send_message_task.push_request(retries=send_message_task.max_retries)
        try:
            send_message_task.run(self.message.pk)
        finally:
            send_message_task.pop_request()

        self.message.refresh_from_db()
        self.assertEqual(self.message.status, Message.Status.FAILED)
        self.assertEqual(self.message.error_message, "temporary provider error")

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_ambiguous_failure_becomes_recovery_required_and_is_never_retried(self, mock_send):
        # An ambiguous failure means the provider was reached but never
        # answered, so the message may already be on the recipient's phone.
        # It is NOT a failure and NOT retried (that risks a duplicate): it
        # goes to the explicit RECOVERY_REQUIRED state for a human or the
        # reconciliation sweep to settle.
        mock_send.return_value = SendResult(
            success=False, error="Timed out waiting for the QR provider.", ambiguous=True,
        )

        send_message_task.push_request(retries=0)
        try:
            send_message_task.run(self.message.pk)  # must not raise Retry
        finally:
            send_message_task.pop_request()

        self.message.refresh_from_db()
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(self.message.status, Message.Status.RECOVERY_REQUIRED)
        self.assertEqual(self.message.retry_count, 0)
        self.assertEqual(
            self.message.failure_reason, Message.FailureReason.DELIVERY_UNCONFIRMED,
        )
        self.assertIn("not retried", self.message.error_message)
        self.assertLessEqual(len(self.message.error_message), 500)

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_skips_when_not_queued(self, mock_send):
        self.message.status = Message.Status.CANCELLED
        self.message.save(update_fields=["status"])

        send_message_task.delay(self.message.pk)

        mock_send.assert_not_called()


class CampaignCounterTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-counters", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-counters-owner", password="x", tenant=self.tenant)
        self.account = _connected_account(self.tenant, self.user)
        self.campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="Test", whatsapp_account=self.account,
            message_body="Hi!", status=Campaign.Status.RUNNING, total_recipients=2,
        )

    def test_completes_when_no_pending_messages_remain(self):
        Message.objects.create(tenant=self.tenant, campaign=self.campaign, whatsapp_account=self.account, to_phone="+1", status=Message.Status.SENT)
        Message.objects.create(tenant=self.tenant, campaign=self.campaign, whatsapp_account=self.account, to_phone="+2", status=Message.Status.FAILED)

        refresh_campaign_counters(self.campaign.pk)

        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.sent_count, 1)
        self.assertEqual(self.campaign.failed_count, 1)
        self.assertEqual(self.campaign.status, Campaign.Status.COMPLETED)

    def test_stays_running_while_messages_are_pending(self):
        Message.objects.create(tenant=self.tenant, campaign=self.campaign, whatsapp_account=self.account, to_phone="+1", status=Message.Status.SENT)
        Message.objects.create(tenant=self.tenant, campaign=self.campaign, whatsapp_account=self.account, to_phone="+2", status=Message.Status.QUEUED)

        refresh_campaign_counters(self.campaign.pk)

        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.RUNNING)


class WebhookStatusReceiverTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-webhook-status", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-webhook-status-owner", password="x", tenant=self.tenant)
        self.account = _connected_account(self.tenant, self.user)
        self.message = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, to_phone="+15551234567",
            status=Message.Status.SENT, provider_message_id="wamid.99",
        )

    def _fire(self, status):
        event = WhatsAppWebhookEvent.objects.create(
            account=self.account, event_type=WhatsAppWebhookEvent.EventType.MESSAGE_STATUS,
            provider_event_id=f"wamid.99:{status.lower()}", payload={}, related_message_id="wamid.99", status=status,
        )
        webhook_event_stored.send(sender=WhatsAppWebhookEvent, event=event)
        event.refresh_from_db()
        return event

    def test_advances_status_and_marks_event_processed(self):
        event = self._fire("DELIVERED")

        self.message.refresh_from_db()
        self.assertEqual(self.message.status, Message.Status.DELIVERED)
        self.assertIsNotNone(self.message.delivered_at)
        self.assertIsNotNone(event.processed_at)

    def test_does_not_regress_a_later_status(self):
        self._fire("READ")
        self._fire("DELIVERED")  # arrives late, out of order

        self.message.refresh_from_db()
        self.assertEqual(self.message.status, Message.Status.READ)

    def test_ignores_event_for_unknown_message(self):
        WhatsAppWebhookEvent.objects.create(
            account=self.account, event_type=WhatsAppWebhookEvent.EventType.MESSAGE_STATUS,
            provider_event_id="wamid.missing:sent", payload={}, related_message_id="wamid.missing", status="SENT",
        )
        # Should not raise even though no Message has this provider_message_id.
        event = WhatsAppWebhookEvent.objects.get(provider_event_id="wamid.missing:sent")
        webhook_event_stored.send(sender=WhatsAppWebhookEvent, event=event)


class MultiAttachmentSendTests(TestCase):
    """
    WhatsApp has no multi-file message type, so a message carrying three
    attachments is three provider calls behind one Message row.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-multimedia", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="acme-multimedia-owner", password="x", tenant=self.tenant
        )
        self.account = _connected_account(self.tenant, self.user)

    def _message(self, **overrides):
        defaults = dict(
            tenant=self.tenant, whatsapp_account=self.account, to_phone="+15551234567",
            message_type=Message.MessageType.IMAGE,
        )
        defaults.update(overrides)
        return Message.objects.create(**defaults)

    @patch("apps.messaging.services.ConnectionManager.send_media_message")
    def test_sends_one_provider_call_per_attachment(self, mock_send):
        mock_send.side_effect = [
            SendResult(success=True, provider_message_id="wamid.1"),
            SendResult(success=True, provider_message_id="wamid.2"),
            SendResult(success=True, provider_message_id="wamid.3"),
        ]
        message = self._message(
            media_url="https://cdn.example.com/a.png",
            media_items=[
                {"url": "https://cdn.example.com/a.png", "type": "IMAGE", "filename": "", "caption": ""},
                {"url": "https://cdn.example.com/b.mp4", "type": "VIDEO", "filename": "", "caption": ""},
                {"url": "https://cdn.example.com/c.pdf", "type": "DOCUMENT", "filename": "c.pdf", "caption": ""},
            ],
        )

        result = attempt_send(message)
        message.refresh_from_db()

        self.assertTrue(result.success)
        self.assertEqual(mock_send.call_count, 3)
        self.assertEqual(
            [call.args[3] for call in mock_send.call_args_list],
            ["https://cdn.example.com/a.png", "https://cdn.example.com/b.mp4", "https://cdn.example.com/c.pdf"],
        )
        self.assertEqual([call.args[2] for call in mock_send.call_args_list], ["image", "video", "document"])
        # One row, one report line: the id kept is the first attachment's,
        # which is what delivery/read webhooks arrive against.
        self.assertEqual(message.status, Message.Status.SENT)
        self.assertEqual(message.provider_message_id, "wamid.1")

    @patch("apps.messaging.services.ConnectionManager.send_media_message")
    def test_a_message_level_caption_labels_only_the_first_file(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.1")
        message = self._message(
            caption="Our new range",
            media_items=[
                {"url": "https://cdn.example.com/a.png", "type": "IMAGE", "filename": "", "caption": ""},
                {"url": "https://cdn.example.com/b.png", "type": "IMAGE", "filename": "", "caption": ""},
            ],
        )

        attempt_send(message)

        self.assertEqual([call.args[4] for call in mock_send.call_args_list], ["Our new range", None])

    @patch("apps.messaging.services.ConnectionManager.send_media_message")
    def test_a_per_attachment_caption_wins(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.1")
        message = self._message(
            caption="Fallback",
            media_items=[{"url": "https://cdn.example.com/a.png", "type": "IMAGE", "filename": "", "caption": "Just this one"}],
        )

        attempt_send(message)

        self.assertEqual(mock_send.call_args.args[4], "Just this one")

    @patch("apps.messaging.services.ConnectionManager.send_media_message")
    def test_a_failure_part_way_through_stops_and_says_which_one(self, mock_send):
        mock_send.side_effect = [
            SendResult(success=True, provider_message_id="wamid.1"),
            SendResult(success=False, error="file too large"),
            SendResult(success=True, provider_message_id="wamid.3"),
        ]
        message = self._message(
            media_items=[
                {"url": "https://cdn.example.com/a.png", "type": "IMAGE", "filename": "", "caption": ""},
                {"url": "https://cdn.example.com/b.png", "type": "IMAGE", "filename": "", "caption": ""},
                {"url": "https://cdn.example.com/c.png", "type": "IMAGE", "filename": "", "caption": ""},
            ],
        )

        result = attempt_send(message)
        message.refresh_from_db()

        self.assertFalse(result.success)
        self.assertIn("Attachment 2 of 3", result.error)
        self.assertIn("file too large", result.error)
        self.assertEqual(mock_send.call_count, 2)  # the third is never attempted
        self.assertNotEqual(message.status, Message.Status.SENT)

    @patch("apps.messaging.services.ConnectionManager.send_media_message")
    def test_a_message_with_only_the_old_media_url_sends_exactly_as_before(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.1")
        message = self._message(
            message_type=Message.MessageType.DOCUMENT,
            media_url="https://cdn.example.com/invoice.pdf",
            filename="invoice.pdf",
            caption="Your invoice",
        )

        result = attempt_send(message)

        self.assertTrue(result.success)
        mock_send.assert_called_once_with(
            self.account, "+15551234567", "document", "https://cdn.example.com/invoice.pdf", "Your invoice", "invoice.pdf",
            operation_id=ANY,
        )

    @patch("apps.messaging.services.ConnectionManager.send_media_message")
    def test_a_media_message_with_nothing_attached_fails_without_calling_the_provider(self, mock_send):
        message = self._message(media_url="", media_items=[])

        result = attempt_send(message)

        self.assertFalse(result.success)
        mock_send.assert_not_called()


class ButtonSendTests(TestCase):
    """
    A message carrying buttons is an *interactive* send, not a text one, and
    its attachments still go out first so the buttons land on the last bubble.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-buttons", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="acme-buttons-owner", password="x", tenant=self.tenant
        )
        self.account = _connected_account(self.tenant, self.user)
        self.buttons = [
            {"type": "REPLY", "id": "yes", "title": "Yes", "url": ""},
            {"type": "REPLY", "id": "no", "title": "No", "url": ""},
        ]

    def _message(self, **overrides):
        defaults = dict(
            tenant=self.tenant, whatsapp_account=self.account, to_phone="+15551234567",
            body="Coming on Friday?", buttons=self.buttons,
        )
        defaults.update(overrides)
        return Message.objects.create(**defaults)

    @patch("apps.messaging.services.ConnectionManager.supports_interactive_buttons", return_value=True)
    @patch("apps.messaging.services.ConnectionManager.send_interactive_message")
    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_text_with_buttons_goes_out_as_an_interactive_message(self, mock_text, mock_interactive, _supported):
        mock_interactive.return_value = SendResult(success=True, provider_message_id="wamid.i.1")
        message = self._message()

        result = attempt_send(message)

        self.assertTrue(result.success)
        mock_text.assert_not_called()
        mock_interactive.assert_called_once_with(
            self.account, "+15551234567", "Coming on Friday?", self.buttons, operation_id=ANY,
        )
        message.refresh_from_db()
        self.assertEqual(message.provider_message_id, "wamid.i.1")

    @patch("apps.messaging.services.ConnectionManager.supports_interactive_buttons", return_value=True)
    @patch("apps.messaging.services.ConnectionManager.send_interactive_message")
    @patch("apps.messaging.services.ConnectionManager.send_media_message")
    def test_attachments_are_sent_before_the_buttons(self, mock_media, mock_interactive, _supported):
        mock_media.return_value = SendResult(success=True, provider_message_id="wamid.m.1")
        mock_interactive.return_value = SendResult(success=True, provider_message_id="wamid.i.2")
        message = self._message(
            message_type=Message.MessageType.IMAGE,
            media_items=[{"url": "https://cdn.example.com/a.png", "type": "IMAGE", "filename": "", "caption": ""}],
        )

        result = attempt_send(message)

        self.assertTrue(result.success)
        mock_media.assert_called_once()
        mock_interactive.assert_called_once()
        message.refresh_from_db()
        # The id kept is the first send's -- that is the one this row's
        # delivery and read webhooks arrive against.
        self.assertEqual(message.provider_message_id, "wamid.m.1")

    @patch("apps.messaging.services.ConnectionManager.supports_interactive_buttons", return_value=True)
    @patch("apps.messaging.services.ConnectionManager.send_interactive_message")
    @patch("apps.messaging.services.ConnectionManager.send_media_message")
    def test_a_failed_interactive_send_after_attachments_went_out_says_so(self, mock_media, mock_interactive, _supported):
        mock_media.return_value = SendResult(success=True, provider_message_id="wamid.m.2")
        mock_interactive.return_value = SendResult(success=False, error="Body too long")
        message = self._message(
            message_type=Message.MessageType.IMAGE,
            media_items=[{"url": "https://cdn.example.com/a.png", "type": "IMAGE", "filename": "", "caption": ""}],
        )

        result = attempt_send(message)

        self.assertFalse(result.success)
        self.assertIn("attachments were sent", result.error)
        self.assertIn("Body too long", result.error)

    @patch("apps.messaging.services.ConnectionManager.supports_interactive_buttons", return_value=False)
    @patch("apps.messaging.services.ConnectionManager.send_interactive_message")
    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_a_connection_without_interactive_support_sends_a_numbered_list(self, mock_text, mock_interactive, _unsupported):
        mock_text.return_value = SendResult(success=True, provider_message_id="wamid.t.1")

        result = attempt_send(self._message())

        self.assertTrue(result.success)
        mock_interactive.assert_not_called()
        # The instruction line is part of the message: a bare list leaves the
        # recipient guessing whether to tap, type the number or type the word,
        # and only two of those can be resolved back to a button.
        mock_text.assert_called_once_with(
            self.account, "+15551234567",
            "Coming on Friday?\n\n1. Yes\n2. No\n\nPlease reply with the option number.",
            operation_id=ANY,
        )

    @patch("apps.messaging.services.ConnectionManager.supports_interactive_buttons", return_value=True)
    @patch("apps.messaging.services.ConnectionManager.send_interactive_message")
    def test_buttons_without_body_text_fail_without_calling_the_provider(self, mock_interactive, _supported):
        result = attempt_send(self._message(body="", caption=""))

        self.assertFalse(result.success)
        self.assertIn("needs body text", result.error)
        mock_interactive.assert_not_called()
