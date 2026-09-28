"""
Buttons on a connection that cannot send buttons.

WhatsApp reserves interactive messages for the official Cloud API. A QR-linked
number can put one on the wire and have it accepted -- the send succeeds, the
row reaches DELIVERED -- and the recipient's phone shows the body text with
nothing under it. Measured on a live connection: 15 button messages sent, zero
taps ever received, and no error anywhere to explain it.

So on those connections a question goes out as a numbered list and the answer
arrives as ordinary text. What is tested here is that the answer still *is* a
button press everywhere it matters: the same `replied_button_id`, matched by
the same rules, resuming the same workflows. There is one automation engine,
and these tests exist to keep it that way.

The tests are written against the whole path -- send, then a real inbound
message through the real receiver -- rather than against the resolver alone,
because the thing worth proving is that the ladder behaves identically for a
tap and for a typed "1".
"""

import itertools
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.contacts.models import Contact
from apps.inbox import services as inbox_services
from apps.inbox.models import Conversation
from apps.inbox.signals import inbound_message_recorded
from apps.messaging import services as messaging_services
from apps.messaging.models import Message, PendingButtonPrompt
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount, WhatsAppWebhookEvent
from apps.whatsapp.providers.base import SendResult

from . import services
from .models import AutomationRule

CALLBACK_BUTTONS = [
    {"type": "REPLY", "id": "callback_yes", "title": "Yes please", "url": ""},
    {"type": "REPLY", "id": "callback_no", "title": "Not now", "url": ""},
]

PLAN_BUTTONS = [
    {"type": "REPLY", "id": "plan_basic", "title": "Basic", "url": ""},
    {"type": "REPLY", "id": "plan_premium", "title": "Premium", "url": ""},
    {"type": "REPLY", "id": "plan_enterprise", "title": "Enterprise", "url": ""},
]


def _sends(prefix):
    counter = itertools.count(1)
    return lambda *args, **kwargs: SendResult(
        success=True, provider_message_id=f"{prefix}.{next(counter)}",
    )


class NumberedOptionsBase(TestCase):
    """One tenant with two numbers: a QR-linked one and a Cloud API one."""

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Acme", slug=f"nо-{self._testMethodName}"[:50], kind=Tenant.Kind.CUSTOMER,
        )
        self.user = get_user_model().objects.create_user(
            username=f"no-owner-{self._testMethodName}"[:140], password="x", tenant=self.tenant, credits=500,
        )
        self.qr_account = self._account("QR line", "qr-1", WhatsAppAccount.ConnectionMethod.QR)
        self.cloud_account = self._account("Cloud line", "cloud-1", WhatsAppAccount.ConnectionMethod.CLOUD_API)

        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Priya", phone="+15551234567",
        )
        self.conversation = Conversation.objects.create(
            tenant=self.tenant, contact=self.contact, whatsapp_account=self.qr_account,
        )

    def _account(self, name, pnid, method):
        # provider_session_id is globally unique, so it is derived from the
        # phone_number_id rather than shared between two QR accounts.
        account = WhatsAppAccount(
            tenant=self.tenant, user=self.user, name=name, phone_number_id=pnid,
            connection_method=method, status=WhatsAppAccount.Status.CONNECTED,
            provider_session_id=(
                f"session-{pnid}-{self._testMethodName}"[:120]
                if method == WhatsAppAccount.ConnectionMethod.QR
                # None, not "" -- the column is unique and nullable, so two
                # Cloud API accounts sharing an empty string collide.
                else None
            ),
        )
        account.set_access_token("token")
        account.set_app_secret("secret")
        account.save()
        return account

    def _ask(self, buttons=None, account=None, body="Would you like a callback?"):
        """Send a question and run the real send path over it."""
        account = account or self.qr_account
        message = Message.objects.create(
            tenant=self.tenant, conversation=self.conversation, whatsapp_account=account,
            contact=self.contact, to_phone=self.contact.phone, direction=Message.Direction.OUTBOUND,
            body=body, buttons=buttons if buttons is not None else CALLBACK_BUTTONS,
            status=Message.Status.QUEUED,
        )
        messaging_services.attempt_send(message)
        message.refresh_from_db()
        return message

    def _reply(self, text, *, message_id=None, account=None):
        """A real inbound message, taken through the real receiver."""
        account = account or self.qr_account
        event = WhatsAppWebhookEvent.objects.create(
            account=account, event_type=WhatsAppWebhookEvent.EventType.INBOUND_MESSAGE,
            provider_event_id=message_id or f"wamid.in.{timezone.now().timestamp()}",
            payload={
                "message": {
                    "id": message_id or f"wamid.in.{timezone.now().timestamp()}",
                    "from": "15551234567", "type": "text", "text": {"body": text},
                },
                "profile_name": "",
            },
        )
        message = inbox_services.record_inbound_message(event)
        inbound_message_recorded.send(
            sender=Message, message=message,
            is_new_conversation=getattr(message, "is_new_conversation", False),
        )
        message.refresh_from_db()
        return message


