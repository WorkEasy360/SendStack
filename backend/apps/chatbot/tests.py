"""
End-to-end tests for the chatbot, driven from the webhook inwards.

Almost nothing here calls the engine directly. A test that starts at
``engine.handle`` proves the engine works and says nothing about whether a real
WhatsApp message reaches it -- so the ones that matter post a signed payload to
the same public webhook Meta and the QR bridge post to, and assert on the
``Message`` rows that come out. The provider is the only thing mocked, because
it is the only thing that is somebody else's server.
"""

import hashlib
import hmac
import itertools
import json
from datetime import time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.ai.models import AIAgentConfig, KnowledgeBase, KnowledgeDocument
from apps.contacts.models import Contact
from apps.inbox.models import Conversation
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount
from apps.whatsapp.providers.base import SendResult

from . import engine, flow, services
from .models import Chatbot, ChatbotEvent, ChatbotHandoff, ChatbotRule, ChatbotSession, ChatbotWorkingHours

APP_SECRET = "chatbot-test-app-secret"


def unique_sends(prefix):
    """A provider mock that returns a distinct id per call -- the column is globally unique."""
    counter = itertools.count(1)
    return lambda *args, **kwargs: SendResult(success=True, provider_message_id=f"{prefix}.{next(counter)}")


class ChatbotTestCase(TestCase):
    """One tenant, one connected number, one published bot -- the shape every test needs."""

    slug = "chatbot-base"

    def setUp(self):
        cache.clear()
        self.tenant = Tenant.objects.create(name="Acme", slug=self.slug, kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username=f"{self.slug}-owner", password="x", tenant=self.tenant, credits=1000,
        )
        self.account = self._account("Support Line", "+919999900001")
        self.chatbot = self._chatbot()

    def _account(self, name, phone):
        account = WhatsAppAccount(
            tenant=self.tenant, user=self.user, name=name, phone_number=phone,
            phone_number_id="pn-1", status=WhatsAppAccount.Status.CONNECTED,
            webhook_verify_token="verify-me",
        )
        account.set_access_token("token")
        account.set_app_secret(APP_SECRET)
        account.save()
        return account

    def _chatbot(self, **overrides):
        fields = {
            "tenant": self.tenant, "whatsapp_account": self.account, "created_by": self.user,
            "name": overrides.pop("name", "Support Bot"),
            "bot_type": Chatbot.BotType.HYBRID,
            "welcome_message": "Hi! I'm the Acme assistant. How can I help?",
            "fallback_message": "Sorry, I didn't get that.",
            "ai_enabled": False,
        }
        fields.update(overrides)
        return Chatbot.objects.create(**fields)

    def _rule(self, name, pattern, body, *, chatbot=None, priority=10, match_type=ChatbotRule.MatchType.CONTAINS, **kwargs):
        return ChatbotRule.objects.create(
            chatbot=chatbot or self.chatbot, name=name, match_type=match_type, pattern=pattern,
            priority=priority, actions=[{"type": "SEND_MESSAGE", "body": body, "buttons": []}], **kwargs,
        )

    def _publish(self, chatbot=None):
        return services.publish(chatbot or self.chatbot, user=self.user)

    # -- the real inbound path ---------------------------------------------

    def _webhook_body(self, *, body="Hello", message_id="wamid.1", from_number="919888800001", extra=None):
        message = {"id": message_id, "from": from_number, "type": "text", "text": {"body": body}}
        if extra:
            message = {**message, **extra}
        return json.dumps({
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "waba-1",
                "changes": [{
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"phone_number_id": "pn-1"},
                        "contacts": [{"profile": {"name": "Riya"}, "wa_id": from_number}],
                        "messages": [message],
                    },
                }],
            }],
        })

    def post_webhook(self, *, account=None, **kwargs):
        """
        Deliver one inbound message the way the provider actually does.

        Signed with the account's own app secret, so this exercises the real
        signature check rather than bypassing it -- a test that posted an
        unsigned body would pass just as well with the verification removed.
        """
        account = account or self.account
        raw = self._webhook_body(**kwargs).encode()
        signature = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        return self.client.post(
            reverse("whatsapp-webhook", args=[account.pk]),
            data=raw, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=signature,
        )

    # -- assertions ---------------------------------------------------------

    def bot_messages(self, conversation=None):
        queryset = Message.objects.filter(direction=Message.Direction.OUTBOUND, is_automated=True)
        if conversation is not None:
            queryset = queryset.filter(conversation=conversation)
        return list(queryset.order_by("queued_at", "id"))

    def bodies(self, conversation=None):
        return [message.body for message in self.bot_messages(conversation)]

    def session(self):
        return ChatbotSession.objects.get(conversation__whatsapp_account=self.account)

    def trace_steps(self, step=None):
        queryset = ChatbotEvent.objects.filter(tenant=self.tenant)
        if step:
            queryset = queryset.filter(step=step)
        return list(queryset.order_by("created_at", "id"))


