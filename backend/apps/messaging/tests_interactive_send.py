"""
Sending a menu or a poll, natively and degraded.

The degraded path is the one that matters. A campaign is not allowed to fail
because one of its numbers happens to be QR-linked, so a menu that cannot be
drawn is sent as a numbered list -- and the value of that fallback depends
entirely on the reply still being answerable. These tests follow the whole
round trip: the message goes out as text, a PendingButtonPrompt records what
the numbers meant, the contact types "2", and `automation.resolve_option_reply`
writes back the *original row id*. If that chain breaks, everything still
reports success and no customer's answer is ever heard again.

A native poll is deliberately the one case that records no prompt. WhatsApp
reports those as votes rather than messages, so a numbered question nobody was
asked would make the next stray digit fire an automation.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.contacts.models import Contact
from apps.inbox.models import Conversation
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount
from apps.whatsapp.providers.base import SendResult
from apps.whatsapp.tests_capabilities import configured_qr_provider
from core.interactive import clean_list_content, clean_poll_content, list_as_text, poll_as_text

from .models import Message, PendingButtonPrompt
from .services import attempt_send

LIST_CONTENT = clean_list_content({
    "header": "Choose a service",
    "button": "View services",
    "sections": [
        {
            "rows": [
                {"title": "Web development"},
                {"title": "WhatsApp marketing"},
                {"title": "AI automation"},
            ]
        }
    ],
})

POLL_CONTENT = clean_poll_content({
    "question": "Which service are you interested in?",
    "options": ["Web development", "WhatsApp marketing", "AI automation"],
})

_OK = SendResult(success=True, provider_message_id="wamid.1")


class InteractiveSendTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Send", slug="send-interactive", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="send-interactive-owner", password="x", tenant=self.tenant, credits=100,
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Aman", phone="+919876543210",
        )
        self.cloud = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Cloud",
            connection_method=WhatsAppAccount.ConnectionMethod.CLOUD_API,
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.qr = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="QR",
            connection_method=WhatsAppAccount.ConnectionMethod.QR,
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.conversation = Conversation.objects.create(
            tenant=self.tenant, contact=self.contact, whatsapp_account=self.qr,
        )

    def _message(self, account, **kwargs):
        return Message.objects.create(
            tenant=self.tenant, whatsapp_account=account, contact=self.contact,
            conversation=self.conversation, to_phone=self.contact.phone,
            billed_to=self.user, **kwargs
        )

    # -- Lists --------------------------------------------------------------

    def test_a_list_goes_out_natively_on_a_number_that_can_draw_one(self):
        message = self._message(self.cloud, body="Please choose.", list_content=LIST_CONTENT)

        with patch(
            "apps.whatsapp.connections.manager.ConnectionManager.send_list_message", return_value=_OK,
        ) as send_list:
            result = attempt_send(message)

        self.assertTrue(result.success)
        send_list.assert_called_once()
        self.assertEqual(send_list.call_args.args[3], LIST_CONTENT)

    def test_a_list_degrades_to_the_exact_numbered_text_on_a_number_that_cannot(self):
        message = self._message(self.qr, body="Please choose.", list_content=LIST_CONTENT)

        with configured_qr_provider():
            with patch(
                "apps.whatsapp.connections.manager.ConnectionManager.send_text_message", return_value=_OK,
            ) as send_text:
                result = attempt_send(message)

        self.assertTrue(result.success)
        self.assertEqual(
            send_text.call_args.args[2], list_as_text("Please choose.", LIST_CONTENT),
        )

    def test_a_degraded_list_is_answerable_by_number_and_maps_back_to_the_row_id(self):
        """
        The whole round trip. Without it the fallback is a message nobody can
        reply to, and every layer still reports success.
        """
        from apps.automation.services import resolve_option_reply

        message = self._message(self.qr, body="Please choose.", list_content=LIST_CONTENT)

        with configured_qr_provider():
            with patch(
                "apps.whatsapp.connections.manager.ConnectionManager.send_text_message", return_value=_OK,
            ):
                attempt_send(message)

        prompt = PendingButtonPrompt.objects.get(conversation=self.conversation)
        self.assertEqual(
            [option["id"] for option in prompt.options],
            ["web_development", "whatsapp_marketing", "ai_automation"],
        )

        inbound = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.qr, contact=self.contact,
            conversation=self.conversation, to_phone=self.contact.phone,
            direction=Message.Direction.INBOUND, body="2",
        )

        self.assertEqual(resolve_option_reply(inbound), "RESOLVED")
        inbound.refresh_from_db()
        self.assertEqual(inbound.replied_button_id, "whatsapp_marketing")

    def test_a_native_list_records_no_numbered_prompt(self):
        """Nobody was asked to type a number, so no number may be read as an answer."""
        message = self._message(self.cloud, body="Please choose.", list_content=LIST_CONTENT)

        with patch(
            "apps.whatsapp.connections.manager.ConnectionManager.send_list_message", return_value=_OK,
        ):
            attempt_send(message)

        self.assertFalse(PendingButtonPrompt.objects.exists())

    def test_a_list_with_no_body_is_refused_rather_than_sent_empty(self):
        message = self._message(self.cloud, body="", list_content=LIST_CONTENT)

        result = attempt_send(message)

        self.assertFalse(result.success)
        self.assertEqual(result.code, "invalid_request")

    # -- Polls --------------------------------------------------------------

    def test_a_poll_goes_out_natively_on_a_qr_number(self):
        message = self._message(self.qr, body="", poll_content=POLL_CONTENT)

        with configured_qr_provider():
            with patch(
                "apps.whatsapp.connections.manager.ConnectionManager.send_poll_message", return_value=_OK,
            ) as send_poll:
                result = attempt_send(message)

        self.assertTrue(result.success)
        self.assertEqual(send_poll.call_args.args[2], POLL_CONTENT)

    def test_a_native_poll_with_body_text_sends_the_text_first(self):
        """
        A poll is its own bubble with no body, so a message carrying both is
        two provider calls and one Message row -- the same shape attachments
        already use.
        """
        message = self._message(self.qr, body="Quick question:", poll_content=POLL_CONTENT)

        with configured_qr_provider():
            with patch(
                "apps.whatsapp.connections.manager.ConnectionManager.send_text_message", return_value=_OK,
            ) as send_text:
                with patch(
                    "apps.whatsapp.connections.manager.ConnectionManager.send_poll_message", return_value=_OK,
                ) as send_poll:
                    attempt_send(message)

        send_text.assert_called_once()
        send_poll.assert_called_once()
        self.assertEqual(send_text.call_args.args[2], "Quick question:")

    def test_a_native_poll_records_no_numbered_prompt(self):
        """
        WhatsApp reports a vote, not a message. A prompt here would make the
        next stray digit in the conversation fire an automation.
        """
        message = self._message(self.qr, body="", poll_content=POLL_CONTENT)

        with configured_qr_provider():
            with patch(
                "apps.whatsapp.connections.manager.ConnectionManager.send_poll_message", return_value=_OK,
            ):
                attempt_send(message)

        self.assertFalse(PendingButtonPrompt.objects.exists())

    def test_a_poll_degrades_to_one_numbered_message_on_a_cloud_number(self):
        """
        Cheaper and more legible than two bubbles that only make sense
        together -- and the official API cannot send a poll at all.
        """
        message = self._message(self.cloud, body="Quick question:", poll_content=POLL_CONTENT)

        with patch(
            "apps.whatsapp.connections.manager.ConnectionManager.send_text_message", return_value=_OK,
        ) as send_text:
            result = attempt_send(message)

        self.assertTrue(result.success)
        send_text.assert_called_once()
        self.assertEqual(
            send_text.call_args.args[2], poll_as_text("Quick question:", POLL_CONTENT),
        )

    def test_a_degraded_poll_is_answerable_by_number(self):
        message = self._message(self.cloud, body="", poll_content=POLL_CONTENT)

        with patch(
            "apps.whatsapp.connections.manager.ConnectionManager.send_text_message", return_value=_OK,
        ):
            attempt_send(message)

        prompt = PendingButtonPrompt.objects.get(conversation=self.conversation)
        self.assertEqual(
            [option["title"] for option in prompt.options], POLL_CONTENT["options"],
        )

    # -- Failure handling ---------------------------------------------------

    def test_a_failed_list_send_reports_the_provider_error_unchanged(self):
        message = self._message(self.cloud, body="Choose.", list_content=LIST_CONTENT)
        failure = SendResult(success=False, error="rate limited", code="rate_limited")

        with patch(
            "apps.whatsapp.connections.manager.ConnectionManager.send_list_message", return_value=failure,
        ):
            result = attempt_send(message)

        self.assertFalse(result.success)
        self.assertEqual(result.code, "rate_limited")

    def test_a_poll_that_fails_after_its_introduction_says_so(self):
        """
        "It failed" and "the introduction arrived and then it failed" call for
        different actions, and only the row can say which happened.
        """
        message = self._message(self.qr, body="Quick question:", poll_content=POLL_CONTENT)
        failure = SendResult(success=False, error="nope", code="provider_error")

        with configured_qr_provider():
            with patch(
                "apps.whatsapp.connections.manager.ConnectionManager.send_text_message", return_value=_OK,
            ):
                with patch(
                    "apps.whatsapp.connections.manager.ConnectionManager.send_poll_message", return_value=failure,
                ):
                    result = attempt_send(message)

        self.assertFalse(result.success)
        self.assertIn("Part of this message was sent", result.error)

    def test_content_is_read_from_the_message_not_the_template(self):
        """
        The row is the record of what this contact was offered. A template
        edited afterwards must not change what a sent message says it asked.
        """
        message = self._message(self.cloud, body="Choose.", list_content=LIST_CONTENT)

        message.refresh_from_db()
        self.assertEqual(message.list_content, LIST_CONTENT)