# ---------------------------------------------------------------------------
# Provider detection -- which format each number gets
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_interactive_message")
@patch("apps.messaging.services.ConnectionManager.send_text_message")
class ProviderDetectionTests(NumberedOptionsBase):

    def test_a_cloud_api_number_still_sends_native_buttons(self, text, interactive):
        interactive.side_effect = _sends("wamid.native")

        self._ask(account=self.cloud_account)

        interactive.assert_called_once()
        text.assert_not_called()
        # The buttons reach the provider untouched.
        self.assertEqual(interactive.call_args.args[3], CALLBACK_BUTTONS)

    def test_a_qr_number_sends_a_numbered_list_instead(self, text, interactive):
        text.side_effect = _sends("wamid.numbered")

        self._ask(account=self.qr_account)

        interactive.assert_not_called()
        text.assert_called_once()
        body = text.call_args.args[2]
        self.assertIn("Would you like a callback?", body)
        self.assertIn("1. Yes please", body)
        self.assertIn("2. Not now", body)
        self.assertIn("Please reply with the option number.", body)

    def test_the_choice_is_made_per_account_not_globally(self, text, interactive):
        text.side_effect = _sends("wamid.t")
        interactive.side_effect = _sends("wamid.i")

        self._ask(account=self.qr_account)
        self._ask(account=self.cloud_account)

        # One QR number using the fallback must not disable buttons on a Cloud
        # API number belonging to the same tenant.
        self.assertEqual(text.call_count, 1)
        self.assertEqual(interactive.call_count, 1)

    def test_a_newly_connected_qr_device_needs_no_configuration(self, text, interactive):
        text.side_effect = _sends("wamid.new")
        fresh = self._account("Brand new line", "qr-2", WhatsAppAccount.ConnectionMethod.QR)

        self._ask(account=fresh)

        # Nothing was set on the account: the format follows from the
        # connection method, which `WhatsAppAccount.save` derives itself.
        self.assertEqual(fresh.provider, WhatsAppAccount.Provider.QR_PROVIDER)
        interactive.assert_not_called()
        text.assert_called_once()

    def test_a_newly_connected_cloud_account_needs_no_configuration(self, text, interactive):
        interactive.side_effect = _sends("wamid.new2")
        fresh = self._account("Brand new cloud", "cloud-2", WhatsAppAccount.ConnectionMethod.CLOUD_API)

        self._ask(account=fresh)

        self.assertEqual(fresh.provider, WhatsAppAccount.Provider.CLOUD_API)
        interactive.assert_called_once()