# ---------------------------------------------------------------------------
# Test 1-2: the customer says hello, and asks a question
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class InboundConversationTests(ChatbotTestCase):
    slug = "chatbot-inbound"

    def test_first_message_gets_the_welcome(self, send):
        send.side_effect = unique_sends("wamid.out")
        self._publish()

        response = self.post_webhook(body="Hi")

        self.assertEqual(response.status_code, 200)
        conversation = Conversation.objects.get(whatsapp_account=self.account)
        self.assertIn("Acme assistant", " ".join(self.bodies(conversation)))
        self.assertEqual(self.session().state, ChatbotSession.State.BOT_ACTIVE)
        self.assertEqual(self.session().messages_received, 1)

    def test_a_rule_answers_the_question(self, send):
        send.side_effect = unique_sends("wamid.out")
        self._rule("Pricing", "price", "Our basic plan starts at Rs.999/month.")
        self._publish()

        self.post_webhook(body="What is your price?")

        self.assertIn("Rs.999/month", " ".join(self.bodies()))

    def test_welcome_and_answer_both_arrive_on_a_first_message(self, send):
        """A greeting is not an answer, so a first message that asks something gets both."""
        send.side_effect = unique_sends("wamid.out")
        self._rule("Pricing", "price", "Our basic plan starts at Rs.999/month.")
        self._publish()

        self.post_webhook(body="hi what is your price?")

        bodies = " ".join(self.bodies())
        self.assertIn("Acme assistant", bodies)
        self.assertIn("Rs.999/month", bodies)

    def test_rules_run_in_priority_order(self, send):
        send.side_effect = unique_sends("wamid.out")
        self._rule("Refunds", "refund", "Refunds take 5-7 days.", priority=1)
        self._rule("Catch all", "", "I can help with pricing and refunds.",
                   priority=100, match_type=ChatbotRule.MatchType.ANY)
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._publish()

        self.post_webhook(body="I need a refund please")

        self.assertEqual(self.bodies(), ["Refunds take 5-7 days."])

    def test_the_catch_all_answers_what_nothing_else_did(self, send):
        send.side_effect = unique_sends("wamid.out")
        self._rule("Refunds", "refund", "Refunds take 5-7 days.", priority=1)
        self._rule("Catch all", "", "I can help with pricing and refunds.",
                   priority=100, match_type=ChatbotRule.MatchType.ANY)
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._publish()

        self.post_webhook(body="do you sell helicopters")

        self.assertEqual(self.bodies(), ["I can help with pricing and refunds."])

    def test_regex_and_keyword_rules_match(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._rule(
            "Order lookup", r"^order\s+#?\d{4,}$", "Let me check that order.",
            match_type=ChatbotRule.MatchType.REGEX, priority=1,
        )
        self._rule(
            "Greetings", "hello, hey, namaste", "Hello there!",
            match_type=ChatbotRule.MatchType.KEYWORD, priority=2,
        )
        self._publish()

        self.post_webhook(body="order #12345", message_id="wamid.a")
        self.assertIn("Let me check that order.", self.bodies())

        self.post_webhook(body="namaste", message_id="wamid.b")
        self.assertIn("Hello there!", self.bodies())

    def test_keyword_rules_match_whole_words_only(self, send):
        """'price' must not fire on 'surprise' -- a bot answering that is worse than silence."""
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._rule("Pricing", "price", "Rs.999/month.", match_type=ChatbotRule.MatchType.KEYWORD)
        self._publish()

        self.post_webhook(body="what a lovely surprise")

        self.assertNotIn("Rs.999/month.", self.bodies())

    def test_an_unpublished_bot_answers_nothing(self, send):
        send.side_effect = unique_sends("wamid.out")
        self._rule("Pricing", "price", "Rs.999/month.")
        # deliberately not published

        self.post_webhook(body="what is your price?")

        self.assertEqual(self.bodies(), [])
        self.assertFalse(ChatbotSession.objects.exists())


# ---------------------------------------------------------------------------
# Test 7: duplicate webhooks
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class IdempotencyTests(ChatbotTestCase):
    slug = "chatbot-idempotent"

    def test_the_same_webhook_twice_produces_one_reply(self, send):
        send.side_effect = unique_sends("wamid.out")
        self._rule("Pricing", "price", "Rs.999/month.")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._publish()

        self.post_webhook(body="price?", message_id="wamid.same")
        self.post_webhook(body="price?", message_id="wamid.same")

        self.assertEqual(self.bodies().count("Rs.999/month."), 1)
        self.assertEqual(Message.objects.filter(direction=Message.Direction.INBOUND).count(), 1)

    def test_the_engine_run_twice_on_one_message_produces_one_reply(self, send):
        """Belt and braces: Celery delivers at least once, so the task itself must be safe to retry."""
        send.side_effect = unique_sends("wamid.out")
        self._rule("Pricing", "price", "Rs.999/month.")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._publish()

        self.post_webhook(body="price?", message_id="wamid.retry")
        inbound = Message.objects.get(direction=Message.Direction.INBOUND)

        self.assertEqual(engine.handle(inbound.pk), "DUPLICATE")
        self.assertEqual(self.bodies().count("Rs.999/month."), 1)

    def test_a_rule_cooldown_stops_a_repeated_keyword_being_answered_twice(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._rule("Pricing", "price", "Rs.999/month.", cooldown_seconds=600)
        self._publish()

        self.post_webhook(body="price?", message_id="wamid.c1")
        self.post_webhook(body="price again?", message_id="wamid.c2")

        self.assertEqual(self.bodies().count("Rs.999/month."), 1)


# ---------------------------------------------------------------------------
# Tests 4-6: human handoff, takeover, and return to bot
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class HandoffTests(ChatbotTestCase):
    slug = "chatbot-handoff"

    def test_asking_for_a_human_stops_the_bot(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._rule("Pricing", "price", "Rs.999/month.")
        self._publish()

        self.post_webhook(body="I want to talk to a human", message_id="wamid.h1")

        session = self.session()
        self.assertEqual(session.state, ChatbotSession.State.HUMAN_ACTIVE)
        self.assertIsNotNone(session.handed_off_at)
        self.assertTrue(
            ChatbotHandoff.objects.filter(
                conversation=session.conversation, trigger=ChatbotHandoff.Trigger.CUSTOMER_REQUEST,
            ).exists()
        )
        self.assertIn("colleague", " ".join(self.bodies()))

    def test_hinglish_asking_for_a_human_also_works(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._publish()

        self.post_webhook(body="mujhe agent se baat karni hai", message_id="wamid.h2")

        self.assertEqual(self.session().state, ChatbotSession.State.HUMAN_ACTIVE)

    def test_the_bot_stays_quiet_once_a_person_has_it(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._rule("Pricing", "price", "Rs.999/month.")
        self._publish()

        self.post_webhook(body="talk to a human", message_id="wamid.h3")
        before = len(self.bot_messages())

        self.post_webhook(body="what is your price?", message_id="wamid.h4")

        self.assertEqual(len(self.bot_messages()), before)
        self.assertNotIn("Rs.999/month.", self.bodies())

    def test_an_agent_takeover_silences_the_bot(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._rule("Pricing", "price", "Rs.999/month.")
        self._publish()

        self.post_webhook(body="hello", message_id="wamid.t1")
        conversation = Conversation.objects.get(whatsapp_account=self.account)
        services.take_over(conversation, self.user)

        self.post_webhook(body="what is your price?", message_id="wamid.t2")

        self.assertNotIn("Rs.999/month.", self.bodies())
        self.assertEqual(self.session().state, ChatbotSession.State.HUMAN_ACTIVE)

    def test_returning_to_the_bot_makes_it_answer_again(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._rule("Pricing", "price", "Rs.999/month.")
        self._publish()

        self.post_webhook(body="hello", message_id="wamid.r1")
        conversation = Conversation.objects.get(whatsapp_account=self.account)
        services.take_over(conversation, self.user)
        services.resume_bot(conversation, self.user)

        self.post_webhook(body="what is your price?", message_id="wamid.r2")

        self.assertIn("Rs.999/month.", self.bodies())
        self.assertEqual(self.session().state, ChatbotSession.State.BOT_ACTIVE)

    def test_returning_to_the_bot_closes_the_open_handoff(self, send):
        send.side_effect = unique_sends("wamid.out")
        self._publish()
        self.post_webhook(body="talk to a human", message_id="wamid.r3")

        conversation = Conversation.objects.get(whatsapp_account=self.account)
        services.resume_bot(conversation, self.user)

        handoff = ChatbotHandoff.objects.filter(conversation=conversation).first()
        self.assertIsNotNone(handoff.returned_at)

    def test_repeated_fallbacks_hand_over(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.handoff_conditions = {"repeated_fallback": 2}
        self.chatbot.save()
        self._publish()

        self.post_webhook(body="qwertyuiop", message_id="wamid.f1")
        self.assertEqual(self.session().state, ChatbotSession.State.BOT_ACTIVE)

        self.post_webhook(body="asdfghjkl", message_id="wamid.f2")

        session = self.session()
        self.assertEqual(session.state, ChatbotSession.State.HUMAN_ACTIVE)
        self.assertTrue(
            ChatbotHandoff.objects.filter(trigger=ChatbotHandoff.Trigger.REPEATED_FALLBACK).exists()
        )

    def test_an_assigned_conversation_is_left_to_its_agent(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._rule("Pricing", "price", "Rs.999/month.")
        self._publish()

        self.post_webhook(body="hello", message_id="wamid.a1")
        conversation = Conversation.objects.get(whatsapp_account=self.account)
        conversation.assigned_agent = self.user
        conversation.save(update_fields=["assigned_agent"])

        self.post_webhook(body="what is your price?", message_id="wamid.a2")

        self.assertNotIn("Rs.999/month.", self.bodies())


# ---------------------------------------------------------------------------
# Test 13: every message type, and Test 15: a closed conversation
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class MessageTypeTests(ChatbotTestCase):
    slug = "chatbot-types"

    def test_a_sticker_gets_a_readable_answer_rather_than_a_crash(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._publish()

        raw = json.dumps({
            "object": "whatsapp_business_account",
            "entry": [{"id": "waba-1", "changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": "pn-1"},
                "contacts": [{"profile": {"name": "Riya"}, "wa_id": "919888800001"}],
                "messages": [{"id": "wamid.sticker", "from": "919888800001", "type": "sticker",
                              "sticker": {"id": "media-1"}}],
            }}]}],
        }).encode()
        signature = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        response = self.client.post(
            reverse("whatsapp-webhook", args=[self.account.pk]),
            data=raw, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=signature,
        )

        self.assertEqual(response.status_code, 200)
        inbound = Message.objects.get(direction=Message.Direction.INBOUND)
        self.assertEqual(inbound.body, "[sticker]")
        self.assertIn("understand text messages best", " ".join(self.bodies()))

    def test_a_location_is_stored_readably(self, send):
        send.side_effect = unique_sends("wamid.out")
        self._publish()

        raw = json.dumps({
            "object": "whatsapp_business_account",
            "entry": [{"id": "waba-1", "changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": "pn-1"},
                "contacts": [{"profile": {"name": "Riya"}, "wa_id": "919888800001"}],
                "messages": [{"id": "wamid.loc", "from": "919888800001", "type": "location",
                              "location": {"latitude": 19.076, "longitude": 72.877, "name": "Andheri"}}],
            }}]}],
        }).encode()
        signature = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        self.client.post(
            reverse("whatsapp-webhook", args=[self.account.pk]),
            data=raw, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=signature,
        )

        inbound = Message.objects.get(direction=Message.Direction.INBOUND)
        self.assertIn("Andheri", inbound.body)
        self.assertIn("19.076", inbound.body)

    def test_an_image_with_a_caption_is_answered_normally(self, send):
        """A photo with a question under it is a question, not an unreadable message."""
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._rule("Pricing", "price", "Rs.999/month.")
        self._publish()

        raw = json.dumps({
            "object": "whatsapp_business_account",
            "entry": [{"id": "waba-1", "changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": "pn-1"},
                "contacts": [{"profile": {"name": "Riya"}, "wa_id": "919888800001"}],
                "messages": [{"id": "wamid.img", "from": "919888800001", "type": "image",
                              "image": {"id": "media-9", "caption": "what is the price of this?"}}],
            }}]}],
        }).encode()
        signature = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        self.client.post(
            reverse("whatsapp-webhook", args=[self.account.pk]),
            data=raw, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=signature,
        )

        self.assertIn("Rs.999/month.", self.bodies())

    def test_a_button_tap_drives_a_button_rule(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        ChatbotRule.objects.create(
            chatbot=self.chatbot, name="Pricing button", match_type=ChatbotRule.MatchType.BUTTON,
            pattern="see_pricing", priority=1,
            actions=[{"type": "SEND_MESSAGE", "body": "Plans start at Rs.999.", "buttons": []}],
        )
        self._publish()

        self.post_webhook(
            message_id="wamid.btn",
            extra={"type": "interactive", "interactive": {
                "type": "button_reply", "button_reply": {"id": "see_pricing", "title": "Pricing"},
            }},
        )

        self.assertIn("Plans start at Rs.999.", self.bodies())

    def test_a_new_message_reopens_a_resolved_conversation(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.conversation_timeout_minutes = 0
        self.chatbot.save()
        self._rule("Pricing", "price", "Rs.999/month.")
        self._publish()

        self.post_webhook(body="hello", message_id="wamid.cl1")
        session = self.session()
        services.resolve_session(session, ChatbotSession.Resolution.BOT)

        self.post_webhook(body="what is your price?", message_id="wamid.cl2")

        self.assertIn("Rs.999/month.", self.bodies())
        self.assertEqual(self.session().state, ChatbotSession.State.BOT_ACTIVE)


# ---------------------------------------------------------------------------
# Tests 9-11: everything breaking
# ---------------------------------------------------------------------------

class FailureTests(ChatbotTestCase):
    slug = "chatbot-failures"

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_a_disconnected_number_records_the_real_reason(self, send):
        send.side_effect = unique_sends("wamid.out")
        self._rule("Pricing", "price", "Rs.999/month.")
        self._publish()
        self.account.status = WhatsAppAccount.Status.DISCONNECTED
        self.account.save(update_fields=["status"])

        self.post_webhook(body="what is your price?")

        self.assertEqual(self.bodies(), [])
        details = [event.detail for event in self.trace_steps(ChatbotEvent.Step.DEDUPE)]
        self.assertTrue(
            any("disconnected" in detail.lower() for detail in details),
            f"expected a disconnected reason in the trail, got {details}",
        )

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_a_provider_failure_leaves_the_message_recorded_not_lost(self, send):
        send.return_value = SendResult(success=False, error="network unreachable", code="network_error")
        self._rule("Pricing", "price", "Rs.999/month.")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._publish()

        self.post_webhook(body="what is your price?")

        message = Message.objects.filter(direction=Message.Direction.OUTBOUND).first()
        self.assertIsNotNone(message, "the reply row must exist even when the send failed")
        self.assertIn(message.status, (Message.Status.QUEUED, Message.Status.FAILED))
        self.assertEqual(self.session().state, ChatbotSession.State.BOT_ACTIVE)

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_an_ai_provider_failure_hands_over_rather_than_going_silent(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.ai_enabled = True
        self.chatbot.bot_type = Chatbot.BotType.AI_BASED
        self.chatbot.save()
        self._publish()

        config = AIAgentConfig.objects.create(tenant=self.tenant, is_enabled=True)
        config.set_api_key("sk-ant-test")
        config.save()
        base = KnowledgeBase.objects.create(tenant=self.tenant, name="General", is_default=True)
        KnowledgeDocument.objects.create(
            tenant=self.tenant, knowledge_base=base, title="Prices",
            content="Our basic plan costs 999 rupees per month and includes 1000 messages.",
        )
        from apps.ai import knowledge as knowledge_module

        knowledge_module.rebuild(KnowledgeDocument.objects.get(title="Prices"))

        from apps.ai.providers.registry import AIProviderError

        with patch("apps.ai.services._ask", side_effect=AIProviderError("provider down")):
            self.post_webhook(body="how much does the basic plan cost per month")

        self.assertEqual(self.session().state, ChatbotSession.State.HUMAN_ACTIVE)
        self.assertTrue(
            ChatbotHandoff.objects.filter(trigger=ChatbotHandoff.Trigger.AI_UNAVAILABLE).exists()
        )

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_a_cache_outage_does_not_duplicate_or_corrupt(self, send):
        """
        Redis is down: the cooldown claim fails open and state stays consistent.

        Failing open is the deliberate choice -- a duplicate reply is a far
        smaller problem than a bot that goes silent because a cache restarted --
        and the *durable* duplicate protection is the session's own high-water
        mark, which is in PostgreSQL and unaffected.
        """
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._rule("Pricing", "price", "Rs.999/month.", cooldown_seconds=600)
        self._publish()

        with patch("django.core.cache.cache.add", side_effect=ConnectionError("redis is gone")):
            self.post_webhook(body="price?", message_id="wamid.rc1")

        self.assertIn("Rs.999/month.", self.bodies())
        session = self.session()
        self.assertEqual(session.state, ChatbotSession.State.BOT_ACTIVE)
        self.assertEqual(session.last_processed_message_id, Message.objects.filter(
            direction=Message.Direction.INBOUND).first().pk)


# ---------------------------------------------------------------------------
# Test 12: several messages in quick succession
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class OrderingTests(ChatbotTestCase):
    slug = "chatbot-ordering"

    def test_three_messages_in_a_row_are_each_answered_once_in_order(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._rule("Pricing", "price", "Rs.999/month.", priority=1)
        self._rule("Refund", "refund", "Refunds take 5-7 days.", priority=2)
        self._rule("Delivery", "delivery", "We deliver in 1-3 days.", priority=3)
        self._publish()

        self.post_webhook(body="what is your price?", message_id="wamid.o1")
        self.post_webhook(body="and the refund policy?", message_id="wamid.o2")
        self.post_webhook(body="how fast is delivery?", message_id="wamid.o3")

        self.assertEqual(
            self.bodies(),
            ["Rs.999/month.", "Refunds take 5-7 days.", "We deliver in 1-3 days."],
        )

    def test_a_runaway_reply_burst_is_stopped(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._rule("Anything", "", "Reply.", match_type=ChatbotRule.MatchType.ANY)
        self._publish()

        for index in range(engine.MAX_REPLIES_PER_MINUTE + 3):
            self.post_webhook(body=f"message {index}", message_id=f"wamid.burst.{index}")

        self.assertLessEqual(len(self.bot_messages()), engine.MAX_REPLIES_PER_MINUTE)


# ---------------------------------------------------------------------------
# Test 8: tenant isolation
# ---------------------------------------------------------------------------

class TenantIsolationTests(TestCase):
    """
    Tenant A must not see, touch or send as Tenant B. Asserted through the API,
    because that is where a leak would actually happen.
    """

    def setUp(self):
        cache.clear()
        self.tenant_a = Tenant.objects.create(name="A", slug="iso-a", kind=Tenant.Kind.CUSTOMER)
        self.tenant_b = Tenant.objects.create(name="B", slug="iso-b", kind=Tenant.Kind.CUSTOMER)
        self.user_a = get_user_model().objects.create_user(
            username="iso-a-owner", password="x", tenant=self.tenant_a, credits=100,
        )
        self.user_b = get_user_model().objects.create_user(
            username="iso-b-owner", password="x", tenant=self.tenant_b, credits=100,
        )
        self.account_a = self._account(self.tenant_a, self.user_a, "A line")
        self.account_b = self._account(self.tenant_b, self.user_b, "B line")
        self.bot_a = Chatbot.objects.create(
            tenant=self.tenant_a, whatsapp_account=self.account_a, name="A bot", created_by=self.user_a,
        )
        self.bot_b = Chatbot.objects.create(
            tenant=self.tenant_b, whatsapp_account=self.account_b, name="B bot", created_by=self.user_b,
        )
        self.client_a = APIClient()
        self.client_a.force_authenticate(self.user_a)

    def _account(self, tenant, user, name):
        account = WhatsAppAccount(
            tenant=tenant, user=user, name=name, phone_number_id=f"pn-{tenant.slug}",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        account.set_access_token("token")
        account.save()
        return account

    def test_the_bot_list_shows_only_our_own(self):
        response = self.client_a.get("/api/v1/chatbot/bots/")
        self.assertEqual(response.status_code, 200)
        names = [row["name"] for row in response.data["results"]]
        self.assertEqual(names, ["A bot"])

    def test_another_tenants_bot_is_not_found_rather_than_forbidden(self):
        """404, never 403 -- a 403 would confirm the object exists."""
        for method, path in (
            ("get", f"/api/v1/chatbot/bots/{self.bot_b.pk}/"),
            ("patch", f"/api/v1/chatbot/bots/{self.bot_b.pk}/"),
            ("delete", f"/api/v1/chatbot/bots/{self.bot_b.pk}/"),
        ):
            response = getattr(self.client_a, method)(path, {}, format="json")
            self.assertEqual(response.status_code, 404, f"{method} {path} leaked a {response.status_code}")

    def test_publishing_another_tenants_bot_is_not_found(self):
        response = self.client_a.post(f"/api/v1/chatbot/bots/{self.bot_b.pk}/publish/", {}, format="json")
        self.assertEqual(response.status_code, 404)

    def test_a_bot_cannot_be_attached_to_another_tenants_number(self):
        response = self.client_a.post("/api/v1/chatbot/bots/", {
            "name": "Sneaky", "whatsapp_account": self.account_b.pk,
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("whatsapp_account", response.data)

    def test_a_rule_cannot_be_attached_to_another_tenants_bot(self):
        response = self.client_a.post("/api/v1/chatbot/rules/", {
            "chatbot": self.bot_b.pk, "name": "Sneaky", "match_type": "CONTAINS", "pattern": "x",
            "actions": [{"type": "SEND_MESSAGE", "body": "hello"}],
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_an_action_cannot_name_another_tenants_knowledge_base(self):
        base_b = KnowledgeBase.objects.create(tenant=self.tenant_b, name="B docs")
        response = self.client_a.post("/api/v1/chatbot/rules/", {
            "chatbot": self.bot_a.pk, "name": "Leak", "match_type": "CONTAINS", "pattern": "x",
            "actions": [{"type": "AI_REPLY", "knowledge_base_id": base_b.pk}],
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_a_flow_cannot_transfer_to_another_tenants_bot(self):
        response = self.client_a.put(f"/api/v1/chatbot/bots/{self.bot_a.pk}/flow/", {
            "flow": {"nodes": [
                {"id": "start", "type": "START", "next": "t"},
                {"id": "t", "type": "END", "actions": []},
            ]},
        }, format="json")
        self.assertEqual(response.status_code, 200)

        response = self.client_a.post("/api/v1/chatbot/rules/", {
            "chatbot": self.bot_a.pk, "name": "Transfer", "match_type": "ANY", "pattern": "",
            "actions": [{"type": "TRANSFER_CHATBOT", "chatbot_id": self.bot_b.pk}],
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_conversations_are_scoped_to_the_tenant(self):
        contact_b = Contact.objects.create(tenant=self.tenant_b, phone="+919000000002", created_by=self.user_b)
        conversation_b = Conversation.objects.create(
            tenant=self.tenant_b, whatsapp_account=self.account_b, contact=contact_b,
        )
        session_b = ChatbotSession.objects.create(
            tenant=self.tenant_b, conversation=conversation_b, chatbot=self.bot_b,
        )

        listing = self.client_a.get("/api/v1/chatbot/conversations/")
        self.assertEqual(listing.data["count"], 0)

        for path in ("", "messages/", "trace/", "handoffs/"):
            response = self.client_a.get(f"/api/v1/chatbot/conversations/{session_b.pk}/{path}")
            self.assertEqual(response.status_code, 404, f"{path} leaked a {response.status_code}")

        for path in ("takeover/", "resume-bot/", "reply/", "close/"):
            response = self.client_a.post(
                f"/api/v1/chatbot/conversations/{session_b.pk}/{path}", {"body": "hi"}, format="json",
            )
            self.assertEqual(response.status_code, 404, f"{path} leaked a {response.status_code}")

    def test_analytics_never_counts_another_tenant(self):
        contact_b = Contact.objects.create(tenant=self.tenant_b, phone="+919000000003", created_by=self.user_b)
        conversation_b = Conversation.objects.create(
            tenant=self.tenant_b, whatsapp_account=self.account_b, contact=contact_b,
        )
        ChatbotSession.objects.create(tenant=self.tenant_b, conversation=conversation_b, chatbot=self.bot_b)

        response = self.client_a.get("/api/v1/chatbot/overview/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total_conversations"], 0)
        self.assertEqual([bot["name"] for bot in response.data["bots"]], ["A bot"])

    def test_an_unauthenticated_caller_gets_nothing(self):
        anonymous = APIClient()
        for path in ("/api/v1/chatbot/bots/", "/api/v1/chatbot/overview/", "/api/v1/chatbot/conversations/"):
            response = anonymous.get(path)
            self.assertIn(response.status_code, (401, 403), f"{path} was reachable anonymously")


# ---------------------------------------------------------------------------
# Business hours, variables, flows, versioning
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class BusinessHoursTests(ChatbotTestCase):
    slug = "chatbot-hours"

    def test_outside_hours_the_bot_says_so_and_answers_nothing_else(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.working_hours_enabled = True
        self.chatbot.save()
        self._rule("Pricing", "price", "Rs.999/month.")
        # Closed every day, so "now" is always outside.
        for weekday in range(7):
            ChatbotWorkingHours.objects.create(chatbot=self.chatbot, weekday=weekday, is_open=False)
        self._publish()

        self.post_webhook(body="what is your price?")

        bodies = " ".join(self.bodies())
        self.assertIn("currently offline", bodies)
        self.assertNotIn("Rs.999/month.", bodies)

    def test_inside_hours_the_bot_answers_normally(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.working_hours_enabled = True
        self.chatbot.save()
        self._rule("Pricing", "price", "Rs.999/month.")
        for weekday in range(7):
            ChatbotWorkingHours.objects.create(
                chatbot=self.chatbot, weekday=weekday, is_open=True,
                opens_at=time(0, 0), closes_at=time(23, 59),
            )
        self._publish()

        self.post_webhook(body="what is your price?")

        self.assertIn("Rs.999/month.", self.bodies())


@patch("apps.messaging.services.ConnectionManager.send_text_message")
class VariableAndFlowTests(ChatbotTestCase):
    slug = "chatbot-flow"

    def test_a_question_flow_remembers_the_answer(self, send):
        """'I want shoes' -> 'what size?' -> '9' must mean shoe size 9."""
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        ChatbotRule.objects.create(
            chatbot=self.chatbot, name="Shoes", match_type=ChatbotRule.MatchType.CONTAINS,
            pattern="shoes", priority=1,
            actions=[{"type": "ASK_QUESTION", "body": "Sure. What size do you need?", "save_to": "shoe_size"}],
        )
        ChatbotRule.objects.create(
            chatbot=self.chatbot, name="Confirm", match_type=ChatbotRule.MatchType.ANY,
            pattern="", priority=50,
            actions=[{"type": "SEND_MESSAGE", "body": "Size {{shoe_size}} it is - checking stock."}],
        )
        self._publish()

        self.post_webhook(body="I want to buy shoes", message_id="wamid.v1")
        self.assertIn("Sure. What size do you need?", self.bodies())

        self.post_webhook(body="9", message_id="wamid.v2")

        session = self.session()
        self.assertEqual(session.variables.get("shoe_size"), "9")
        self.assertIn("Size 9 it is - checking stock.", self.bodies())

    def test_customer_name_renders_in_a_reply(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = "Hi {{first_name}}, welcome to Acme."
        self.chatbot.save()
        self._publish()

        self.post_webhook(body="hello")

        self.assertIn("Hi Riya, welcome to Acme.", self.bodies())

    def test_a_flow_walks_a_condition_and_answers(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.bot_type = Chatbot.BotType.RULE_BASED
        self.chatbot.save()

        draft = services.draft_version(self.chatbot)
        draft.flow = {"nodes": [
            {"id": "start", "type": "START", "next": "check", "actions": [], "position": {"x": 0, "y": 0}},
            {"id": "check", "type": "CONDITION", "actions": [], "next": "",
             "condition": {"field": "last_message", "operator": "contains", "name": "", "value": "urgent"},
             "true_next": "hot", "false_next": "cold", "position": {"x": 0, "y": 0}},
            {"id": "hot", "type": "MESSAGE", "next": "", "position": {"x": 0, "y": 0},
             "actions": [{"type": "SEND_MESSAGE", "body": "Marking this urgent.", "buttons": []}]},
            {"id": "cold", "type": "MESSAGE", "next": "", "position": {"x": 0, "y": 0},
             "actions": [{"type": "SEND_MESSAGE", "body": "Thanks, we'll get back to you.", "buttons": []}]},
        ]}
        draft.save()
        self._publish()

        self.post_webhook(body="this is urgent please help", message_id="wamid.fl1")

        self.assertIn("Marking this urgent.", self.bodies())

    def test_a_tag_action_tags_the_contact(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        ChatbotRule.objects.create(
            chatbot=self.chatbot, name="Interested", match_type=ChatbotRule.MatchType.CONTAINS,
            pattern="buy", priority=1,
            actions=[
                {"type": "ADD_TAG", "tag": "Hot lead"},
                {"type": "SEND_MESSAGE", "body": "Great - let me help you.", "buttons": []},
            ],
        )
        self._publish()

        self.post_webhook(body="I want to buy")

        contact = Contact.objects.get(tenant=self.tenant)
        self.assertIn("Hot lead", list(contact.tags.values_list("name", flat=True)))


@patch("apps.messaging.services.ConnectionManager.send_media_message")
@patch("apps.messaging.services.ConnectionManager.send_text_message")
class MediaResponseTests(ChatbotTestCase):
    slug = "chatbot-media"

    def test_the_bot_sends_a_document(self, send_text, send_media):
        send_text.side_effect = unique_sends("wamid.txt")
        send_media.side_effect = unique_sends("wamid.doc")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        ChatbotRule.objects.create(
            chatbot=self.chatbot, name="Brochure", match_type=ChatbotRule.MatchType.CONTAINS,
            pattern="brochure", priority=1,
            actions=[{
                "type": "SEND_MEDIA", "media_url": "https://cdn.example.com/brochure.pdf",
                "media_type": "DOCUMENT", "filename": "brochure.pdf", "caption": "Here you go.",
            }],
        )
        self._publish()

        self.post_webhook(body="send me the brochure")

        self.assertTrue(send_media.called, "the provider's media call must actually be made")
        message = Message.objects.filter(
            direction=Message.Direction.OUTBOUND, message_type=Message.MessageType.DOCUMENT,
        ).first()
        self.assertIsNotNone(message)
        self.assertEqual(message.media_url, "https://cdn.example.com/brochure.pdf")


@patch("apps.messaging.services.ConnectionManager.send_text_message")
class VersioningTests(ChatbotTestCase):
    slug = "chatbot-versions"

    def test_a_conversation_part_way_through_a_flow_keeps_its_version(self, send):
        """
        A publish must not move a customer who is standing on a node.

        The node they are parked at may not exist in the new graph, so the
        session keeps the snapshot it started on until the flow lets go of it.
        """
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.bot_type = Chatbot.BotType.RULE_BASED
        self.chatbot.save()
        ChatbotRule.objects.create(
            chatbot=self.chatbot, name="Shoes", match_type=ChatbotRule.MatchType.CONTAINS,
            pattern="shoes", priority=1,
            actions=[{"type": "ASK_QUESTION", "body": "What size?", "save_to": "shoe_size"}],
        )
        first = self._publish()

        self.post_webhook(body="I want shoes", message_id="wamid.pin1")
        session = self.session()
        self.assertEqual(session.version_id, first.pk)
        self.assertTrue(session.variables.get("__awaiting"))

        second = self._publish()
        self.assertNotEqual(second.pk, first.pk)

        self.post_webhook(body="9", message_id="wamid.pin2")

        # The answer was still taken against the version they started on.
        self.assertEqual(self.session().variables.get("shoe_size"), "9")

    def test_editing_a_rule_does_not_change_the_live_bot_until_publish(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        rule = self._rule("Pricing", "price", "Old answer: Rs.999.")
        self._publish()

        rule.actions = [{"type": "SEND_MESSAGE", "body": "New answer: Rs.1499.", "buttons": []}]
        rule.save()

        self.post_webhook(body="price?", message_id="wamid.pv1")
        self.assertIn("Old answer: Rs.999.", self.bodies())

        self._publish()
        self.post_webhook(body="price?", message_id="wamid.pv2")
        self.assertIn("New answer: Rs.1499.", self.bodies())

    def test_rollback_puts_the_previous_version_back(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        rule = self._rule("Pricing", "price", "Version one.")
        first = self._publish()

        rule.actions = [{"type": "SEND_MESSAGE", "body": "Version two.", "buttons": []}]
        rule.save()
        self._publish()

        services.rollback(self.chatbot, first, user=self.user)
        self.post_webhook(body="price?", message_id="wamid.rb1")

        self.assertIn("Version one.", self.bodies())

    def test_only_one_bot_may_be_published_on_a_number(self, send):
        send.side_effect = unique_sends("wamid.out")
        self._rule("Pricing", "price", "Rs.999.")
        self._publish()

        second = self._chatbot(name="Second Bot")
        ChatbotRule.objects.create(
            chatbot=second, name="Anything", match_type=ChatbotRule.MatchType.ANY, pattern="",
            actions=[{"type": "SEND_MESSAGE", "body": "second bot", "buttons": []}],
        )

        client = APIClient()
        client.force_authenticate(self.user)
        response = client.post(f"/api/v1/chatbot/bots/{second.pk}/publish/", {}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("already published", response.data["detail"])

    def test_a_paused_bot_answers_nothing(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._rule("Pricing", "price", "Rs.999.")
        self._publish()
        self.chatbot.status = Chatbot.Status.PAUSED
        self.chatbot.save(update_fields=["status"])

        self.post_webhook(body="price?")

        self.assertEqual(self.bodies(), [])


# ---------------------------------------------------------------------------
# Backward compatibility -- the old ladder must be untouched
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class BackwardCompatibilityTests(ChatbotTestCase):
    slug = "chatbot-compat"

    def test_legacy_automation_still_answers_when_no_chatbot_is_published(self, send):
        send.side_effect = unique_sends("wamid.out")
        from apps.automation.models import AutomationRule

        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Legacy pricing",
            trigger_type=AutomationRule.Trigger.KEYWORD, keyword="price",
            message_body="Legacy answer: Rs.499.",
        )
        # The chatbot exists but is a draft, so the old ladder owns the number.

        self.post_webhook(body="what is your price?")

        self.assertIn("Legacy answer: Rs.499.", self.bodies())

    def test_a_published_chatbot_takes_over_from_legacy_rules(self, send):
        send.side_effect = unique_sends("wamid.out")
        from apps.automation.models import AutomationRule

        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Legacy pricing",
            trigger_type=AutomationRule.Trigger.KEYWORD, keyword="price",
            message_body="Legacy answer: Rs.499.",
        )
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._rule("Pricing", "price", "Bot answer: Rs.999.")
        self._publish()

        self.post_webhook(body="what is your price?")

        bodies = self.bodies()
        self.assertIn("Bot answer: Rs.999.", bodies)
        self.assertNotIn("Legacy answer: Rs.499.", bodies)

    def test_defer_to_automation_lets_the_old_rules_go_first(self, send):
        send.side_effect = unique_sends("wamid.out")
        from apps.automation.models import AutomationRule

        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Legacy pricing",
            trigger_type=AutomationRule.Trigger.KEYWORD, keyword="price",
            message_body="Legacy answer: Rs.499.",
        )
        self.chatbot.welcome_message = ""
        self.chatbot.defer_to_automation = True
        self.chatbot.save()
        self._rule("Pricing", "price", "Bot answer: Rs.999.")
        self._publish()

        self.post_webhook(body="what is your price?")

        self.assertIn("Legacy answer: Rs.499.", self.bodies())

    def test_an_opt_out_is_still_answered_by_compliance_alone(self, send):
        """Consent comes before everything, chatbot included."""
        send.side_effect = unique_sends("wamid.out")
        self._rule("Anything", "", "Bot reply.", match_type=ChatbotRule.MatchType.ANY)
        self._publish()

        self.post_webhook(body="STOP")

        self.assertNotIn("Bot reply.", self.bodies())
        contact = Contact.objects.get(tenant=self.tenant)
        self.assertFalse(contact.subscribed)


# ---------------------------------------------------------------------------
# Test 3: a knowledge-base question gets a grounded answer
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class KnowledgeAnswerTests(ChatbotTestCase):
    """
    The AI path, end to end from the webhook.

    Only the model call itself is stubbed. Retrieval, the relevance gate, the
    prompt assembly, the send and the provenance row are all the real ones --
    a test that stubbed retrieval too would prove the plumbing and say nothing
    about whether the answer was grounded in anything.
    """

    slug = "chatbot-rag"

    def setUp(self):
        super().setUp()
        self.chatbot.welcome_message = ""
        self.chatbot.bot_type = Chatbot.BotType.AI_BASED
        self.chatbot.ai_enabled = True
        self.chatbot.save()

        config = AIAgentConfig.objects.create(tenant=self.tenant, is_enabled=True)
        config.set_api_key("sk-ant-test")
        config.save()

        self.base = KnowledgeBase.objects.create(tenant=self.tenant, name="Policies", is_default=True)
        document = KnowledgeDocument.objects.create(
            tenant=self.tenant, knowledge_base=self.base, title="Refund policy",
            content=(
                "Our refund policy allows returns within 30 days of delivery. "
                "Refunds are processed to the original payment method within 5 to 7 working days. "
                "Items must be unused and in their original packaging."
            ),
        )
        from apps.ai import knowledge as knowledge_module

        knowledge_module.rebuild(document)
        self.chatbot.knowledge_base = self.base
        self.chatbot.save(update_fields=["knowledge_base"])
        self._publish()

    def test_a_knowledge_question_is_answered_from_the_documents(self, send):
        send.side_effect = unique_sends("wamid.out")
        answer = "You can return anything within 30 days, and refunds reach you in 5 to 7 working days."

        with patch("apps.ai.services._ask") as ask:
            ask.return_value = SimpleNamespace(
                text=answer, input_tokens=120, output_tokens=25, latency_ms=430,
            )
            self.post_webhook(body="what is your refund policy?", message_id="wamid.rag1")

            self.assertTrue(ask.called, "the model was never called")
            system_prompt = ask.call_args.args[1]

        # The retrieved passage really was put in front of the model.
        self.assertIn("30 days", system_prompt)
        self.assertIn("5 to 7 working days", system_prompt)

        self.assertIn(answer, self.bodies())

        # And the provenance row says where it came from.
        from apps.ai.models import KnowledgeAnswer

        record = KnowledgeAnswer.objects.filter(tenant=self.tenant, is_test=False).first()
        self.assertIsNotNone(record)
        self.assertTrue(record.answered)
        self.assertGreater(record.relevance, 0)
        self.assertTrue(any(source["title"] == "Refund policy" for source in record.sources))

    def test_a_question_the_documents_do_not_cover_is_not_answered(self, send):
        """The relevance gate: below the floor, the model is never called at all."""
        send.side_effect = unique_sends("wamid.out")

        with patch("apps.ai.services._ask") as ask:
            self.post_webhook(body="do you sell helicopters or submarines", message_id="wamid.rag2")
            self.assertFalse(ask.called, "the model was called on a question with no relevant material")

        # The customer is still spoken to rather than left in silence.
        self.assertTrue(self.bot_messages(), "the customer got nothing at all")

    def test_a_reply_longer_than_the_limit_is_trimmed_at_a_sentence(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.max_ai_response_chars = 60
        self.chatbot.save(update_fields=["max_ai_response_chars"])
        self._publish()

        with patch("apps.ai.services._ask") as ask:
            ask.return_value = SimpleNamespace(
                text=(
                    "Returns are accepted within 30 days. "
                    "Refunds reach the original payment method in 5 to 7 working days. "
                    "Items must be unused and in the original packaging."
                ),
                input_tokens=1, output_tokens=1, latency_ms=1,
            )
            self.post_webhook(body="what is your refund policy?", message_id="wamid.rag3")

        sent = [body for body in self.bodies() if "Returns are accepted" in body]
        self.assertTrue(sent, "nothing was sent")
        self.assertLessEqual(len(sent[0]), 65, f"reply was not trimmed: {sent[0]!r}")
        # Trimmed at a sentence, not mid-word.
        self.assertTrue(sent[0].endswith(".") or sent[0].endswith("..."), sent[0])


# ---------------------------------------------------------------------------
# Interactive replies, and what happens on a connection that cannot show them
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class InteractiveReplyTests(ChatbotTestCase):
    slug = "chatbot-buttons"

    def _button_rule(self):
        ChatbotRule.objects.create(
            chatbot=self.chatbot, name="Menu", match_type=ChatbotRule.MatchType.CONTAINS,
            pattern="help", priority=1,
            actions=[{
                "type": "SEND_MESSAGE",
                "body": "How can we help?",
                "buttons": [
                    {"title": "Pricing"},
                    {"title": "Products"},
                    {"title": "Talk to Agent"},
                ],
            }],
        )

    @patch("apps.messaging.services.ConnectionManager.send_interactive_message")
    def test_a_rule_can_send_tappable_buttons(self, send_interactive, send):
        send.side_effect = unique_sends("wamid.txt")
        send_interactive.side_effect = unique_sends("wamid.int")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._button_rule()
        self._publish()

        self.post_webhook(body="I need help", message_id="wamid.b1")

        message = Message.objects.filter(
            direction=Message.Direction.OUTBOUND, body="How can we help?",
        ).first()
        self.assertIsNotNone(message, "the button message was never created")
        self.assertEqual(len(message.buttons), 3)
        self.assertEqual([button["title"] for button in message.buttons],
                         ["Pricing", "Products", "Talk to Agent"])
        # Ids are derived from the labels and are what a BUTTON rule matches on.
        self.assertTrue(all(button.get("id") for button in message.buttons))

    @patch("apps.messaging.services.ConnectionManager.supports_interactive_buttons", return_value=False)
    def test_buttons_degrade_to_a_numbered_list_where_they_cannot_be_shown(self, supports, send):
        """
        A QR-linked number cannot deliver real buttons, so the platform writes
        them out as a numbered list instead of failing or dropping them. That
        degradation is the messaging layer's, not the chatbot's -- this asserts
        the chatbot's message actually reaches it intact, with every label
        still on it.

        The connection is stubbed at the capability rather than by moving the
        account to QR, because ``supports_interactive_buttons`` *is* the
        condition being described. Switching ``connection_method`` would also
        switch the account's provider, and the QR provider is built once at
        import time from deployment settings that no test sets -- so the
        account would end up on ``NotConfiguredProvider`` and the inbound
        webhook would be rejected before the chatbot ever ran. That failure
        would look like this assertion, while saying nothing about buttons.
        """
        send.side_effect = unique_sends("wamid.txt")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._button_rule()
        self._publish()

        self.post_webhook(body="I need help", message_id="wamid.b2")

        self.assertTrue(send.called, "nothing was sent at all")
        body = send.call_args.args[2] if len(send.call_args.args) > 2 else send.call_args.kwargs.get("body", "")
        for label in ("Pricing", "Products", "Talk to Agent"):
            self.assertIn(label, body, f"'{label}' was dropped rather than degraded")

    @patch("apps.messaging.services.ConnectionManager.send_interactive_message")
    def test_tapping_a_button_reaches_the_rule_that_answers_it(self, send_interactive, send):
        """The full round trip: we offer options, they tap one, a BUTTON rule answers."""
        send.side_effect = unique_sends("wamid.txt")
        send_interactive.side_effect = unique_sends("wamid.int")
        self.chatbot.welcome_message = ""
        self.chatbot.save()
        self._button_rule()
        self._publish()

        self.post_webhook(body="I need help", message_id="wamid.b3")
        offered = Message.objects.filter(
            direction=Message.Direction.OUTBOUND, body="How can we help?",
        ).first()
        pricing_id = next(b["id"] for b in offered.buttons if b["title"] == "Pricing")

        ChatbotRule.objects.create(
            chatbot=self.chatbot, name="Pricing tap", match_type=ChatbotRule.MatchType.BUTTON,
            pattern=pricing_id, priority=0,
            actions=[{"type": "SEND_MESSAGE", "body": "Plans start at Rs.999.", "buttons": []}],
        )
        self._publish()

        self.post_webhook(
            message_id="wamid.b4",
            extra={"type": "interactive", "interactive": {
                "type": "button_reply", "button_reply": {"id": pricing_id, "title": "Pricing"},
            }},
        )

        self.assertIn("Plans start at Rs.999.", self.bodies())


@patch("apps.messaging.services.ConnectionManager.send_text_message")
class ReplyLimitTests(ChatbotTestCase):
    """The ceiling on how much one bot may say in one conversation."""

    slug = "chatbot-limit"

    def test_the_limit_counts_rule_replies_not_only_ai_ones(self, send):
        """
        A limit that counted model answers but not rule answers would not mean
        what the setting says -- and a bot looping on a catch-all rule is
        exactly what somebody sets this for.
        """
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.handoff_conditions = {"max_bot_replies": 2}
        self.chatbot.save()
        self._rule("Anything", "", "Rule reply.", match_type=ChatbotRule.MatchType.ANY)
        self._publish()

        for index in range(4):
            self.post_webhook(body=f"message {index}", message_id=f"wamid.lim.{index}")

        replies = [body for body in self.bodies() if body == "Rule reply."]
        self.assertLessEqual(len(replies), 2, f"the bot spoke {len(replies)} times past a limit of 2")
        self.assertEqual(self.session().state, ChatbotSession.State.HUMAN_ACTIVE)
        self.assertTrue(
            ChatbotHandoff.objects.filter(trigger=ChatbotHandoff.Trigger.REPLY_LIMIT).exists(),
            "hitting the limit should hand the conversation to a person",
        )

    def test_no_limit_means_no_limit(self, send):
        send.side_effect = unique_sends("wamid.out")
        self.chatbot.welcome_message = ""
        self.chatbot.handoff_conditions = {"max_bot_replies": 0}
        self.chatbot.save()
        self._rule("Anything", "", "Rule reply.", match_type=ChatbotRule.MatchType.ANY)
        self._publish()

        for index in range(4):
            self.post_webhook(body=f"message {index}", message_id=f"wamid.nolim.{index}")

        self.assertEqual(len([body for body in self.bodies() if body == "Rule reply."]), 4)
        self.assertEqual(self.session().state, ChatbotSession.State.BOT_ACTIVE)


# ---------------------------------------------------------------------------
# Per-option branching: one question, three destinations
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_interactive_message")
@patch("apps.messaging.services.ConnectionManager.send_text_message")
class QuestionRouteTests(ChatbotTestCase):
    """
    A question's buttons are edges in the graph, not a lookup somebody has to
    build out of CONDITION nodes afterwards.

    Driven from the webhook so the whole chain is under test: the tap arrives
    as a provider payload, ``inbox.services`` flattens it to a button id, the
    session resumes at the node that asked, and ``flow._answer_target`` picks
    the branch. A test that called ``resume`` directly would prove none of
    that.
    """

    slug = "chatbot-routes"

    MENU = {
        "id": "menu", "type": "QUESTION", "next": "other", "position": {"x": 0, "y": 0},
        "actions": [{
            "type": "ASK_QUESTION",
            "body": "How can we help?",
            "save_to": "topic",
            "buttons": [
                {"id": "pricing", "title": "Pricing"},
                {"id": "products", "title": "Products"},
                {"id": "support", "title": "Support"},
            ],
        }],
        "routes": [
            {"value": "pricing", "next": "say_pricing"},
            {"value": "products", "next": "say_products"},
        ],
    }

    def _menu_flow(self):
        message = lambda node_id, body: {  # noqa: E731 - a table of four near-identical nodes
            "id": node_id, "type": "MESSAGE", "next": "", "position": {"x": 0, "y": 0},
            "actions": [{"type": "SEND_MESSAGE", "body": body, "buttons": []}],
        }
        self.chatbot.welcome_message = ""
        self.chatbot.bot_type = Chatbot.BotType.RULE_BASED
        self.chatbot.save()

        draft = services.draft_version(self.chatbot)
        draft.flow = {"nodes": [
            {"id": "start", "type": "START", "next": "menu", "actions": [], "position": {"x": 0, "y": 0}},
            self.MENU,
            message("say_pricing", "Our plans start at Rs.999."),
            message("say_products", "Here is our catalogue."),
            message("other", "Sorry, I did not catch that."),
        ]}
        draft.save()
        self._publish()

    def _tap(self, button_id, title, message_id):
        return self.post_webhook(
            message_id=message_id,
            extra={
                "type": "interactive",
                "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": title}},
            },
        )

    def test_a_tapped_button_takes_its_own_branch(self, send, send_interactive):
        send.side_effect = unique_sends("wamid.txt")
        send_interactive.side_effect = unique_sends("wamid.int")
        self._menu_flow()

        self.post_webhook(body="hello", message_id="wamid.r1")
        self.assertIn("How can we help?", self.bodies())

        self._tap("pricing", "Pricing", "wamid.r2")

        self.assertIn("Our plans start at Rs.999.", self.bodies())
        self.assertNotIn("Here is our catalogue.", self.bodies())
        self.assertNotIn("Sorry, I did not catch that.", self.bodies())

    def test_each_button_reaches_a_different_node(self, send, send_interactive):
        send.side_effect = unique_sends("wamid.txt")
        send_interactive.side_effect = unique_sends("wamid.int")
        self._menu_flow()

        self.post_webhook(body="hello", message_id="wamid.r3")
        self._tap("products", "Products", "wamid.r4")

        self.assertIn("Here is our catalogue.", self.bodies())
        self.assertNotIn("Our plans start at Rs.999.", self.bodies())

    def test_an_unrouted_answer_follows_the_default_path(self, send, send_interactive):
        """
        Free text against a button question is normal, and must land on the
        node the author wired to ``next`` rather than stopping the flow dead.
        'support' is deliberately left unrouted here too, so the same path
        covers "a choice with no branch of its own".
        """
        send.side_effect = unique_sends("wamid.txt")
        send_interactive.side_effect = unique_sends("wamid.int")
        self._menu_flow()

        self.post_webhook(body="hello", message_id="wamid.r5")
        self.post_webhook(body="something else entirely", message_id="wamid.r6")

        self.assertIn("Sorry, I did not catch that.", self.bodies())

    def test_the_answer_is_still_saved_when_a_branch_is_taken(self, send, send_interactive):
        """Routing must not cost the author the variable the question saves."""
        send.side_effect = unique_sends("wamid.txt")
        send_interactive.side_effect = unique_sends("wamid.int")
        self._menu_flow()

        self.post_webhook(body="hello", message_id="wamid.r7")
        self._tap("pricing", "Pricing", "wamid.r8")

        self.assertEqual(self.session().variables.get("topic"), "Pricing")


# ---------------------------------------------------------------------------
# What starts a flow
# ---------------------------------------------------------------------------

@patch("apps.messaging.services.ConnectionManager.send_text_message")
class FlowTriggerTests(ChatbotTestCase):
    """
    The Start node's trigger, from the graph through the engine.

    The property under test is the one the feature exists for: a flow with a
    trigger runs on the messages that match it and stays out of the way of the
    rest, and a flow with no trigger at all behaves exactly as it did before
    triggers existed -- which is what every graph already saved looks like.
    """

    slug = "chatbot-trigger"

    def _flow(self, trigger, *, body="You are in the flow."):
        start = {"id": "start", "type": "START", "next": "say", "actions": [], "position": {"x": 0, "y": 0}}
        if trigger is not None:
            start["trigger"] = trigger
        draft = services.draft_version(self.chatbot)
        draft.flow = {"nodes": [
            start,
            {"id": "say", "type": "MESSAGE", "next": "", "position": {"x": 0, "y": 0},
             "actions": [{"type": "SEND_MESSAGE", "body": body, "buttons": []}]},
        ]}
        draft.save()

        self.chatbot.welcome_message = ""
        self.chatbot.bot_type = Chatbot.BotType.RULE_BASED
        self.chatbot.save()
        self._publish()

    def test_a_graph_saved_without_a_trigger_still_runs_on_any_message(self, send):
        """Backwards compatibility, and the reason a missing trigger is not an error."""
        send.side_effect = unique_sends("wamid.out")
        self._flow(None)

        self.post_webhook(body="anything at all", message_id="wamid.t1")

        self.assertIn("You are in the flow.", self.bodies())

    def test_a_keyword_trigger_runs_the_flow_only_on_its_keyword(self, send):
        send.side_effect = unique_sends("wamid.out")
        self._flow({"type": "KEYWORD", "pattern": "refund, return"})

        self.post_webhook(body="how much is delivery?", message_id="wamid.t2")
        self.assertNotIn("You are in the flow.", self.bodies())

        self.post_webhook(body="I need a refund please", message_id="wamid.t3")
        self.assertIn("You are in the flow.", self.bodies())

    def test_a_keyword_trigger_matches_whole_words_only(self, send):
        """The same rule ``matching`` applies to a keyword rule, because it is the same matcher."""
        send.side_effect = unique_sends("wamid.out")
        self._flow({"type": "KEYWORD", "pattern": "price"})

        self.post_webhook(body="what a surprise", message_id="wamid.t4")

        self.assertNotIn("You are in the flow.", self.bodies())

    def test_a_message_the_trigger_declines_falls_through_to_the_fallback(self, send):
        send.side_effect = unique_sends("wamid.out")
        self._flow({"type": "CONTAINS", "pattern": "refund"})

        self.post_webhook(body="hello there", message_id="wamid.t5")

        self.assertIn("Sorry, I didn't get that.", self.bodies())

    def test_a_declined_trigger_says_so_in_the_trail(self, send):
        send.side_effect = unique_sends("wamid.out")
        self._flow({"type": "KEYWORD", "pattern": "refund"})

        self.post_webhook(body="hello there", message_id="wamid.t6")

        skipped = [
            event for event in self.trace_steps(ChatbotEvent.Step.FLOW)
            if "refund" in (event.detail or "")
        ]
        self.assertTrue(skipped, [event.detail for event in self.trace_steps(ChatbotEvent.Step.FLOW)])

    def test_a_new_conversation_trigger_fires_once(self, send):
        send.side_effect = unique_sends("wamid.out")
        self._flow({"type": "NEW_CONVERSATION", "pattern": ""})

        self.post_webhook(body="hello", message_id="wamid.t7")
        self.assertEqual(self.bodies().count("You are in the flow."), 1)

        self.post_webhook(body="hello again", message_id="wamid.t8")
        self.assertEqual(self.bodies().count("You are in the flow."), 1)

    def test_a_rule_still_answers_before_a_flow_that_would_also_start(self, send):
        """
        The ladder is unchanged: a trigger decides *whether* the flow may start,
        never *when* it is asked. A rule that claims the message still wins.
        """
        send.side_effect = unique_sends("wamid.out")
        self._flow({"type": "KEYWORD", "pattern": "refund"})
        self._rule("Refunds", "refund", "A rule answered this.", priority=1)
        self._publish()

        self.post_webhook(body="refund please", message_id="wamid.t9")

        self.assertIn("A rule answered this.", self.bodies())
        self.assertNotIn("You are in the flow.", self.bodies())


class TriggerValidationTests(TestCase):
    """``flow.validate`` on the one field a START node carries."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="T", slug="trigger-validation", kind=Tenant.Kind.CUSTOMER)

    def _validate(self, trigger):
        return flow.validate(
            {"nodes": [
                {"id": "start", "type": "START", "next": "say", "trigger": trigger},
                {"id": "say", "type": "MESSAGE", "next": "",
                 "actions": [{"type": "SEND_MESSAGE", "body": "hi", "buttons": []}]},
            ]},
            tenant_id=self.tenant.pk,
        )

    def _start(self, cleaned):
        return next(node for node in cleaned["nodes"] if node["type"] == "START")

    def test_a_missing_trigger_normalises_to_any_message(self):
        cleaned = self._validate(None)
        self.assertEqual(self._start(cleaned)["trigger"]["type"], flow.TriggerType.ANY_MESSAGE)

    def test_an_unknown_trigger_is_refused(self):
        with self.assertRaises(flow.FlowError):
            self._validate({"type": "PAYMENT_RECEIVED"})

    def test_a_keyword_trigger_with_no_keywords_is_refused(self):
        with self.assertRaises(flow.FlowError):
            self._validate({"type": "KEYWORD", "pattern": "  "})

    def test_an_invalid_regular_expression_is_refused_on_save(self):
        with self.assertRaises(flow.FlowError):
            self._validate({"type": "REGEX", "pattern": "(unclosed"})

    def test_a_pattern_left_over_from_another_trigger_is_dropped(self):
        cleaned = self._validate({"type": "ANY_MESSAGE", "pattern": "refund", "case_sensitive": True})
        self.assertEqual(self._start(cleaned)["trigger"], flow.DEFAULT_TRIGGER)

    def test_an_unknown_trigger_type_at_run_time_starts_nothing(self):
        """
        A graph hand-edited in the admin, or one exported by a newer build. It
        must not fall back to "any message": a flow that answers everything when
        it was meant to answer one word is the failure nobody notices in time.
        """
        self.assertFalse(flow.trigger_matches({"type": "FROM_THE_FUTURE"}, text="anything"))

    def test_a_button_trigger_matches_the_id_and_not_the_label(self):
        trigger = {"type": "BUTTON", "pattern": "talk_to_sales"}
        self.assertTrue(flow.trigger_matches(trigger, text="Talk to sales", button_id="talk_to_sales"))
        self.assertFalse(flow.trigger_matches(trigger, text="talk_to_sales", button_id=""))