# ---------------------------------------------------------------------------
# Resolving a typed answer
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class ReplyResolutionTests(NumberedOptionsBase):

    def test_a_number_resolves_to_the_button_it_stood_for(self, send):
        send.side_effect = _sends("wamid.r1")
        self._ask()

        reply = self._reply("1")

        self.assertEqual(reply.replied_button_id, "callback_yes")
        self.assertEqual(reply.interaction_type, Message.Interaction.NUMBERED_FALLBACK)

    def test_the_second_option_resolves_to_the_second_button(self, send):
        send.side_effect = _sends("wamid.r2")
        self._ask()

        self.assertEqual(self._reply("2").replied_button_id, "callback_no")

    def test_surrounding_whitespace_is_ignored(self, send):
        send.side_effect = _sends("wamid.r3")
        self._ask()

        self.assertEqual(self._reply("  1  ").replied_button_id, "callback_yes")

    def test_the_punctuation_people_copy_off_the_list_is_ignored(self, send):
        send.side_effect = _sends("wamid.r4")
        self._ask()

        self.assertEqual(self._reply("2.").replied_button_id, "callback_no")

    def test_the_options_own_wording_resolves_too(self, send):
        send.side_effect = _sends("wamid.r5")
        self._ask()

        reply = self._reply("Yes please")

        self.assertEqual(reply.replied_button_id, "callback_yes")
        # A different route to the same choice, and recorded as such.
        self.assertEqual(reply.interaction_type, Message.Interaction.TEXT_OPTION)

    def test_the_wording_match_is_case_insensitive(self, send):
        send.side_effect = _sends("wamid.r6")
        self._ask()

        self.assertEqual(self._reply("YES PLEASE").replied_button_id, "callback_yes")

    def test_internal_spacing_in_the_wording_is_forgiven(self, send):
        send.side_effect = _sends("wamid.r7")
        self._ask()

        self.assertEqual(self._reply("yes  please").replied_button_id, "callback_yes")

    def test_more_than_two_options_are_numbered_and_resolved(self, send):
        send.side_effect = _sends("wamid.r8")
        self._ask(buttons=PLAN_BUTTONS, body="Which plan?")

        body = send.call_args.args[2]
        self.assertIn("1. Basic", body)
        self.assertIn("2. Premium", body)
        self.assertIn("3. Enterprise", body)
        self.assertEqual(self._reply("3").replied_button_id, "plan_enterprise")

    def test_a_number_in_a_sentence_is_not_read_as_a_choice(self, send):
        # "I'll take 2 of them" is a sentence. Reading a number out of the
        # middle of one would fire an automation the customer never picked.
        send.side_effect = _sends("wamid.r9")
        self._ask(buttons=PLAN_BUTTONS, body="Which plan?")

        self.assertEqual(self._reply("can I have 2 of those?").replied_button_id, "")

    def test_a_partial_word_does_not_match(self, send):
        # No fuzzy matching: "Not" is one edit from "Not now" and the cost of
        # guessing wrong is the wrong automation on somebody's customer.
        send.side_effect = _sends("wamid.r10")
        self._ask()

        self.assertEqual(self._reply("Not").replied_button_id, "")

    def test_two_options_worded_the_same_resolve_to_neither(self, send):
        send.side_effect = _sends("wamid.r11")
        self._ask(buttons=[
            {"type": "REPLY", "id": "a", "title": "Yes", "url": ""},
            {"type": "REPLY", "id": "b", "title": "yes", "url": ""},
        ])

        # Ambiguous by wording -- but the numbers still work.
        self.assertEqual(self._reply("yes").replied_button_id, "")
        self.assertEqual(self._reply("1").replied_button_id, "a")

    def test_ordinary_conversation_while_a_question_is_open_is_left_alone(self, send):
        send.side_effect = _sends("wamid.r12")
        self._ask()

        reply = self._reply("actually, can I speak to someone?")

        self.assertEqual(reply.replied_button_id, "")
        self.assertEqual(reply.interaction_type, "")
        # And the question is still open for a later answer.
        self.assertTrue(PendingButtonPrompt.objects.get(conversation=self.conversation).is_open)


# ---------------------------------------------------------------------------
# The choice drives the existing automation, not a second engine
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class ExistingAutomationTests(NumberedOptionsBase):

    def test_a_typed_number_fires_the_button_rule_it_stands_for(self, send):
        send.side_effect = _sends("wamid.a1")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.qr_account, name="Callback yes",
            trigger_type=AutomationRule.Trigger.BUTTON, button_id="callback_yes",
            message_body="Great -- we'll ring you within the hour.",
        )
        self._ask()

        self._reply("1")

        answered = Message.objects.filter(
            direction=Message.Direction.OUTBOUND, body="Great -- we'll ring you within the hour.",
        )
        self.assertTrue(answered.exists())

    def test_the_wording_reply_fires_the_same_rule(self, send):
        send.side_effect = _sends("wamid.a2")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.qr_account, name="Callback yes",
            trigger_type=AutomationRule.Trigger.BUTTON, button_id="callback_yes", message_body="On our way.",
        )
        self._ask()

        self._reply("Yes please")

        self.assertTrue(Message.objects.filter(direction=Message.Direction.OUTBOUND, body="On our way.").exists())

    def test_a_resolved_choice_does_not_also_fire_a_keyword_rule(self, send):
        # The contact chose a specific option. A keyword rule that happens to
        # match the label too must not send a second message seconds later.
        send.side_effect = _sends("wamid.a3")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.qr_account, name="Callback yes",
            trigger_type=AutomationRule.Trigger.BUTTON, button_id="callback_yes", message_body="Booked.",
        )
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.qr_account, name="Yes keyword",
            trigger_type=AutomationRule.Trigger.KEYWORD, keyword="yes please", keyword_match_type=AutomationRule.MatchType.EXACT, message_body="Keyword reply.",
        )
        self._ask()

        self._reply("Yes please")

        self.assertTrue(Message.objects.filter(body="Booked.").exists())
        self.assertFalse(Message.objects.filter(body="Keyword reply.").exists())


# ---------------------------------------------------------------------------
# An answer that is not on the list
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class InvalidReplyTests(NumberedOptionsBase):

    def test_a_number_off_the_end_gets_the_options_read_back(self, send):
        send.side_effect = _sends("wamid.i1")
        self._ask()

        self._reply("99")

        reprompt = Message.objects.filter(direction=Message.Direction.OUTBOUND).order_by("-id").first()
        self.assertIn("Please choose one of the available options", reprompt.body)
        self.assertIn("1. Yes please", reprompt.body)
        self.assertIn("2. Not now", reprompt.body)

    def test_an_invalid_choice_does_not_fire_an_unrelated_automation(self, send):
        send.side_effect = _sends("wamid.i2")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.qr_account, name="Nine",
            trigger_type=AutomationRule.Trigger.KEYWORD, keyword="99", keyword_match_type=AutomationRule.MatchType.EXACT, message_body="Unrelated reply.",
        )
        self._ask()

        self._reply("99")

        self.assertFalse(Message.objects.filter(body="Unrelated reply.").exists())

    def test_the_question_stays_open_after_an_invalid_choice(self, send):
        send.side_effect = _sends("wamid.i3")
        self._ask()

        self._reply("99")

        self.assertTrue(PendingButtonPrompt.objects.get(conversation=self.conversation).is_open)
        self.assertEqual(self._reply("1").replied_button_id, "callback_yes")

    def test_the_options_are_not_read_back_for_ever(self, send):
        send.side_effect = _sends("wamid.i4")
        self._ask()

        for attempt in range(4):
            self._reply("99", message_id=f"wamid.in.loop.{attempt}")

        reprompts = Message.objects.filter(
            direction=Message.Direction.OUTBOUND, body__startswith="Please choose one",
        ).count()
        self.assertEqual(reprompts, services.MAX_REPROMPTS)


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class ExpiryTests(NumberedOptionsBase):

    def test_a_number_typed_after_the_window_is_not_an_answer(self, send):
        send.side_effect = _sends("wamid.e1")
        self._ask()
        PendingButtonPrompt.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        reply = self._reply("1")

        self.assertEqual(reply.replied_button_id, "")
        self.assertEqual(reply.interaction_type, "")

    def test_an_expired_question_fires_no_automation(self, send):
        send.side_effect = _sends("wamid.e2")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.qr_account, name="Callback yes",
            trigger_type=AutomationRule.Trigger.BUTTON, button_id="callback_yes", message_body="Booked.",
        )
        self._ask()
        PendingButtonPrompt.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        self._reply("1")

        self.assertFalse(Message.objects.filter(body="Booked.").exists())

    @override_settings(BUTTON_PROMPT_TIMEOUT_MINUTES=90)
    def test_the_window_is_configurable(self, send):
        send.side_effect = _sends("wamid.e3")

        self._ask()

        prompt = PendingButtonPrompt.objects.get()
        minutes = (prompt.expires_at - prompt.created_at).total_seconds() / 60
        self.assertAlmostEqual(minutes, 90, delta=1)


# ---------------------------------------------------------------------------
# Isolation -- one question cannot answer another
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class IsolationTests(NumberedOptionsBase):

    def _second_conversation(self):
        other_contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Ravi", phone="+15559998888",
        )
        return Conversation.objects.create(
            tenant=self.tenant, contact=other_contact, whatsapp_account=self.qr_account,
        ), other_contact

    def test_one_contacts_number_cannot_answer_anothers_question(self, send):
        send.side_effect = _sends("wamid.s1")
        other_conversation, other_contact = self._second_conversation()
        # Priya is asked about a callback; Ravi is asked about plans.
        self._ask()
        other_message = Message.objects.create(
            tenant=self.tenant, conversation=other_conversation, whatsapp_account=self.qr_account,
            contact=other_contact, to_phone=other_contact.phone, direction=Message.Direction.OUTBOUND,
            body="Which plan?", buttons=PLAN_BUTTONS, status=Message.Status.QUEUED,
        )
        messaging_services.attempt_send(other_message)

        # Priya types "3" -- valid for Ravi's question, off the end of hers.
        reply = self._reply("3")

        self.assertEqual(reply.replied_button_id, "")
        self.assertTrue(
            PendingButtonPrompt.objects.get(conversation=other_conversation).is_open,
            "answering one conversation must not close another's question",
        )

    def test_each_conversation_resolves_against_its_own_question(self, send):
        send.side_effect = _sends("wamid.s2")
        other_conversation, other_contact = self._second_conversation()
        self._ask()
        messaging_services.attempt_send(Message.objects.create(
            tenant=self.tenant, conversation=other_conversation, whatsapp_account=self.qr_account,
            contact=other_contact, to_phone=other_contact.phone, direction=Message.Direction.OUTBOUND,
            body="Which plan?", buttons=PLAN_BUTTONS, status=Message.Status.QUEUED,
        ))

        self.assertEqual(self._reply("1").replied_button_id, "callback_yes")

    def test_another_tenants_question_is_never_resolved_against(self, send):
        send.side_effect = _sends("wamid.s3")
        rival = Tenant.objects.create(
            name="Rival", slug=f"no-rival-{self._testMethodName}"[:50], kind=Tenant.Kind.CUSTOMER,
        )
        rival_user = get_user_model().objects.create_user(
            username=f"no-rival-{self._testMethodName}"[:140], password="x", tenant=rival, credits=100,
        )
        rival_account = WhatsAppAccount(
            tenant=rival, user=rival_user, name="Rival line", phone_number_id="rival-1",
            connection_method=WhatsAppAccount.ConnectionMethod.QR, status=WhatsAppAccount.Status.CONNECTED,
            provider_session_id=f"rival-session-{self._testMethodName}"[:120],
        )
        rival_account.set_access_token("t")
        rival_account.set_app_secret("s")
        rival_account.save()
        rival_contact = Contact.objects.create(
            tenant=rival, created_by=rival_user, name="Someone", phone="+15551234567",
        )
        rival_conversation = Conversation.objects.create(
            tenant=rival, contact=rival_contact, whatsapp_account=rival_account,
        )
        messaging_services.attempt_send(Message.objects.create(
            tenant=rival, conversation=rival_conversation, whatsapp_account=rival_account,
            contact=rival_contact, to_phone=rival_contact.phone, direction=Message.Direction.OUTBOUND,
            body="Which plan?", buttons=PLAN_BUTTONS, status=Message.Status.QUEUED,
        ))

        # Our tenant has no open question at all; the rival's must not serve.
        reply = self._reply("1")

        self.assertEqual(reply.replied_button_id, "")
        self.assertTrue(PendingButtonPrompt.objects.get(tenant=rival).is_open)

    def test_a_new_question_supersedes_the_one_before_it(self, send):
        # Two open questions would make "2" ambiguous; the honest reading of a
        # number is always the most recent thing the customer was asked.
        send.side_effect = _sends("wamid.s4")
        self._ask()
        self._ask(buttons=PLAN_BUTTONS, body="Which plan?")

        self.assertEqual(PendingButtonPrompt.objects.filter(answered_at__isnull=True).count(), 1)
        self.assertEqual(self._reply("3").replied_button_id, "plan_enterprise")


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class InteractionTypeTests(NumberedOptionsBase):

    def test_a_native_tap_is_recorded_as_a_native_tap(self, send):
        event = WhatsAppWebhookEvent.objects.create(
            account=self.cloud_account, event_type=WhatsAppWebhookEvent.EventType.INBOUND_MESSAGE,
            provider_event_id="wamid.tap.1",
            payload={
                "message": {
                    "id": "wamid.tap.1", "from": "15551234567", "type": "interactive",
                    "interactive": {
                        "type": "button_reply",
                        "button_reply": {"id": "callback_yes", "title": "Yes please"},
                    },
                },
                "profile_name": "",
            },
        )

        message = inbox_services.record_inbound_message(event)

        self.assertEqual(message.interaction_type, Message.Interaction.NATIVE_BUTTON)

    def test_a_numbered_reply_is_not_counted_as_a_tap(self, send):
        send.side_effect = _sends("wamid.n1")
        self._ask()

        reply = self._reply("1")

        self.assertEqual(reply.interaction_type, Message.Interaction.NUMBERED_FALLBACK)
        self.assertNotEqual(reply.interaction_type, Message.Interaction.NATIVE_BUTTON)

    def test_an_ordinary_message_carries_no_interaction_type(self, send):
        reply = self._reply("hello there")

        self.assertEqual(reply.interaction_type, "")

    def test_the_three_routes_are_separately_countable(self, send):
        send.side_effect = _sends("wamid.n2")
        self._ask()
        self._reply("1", message_id="wamid.count.1")
        self._ask(buttons=PLAN_BUTTONS, body="Which plan?")
        self._reply("Premium", message_id="wamid.count.2")

        counts = {
            row: Message.objects.filter(interaction_type=row).count()
            for row in (
                Message.Interaction.NUMBERED_FALLBACK,
                Message.Interaction.TEXT_OPTION,
                Message.Interaction.NATIVE_BUTTON,
            )
        }
        self.assertEqual(counts[Message.Interaction.NUMBERED_FALLBACK], 1)
        self.assertEqual(counts[Message.Interaction.TEXT_OPTION], 1)
        self.assertEqual(counts[Message.Interaction.NATIVE_BUTTON], 0)


# ---------------------------------------------------------------------------
# The prompt row itself
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class PendingPromptTests(NumberedOptionsBase):

    def test_a_cloud_api_send_records_no_pending_question(self, send):
        with patch("apps.messaging.services.ConnectionManager.send_interactive_message") as interactive:
            interactive.side_effect = _sends("wamid.p1")
            self._ask(account=self.cloud_account)

        # Nothing to resolve: the real buttons come back with their own id.
        self.assertFalse(PendingButtonPrompt.objects.exists())

    def test_a_qr_send_records_the_options_in_the_order_they_were_numbered(self, send):
        send.side_effect = _sends("wamid.p2")

        self._ask(buttons=PLAN_BUTTONS, body="Which plan?")

        prompt = PendingButtonPrompt.objects.get()
        self.assertEqual([option["id"] for option in prompt.options], ["plan_basic", "plan_premium", "plan_enterprise"])
        self.assertEqual(prompt.tenant_id, self.tenant.pk)
        self.assertEqual(prompt.conversation_id, self.conversation.pk)
        self.assertEqual(prompt.contact_id, self.contact.pk)
        self.assertEqual(prompt.whatsapp_account_id, self.qr_account.pk)

    def test_a_link_only_message_records_no_question(self, send):
        # There is no number to reply with, so there is nothing to resolve.
        send.side_effect = _sends("wamid.p3")

        self._ask(buttons=[{"type": "URL", "id": "", "title": "Open", "url": "https://example.com"}])

        self.assertFalse(PendingButtonPrompt.objects.exists())
        self.assertNotIn("Please reply with the option number", send.call_args.args[2])

    def test_answering_closes_the_question(self, send):
        send.side_effect = _sends("wamid.p4")
        self._ask()

        self._reply("1")

        prompt = PendingButtonPrompt.objects.get()
        self.assertIsNotNone(prompt.answered_at)
        self.assertEqual(prompt.resolved_button_id, "callback_yes")

    def test_a_closed_question_cannot_be_answered_twice(self, send):
        send.side_effect = _sends("wamid.p5")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.qr_account, name="Callback yes",
            trigger_type=AutomationRule.Trigger.BUTTON, button_id="callback_yes", message_body="Booked.",
        )
        self._ask()
        self._reply("1", message_id="wamid.twice.1")

        self._reply("2", message_id="wamid.twice.2")

        # The second number is not an answer to a question already closed.
        self.assertEqual(Message.objects.filter(body="Booked.").count(), 1)
        self.assertEqual(PendingButtonPrompt.objects.get().resolved_button_id, "callback_yes")
