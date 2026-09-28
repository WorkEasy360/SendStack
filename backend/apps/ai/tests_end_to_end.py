"""
The whole path, from a webhook arriving to a reply leaving on the right number.

Every test here starts at a stored `WhatsAppWebhookEvent` and ends at an
outbound `Message` row, going through the real inbound receiver, the real
trigger ladder, the real retrieval and the real send helper. Only two things
are stubbed: the model provider (a network call, and the thing under test is
what we ask it and what we do with the answer, not its weights) and the
WhatsApp transport (likewise, and it would message a real person).

The unit tests around these cover each component; what is checked here is the
seams between them, which is where multi-account routing, tenant isolation and
the automation-versus-AI ordering actually live. None of those can be seen
from inside any one component.
"""

import itertools
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.automation.models import AutomationRule
from apps.contacts.models import Contact
from apps.inbox import services as inbox_services
from apps.inbox.models import Conversation
from apps.inbox.signals import inbound_message_recorded
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount, WhatsAppWebhookEvent
from apps.whatsapp.providers.base import SendResult

from . import knowledge, services, status
from .models import AIAgentConfig, KnowledgeBase, KnowledgeDocument
from .providers.base import AIResponse

FAQ = """Our shop is open from nine in the morning until six in the evening, Monday to Saturday.

Delivery anywhere inside Mumbai takes two working days and costs sixty rupees per parcel.

We accept returns within fourteen days of purchase if the item is unused and still in its packaging."""


def _account(tenant, user, name="Line", phone="+15551110000"):
    """One connected WhatsApp number. No AI enrolment step exists, by design."""
    account = WhatsAppAccount(
        tenant=tenant, user=user, name=name, phone_number=phone, phone_number_id=f"pnid-{name}",
        status=WhatsAppAccount.Status.CONNECTED,
    )
    account.set_access_token("token")
    account.set_app_secret("secret")
    account.save()
    return account


def _event(account, *, from_number="15551234567", body="Hi", message_id="wamid.in.1"):
    return WhatsAppWebhookEvent.objects.create(
        account=account, event_type=WhatsAppWebhookEvent.EventType.INBOUND_MESSAGE, provider_event_id=message_id,
        payload={
            "message": {"id": message_id, "from": from_number, "type": "text", "text": {"body": body}},
            "profile_name": "Priya",
        },
        related_message_id=message_id,
    )


def _unique_sends(prefix="wamid.out"):
    counter = itertools.count(1)
    return lambda *args, **kwargs: SendResult(success=True, provider_message_id=f"{prefix}.{next(counter)}")


def _reply(text="We open at nine in the morning."):
    return AIResponse(text=text, input_tokens=120, output_tokens=18, latency_ms=210)


def _deliver(account, body, message_id):
    """
    One inbound message, taken all the way through the receiver.

    `record_inbound_message` writes the rows; the signal is what the webhook
    view sends afterwards and what the automation ladder listens on. Both are
    needed to exercise the real ordering.
    """
    message = inbox_services.record_inbound_message(_event(account, body=body, message_id=message_id))
    inbound_message_recorded.send(
        sender=Message, message=message, is_new_conversation=getattr(message, "is_new_conversation", False),
    )
    return message


@override_settings(OPENAI_API_KEY="sk-deployment", OPENAI_MODEL="gpt-4o-mini", AI_EMBEDDINGS_ENABLED=False)
class EndToEndBase(TestCase):
    """A tenant with one number, one knowledge base and an OpenAI agent."""

    def setUp(self):
        # Started here rather than as a class decorator: a class-level @patch
        # decorates only the test methods defined on that class, so every
        # subclass below would silently send for real.
        transport = patch("apps.messaging.services.ConnectionManager.send_text_message")
        self.mock_send = transport.start()
        self.addCleanup(transport.stop)
        self.mock_send.side_effect = _unique_sends()

        self.tenant = Tenant.objects.create(
            name="Acme", slug=f"e2e-{self._testMethodName}"[:50], kind=Tenant.Kind.CUSTOMER,
        )
        self.owner = get_user_model().objects.create_user(
            username=f"e2e-owner-{self._testMethodName}"[:140], password="x", tenant=self.tenant, credits=500,
        )
        self.account = _account(self.tenant, self.owner)
        self.base = KnowledgeBase.objects.create(tenant=self.tenant, name="General", is_default=True)
        self.config = AIAgentConfig.objects.create(
            tenant=self.tenant, is_enabled=True, provider=AIAgentConfig.Provider.OPENAI, model="",
            knowledge_base=self.base, reply_scope=AIAgentConfig.ReplyScope.EVERY,
        )
        self._document(FAQ)

    def _document(self, content, title="FAQ", tenant=None, base=None):
        document = KnowledgeDocument.objects.create(
            tenant=tenant or self.tenant, knowledge_base=base or self.base, title=title, content=content,
        )
        knowledge.rebuild(document)
        return document

    def _outbound(self):
        return list(Message.objects.filter(direction=Message.Direction.OUTBOUND).order_by("id"))


class AIReplyTests(EndToEndBase):
    """Test 1 -- a customer messages, the agent answers, the answer is sent."""

    def test_an_inbound_message_is_answered_on_the_same_number(self):

        with patch("apps.ai.services.complete", return_value=_reply()) as ask:
            _deliver(self.account, "what time do you open?", "wamid.in.open")

        ask.assert_called_once()
        outbound = self._outbound()
        self.assertEqual(len(outbound), 1)
        self.assertEqual(outbound[0].body, "We open at nine in the morning.")
        self.assertEqual(outbound[0].whatsapp_account_id, self.account.pk)
        self.assertEqual(outbound[0].kind, Message.Kind.SERVICE)

    def test_the_model_is_the_one_the_environment_names(self):

        with patch("apps.ai.services.complete", return_value=_reply()) as ask:
            _deliver(self.account, "what time do you open?", "wamid.in.model")

        self.assertEqual(ask.call_args.kwargs["model"], "gpt-4o-mini")
        self.assertEqual(ask.call_args.kwargs["provider"], AIAgentConfig.Provider.OPENAI)
        self.assertEqual(ask.call_args.kwargs["api_key"], "sk-deployment")

    def test_the_agents_own_reply_does_not_trigger_the_agent_again(self):
        # The loop this guards against: an outbound message that re-entered the
        # inbound path would answer itself for ever, on the customer's number.

        with patch("apps.ai.services.complete", return_value=_reply()) as ask:
            _deliver(self.account, "what time do you open?", "wamid.in.loop")

        self.assertEqual(ask.call_count, 1)
        self.assertEqual(len(self._outbound()), 1)

    def test_a_question_the_documents_do_not_cover_is_not_answered_by_the_model(self):
        # No call is made at all: there is nothing to answer from, so the only
        # thing a model could do is invent, and it would be billed for it.
        self.config.answer_from_knowledge_only = True
        self.config.fallback_action = AIAgentConfig.Fallback.SAY_UNKNOWN
        self.config.save()

        with patch("apps.ai.services.complete", return_value=_reply()) as ask:
            _deliver(self.account, "do you sell helicopter parts?", "wamid.in.helicopter")

        ask.assert_not_called()
        self.assertIn("don't have that information", self._outbound()[0].body)

    def test_a_provider_outage_hands_over_rather_than_going_silent(self):

        with patch("apps.ai.services.complete", side_effect=services.AIProviderError("502 from upstream")):
            _deliver(self.account, "what time do you open?", "wamid.in.outage")

        conversation = Conversation.objects.get(whatsapp_account=self.account)
        self.assertIsNotNone(conversation.ai_state.handed_off_at)
        self.assertIn("colleague", self._outbound()[0].body)


class DocumentKnowledgeTests(EndToEndBase):
    """Test 2 -- upload, extract, chunk, ask, retrieve, answer."""

    def test_an_uploaded_json_file_becomes_answerable_knowledge(self):
        from django.core.files.base import ContentFile

        from .processing import process_document

        payload = (
            '{"refund_policy": {"window_days": 30, "condition": "unused and in original packaging"},'
            ' "warranty": {"months": 12}}'
        )
        document = KnowledgeDocument.objects.create(
            tenant=self.tenant, knowledge_base=self.base, title="Policies",
            source_type=KnowledgeDocument.Source.FILE, original_filename="policies.json",
            file=ContentFile(payload.encode("utf-8"), name="policies.json"),
            status=KnowledgeDocument.Status.PENDING,
        )

        self.assertEqual(process_document(document.pk), KnowledgeDocument.Status.READY)

        document.refresh_from_db()
        self.assertGreater(document.chunk_count, 0)
        retrieval = knowledge.retrieve(self.tenant.pk, "what is the refund window?", knowledge_base_id=self.base.pk)
        self.assertTrue(retrieval)
        self.assertIn("30", " ".join(chunk.content for chunk in retrieval.chunks))

    def test_matching_chunks_are_sent_and_unrelated_ones_are_not(self):
        # The point of chunking: a chunk sharing no term with the question is
        # not sent at all. Chunks that *do* match are all sent, best first --
        # "days" is in the delivery paragraph too, so it earns its place;
        # opening hours share nothing with the question and do not.

        with patch("apps.ai.services.complete", return_value=_reply("Fourteen days.")) as ask:
            _deliver(self.account, "how many days for returns?", "wamid.in.returns")

        system = ask.call_args.kwargs["system"]
        references = system.split("Reference material:")[1]
        self.assertNotIn("nine in the morning", references)
        self.assertLess(references.index("fourteen days"), references.index("sixty rupees"))

    def test_an_answer_records_where_it_came_from(self):

        with patch("apps.ai.services.complete", return_value=_reply("Fourteen days.")):
            _deliver(self.account, "how many days for returns?", "wamid.in.sources")

        from .models import KnowledgeAnswer

        recorded = KnowledgeAnswer.objects.get(is_test=False)
        self.assertTrue(recorded.answered)
        self.assertEqual(recorded.sources[0]["title"], "FAQ")


class ConversationMemoryTests(EndToEndBase):
    """Test 3 -- a follow-up question that only makes sense in context."""

    def test_the_second_question_is_asked_with_the_first_still_in_view(self):

        with patch("apps.ai.services.complete", return_value=_reply("Within fourteen days.")) as ask:
            _deliver(self.account, "what is your returns policy?", "wamid.in.q1")
            _deliver(self.account, "and how many days do I have?", "wamid.in.q2")

        history = ask.call_args.kwargs["messages"]
        transcript = " ".join(turn["content"] for turn in history)
        self.assertIn("what is your returns policy?", transcript)
        self.assertIn("and how many days do I have?", transcript)
        # Our own reply is in there as the assistant's turn, which is what makes
        # "how many days" resolvable at all.
        self.assertIn("assistant", [turn["role"] for turn in history])

    def test_history_is_bounded_by_the_configured_window(self):
        self.config.history_messages = 4
        self.config.save()

        with patch("apps.ai.services.complete", return_value=_reply()) as ask:
            for index in range(6):
                _deliver(self.account, f"question number {index} about delivery", f"wamid.in.h{index}")

        self.assertLessEqual(len(ask.call_args.kwargs["messages"]), 4)

    def test_another_contacts_thread_is_never_mixed_in(self):

        with patch("apps.ai.services.complete", return_value=_reply()) as ask:
            _deliver(self.account, "my order is about a red shirt", "wamid.in.c1")
            message = inbox_services.record_inbound_message(
                _event(self.account, from_number="15559990000", body="what are your delivery charges?",
                       message_id="wamid.in.c2")
            )
            inbound_message_recorded.send(sender=Message, message=message, is_new_conversation=True)

        transcript = " ".join(turn["content"] for turn in ask.call_args.kwargs["messages"])
        self.assertNotIn("red shirt", transcript)


class MultipleAccountTests(EndToEndBase):
    """Test 4 -- two numbers, two conversations, two correctly routed replies."""

    def test_each_number_is_answered_on_itself(self):
        second = _account(self.tenant, self.owner, name="Support", phone="+15552220000")

        with patch("apps.ai.services.complete", return_value=_reply()):
            _deliver(self.account, "what time do you open?", "wamid.in.a")
            _deliver(second, "what time do you open?", "wamid.in.b")

        by_account = {message.whatsapp_account_id for message in self._outbound()}
        self.assertEqual(by_account, {self.account.pk, second.pk})
        self.assertEqual(len(self._outbound()), 2)

    def test_a_reply_never_leaves_on_a_different_number(self):
        second = _account(self.tenant, self.owner, name="Support", phone="+15552220000")

        with patch("apps.ai.services.complete", return_value=_reply()):
            _deliver(second, "what time do you open?", "wamid.in.only-b")

        outbound = self._outbound()
        self.assertEqual(len(outbound), 1)
        self.assertEqual(outbound[0].whatsapp_account_id, second.pk)
        # And its conversation is the one on that number, not a shared thread.
        self.assertEqual(outbound[0].conversation.whatsapp_account_id, second.pk)

    def test_a_disconnected_number_does_not_crash_the_reply_path(self):
        offline = _account(self.tenant, self.owner, name="Offline", phone="+15553330000")
        WhatsAppAccount.objects.filter(pk=offline.pk).update(status=WhatsAppAccount.Status.DISCONNECTED)

        with patch("apps.ai.services.complete", return_value=_reply()):
            _deliver(offline, "what time do you open?", "wamid.in.offline")

        # Nothing was sent, and nothing raised: the send helper refuses a
        # number that is not connected rather than the agent having to know.
        self.assertEqual(self._outbound(), [])


class NewDeviceTests(EndToEndBase):
    """
    Test 5 -- a number connected after everything was configured.

    This is the requirement that the AI and automation pick up a new device
    with no code change and no enrolment step. It holds because nothing keeps
    a list of AI-enabled accounts: the agent is configured per tenant, and the
    number is read from the conversation the message arrived on.
    """

    def test_a_number_connected_today_is_answered_today(self):
        brand_new = _account(self.tenant, self.owner, name="Just Connected", phone="+15554440000")

        with patch("apps.ai.services.complete", return_value=_reply()):
            _deliver(brand_new, "what time do you open?", "wamid.in.new")

        outbound = self._outbound()
        self.assertEqual(len(outbound), 1)
        self.assertEqual(outbound[0].whatsapp_account_id, brand_new.pk)

    def test_a_new_number_reads_the_same_knowledge_base(self):
        brand_new = _account(self.tenant, self.owner, name="Just Connected", phone="+15554440000")

        with patch("apps.ai.services.complete", return_value=_reply()) as ask:
            _deliver(brand_new, "how many days for returns?", "wamid.in.new-kb")

        self.assertIn("fourteen days", ask.call_args.kwargs["system"])

    def test_the_status_panel_counts_every_number_as_covered(self):
        _account(self.tenant, self.owner, name="Second", phone="+15555550000")
        _account(self.tenant, self.owner, name="Third", phone="+15556660000")

        whatsapp = status.whatsapp_status(self.tenant.pk)

        self.assertEqual(whatsapp["total"], 3)
        self.assertEqual(whatsapp["connected"], 3)
        self.assertEqual(whatsapp["covered_by_agent"], 3)


class TenantIsolationTests(EndToEndBase):
    """Test 6 -- one tenant's agent must never read another's documents."""

    def setUp(self):
        super().setUp()
        self.rival = Tenant.objects.create(name="Rival", slug=f"e2e-rival-{self._testMethodName}"[:50], kind=Tenant.Kind.CUSTOMER)
        self.rival_owner = get_user_model().objects.create_user(
            username=f"e2e-rival-{self._testMethodName}"[:140], password="x", tenant=self.rival, credits=500,
        )
        self.rival_base = KnowledgeBase.objects.create(tenant=self.rival, name="General", is_default=True)
        self._document(
            "Our confidential wholesale margin is forty two percent on every single order we ship.",
            title="Secret", tenant=self.rival, base=self.rival_base,
        )

    def test_a_question_cannot_reach_another_tenants_documents(self):
        retrieval = knowledge.retrieve(self.tenant.pk, "what is the wholesale margin?")

        self.assertNotIn("forty two percent", " ".join(chunk.content for chunk in retrieval.chunks))

    def test_the_rivals_text_never_reaches_the_prompt(self):
        self.config.answer_from_knowledge_only = False
        self.config.save()

        with patch("apps.ai.services.complete", return_value=_reply()) as ask:
            _deliver(self.account, "what is the wholesale margin?", "wamid.in.margin")

        self.assertNotIn("forty two percent", ask.call_args.kwargs["system"])

    def test_naming_another_tenants_knowledge_base_still_returns_nothing(self):
        # The base id is a plain integer, so passing somebody else's is the
        # obvious attack. The tenant filter is applied regardless of it.
        retrieval = knowledge.retrieve(
            self.tenant.pk, "what is the wholesale margin?", knowledge_base_id=self.rival_base.pk,
        )

        self.assertFalse(retrieval)

    def test_conversations_do_not_cross_tenants(self):
        rival_account = _account(self.rival, self.rival_owner, name="Rival Line", phone="+15557770000")

        with patch("apps.ai.services.complete", return_value=_reply()):
            _deliver(self.account, "what time do you open?", "wamid.in.mine")

        self.assertFalse(Conversation.objects.filter(tenant=self.rival).exists())
        self.assertFalse(Message.objects.filter(whatsapp_account=rival_account).exists())


class AutomationPriorityTests(EndToEndBase):
    """Test 7 -- the ladder, and the agent's place at the bottom of it."""

    def test_a_keyword_rule_answers_instead_of_the_agent(self):
        # A rule is something the tenant wrote down on purpose. The agent
        # guessing over the top of it would be slower and less correct, and
        # the contact would get two replies to one message.
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Pricing",
            trigger_type=AutomationRule.Trigger.KEYWORD, keyword="price",
            message_type=Message.MessageType.TEXT, message_body="Our price list is attached.",
        )

        with patch("apps.ai.services.complete", return_value=_reply()) as ask:
            _deliver(self.account, "what is the price?", "wamid.in.kw")

        ask.assert_not_called()
        outbound = self._outbound()
        self.assertEqual(len(outbound), 1)
        self.assertEqual(outbound[0].body, "Our price list is attached.")

    def test_the_agent_answers_what_no_rule_claimed(self):
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Pricing",
            trigger_type=AutomationRule.Trigger.KEYWORD, keyword="price",
            message_type=Message.MessageType.TEXT, message_body="Our price list is attached.",
        )

        with patch("apps.ai.services.complete", return_value=_reply()) as ask:
            _deliver(self.account, "what time do you open?", "wamid.in.nokw")

        ask.assert_called_once()
        self.assertEqual(self._outbound()[0].body, "We open at nine in the morning.")

    def test_a_rule_answers_on_the_number_it_belongs_to(self):
        # Rules are per number. A rule written for the support line must not
        # answer a message that arrived on the sales line -- and whatever does
        # answer must go back out on the line it came in on.
        second = _account(self.tenant, self.owner, name="Support", phone="+15552220000")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=second, name="Pricing",
            trigger_type=AutomationRule.Trigger.KEYWORD, keyword="price",
            message_type=Message.MessageType.TEXT, message_body="Support price list.",
        )

        _deliver(self.account, "what is the price?", "wamid.in.wrongline")

        outbound = self._outbound()
        self.assertNotIn("Support price list.", [message.body for message in outbound])
        for message in outbound:
            self.assertEqual(message.whatsapp_account_id, self.account.pk)

    def test_an_opt_out_is_answered_by_compliance_and_nothing_else(self):

        with patch("apps.ai.services.complete", return_value=_reply()) as ask:
            _deliver(self.account, "STOP", "wamid.in.stop")

        ask.assert_not_called()

    def test_a_human_owning_the_conversation_keeps_the_agent_out(self):
        self.config.reply_scope = AIAgentConfig.ReplyScope.UNASSIGNED
        self.config.save()
        agent_user = get_user_model().objects.create_user(
            username=f"e2e-agent-{self._testMethodName}"[:140], password="x", tenant=self.tenant,
        )
        contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.owner, name="Priya", phone="+15551234567",
        )
        Conversation.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=contact, assigned_agent=agent_user,
        )

        with patch("apps.ai.services.complete", return_value=_reply()) as ask:
            _deliver(self.account, "what time do you open?", "wamid.in.assigned")

        ask.assert_not_called()
        self.assertEqual(self._outbound(), [])


class StatusEndpointTests(EndToEndBase):
    """Test 16's screen: whether all of this is working, in one request."""

    def test_the_status_reports_the_resolved_model_not_the_blank_field(self):
        agent = status.agent_status(self.config)

        self.assertEqual(agent["model"], "gpt-4o-mini")
        self.assertEqual(agent["api_key_source"], "environment")
        self.assertEqual(agent["status"], "CONNECTED")

    def test_a_tenant_key_is_reported_as_such_and_never_echoed(self):
        self.config.set_api_key("sk-tenant-secret")
        self.config.save()

        agent = status.agent_status(self.config)

        self.assertEqual(agent["api_key_source"], "tenant")
        self.assertNotIn("sk-tenant-secret", str(agent))

    @override_settings(OPENAI_API_KEY="", AI_API_KEY="")
    def test_an_agent_with_no_key_reports_the_reason(self):
        agent = status.agent_status(AIAgentConfig.for_tenant(self.tenant.pk))

        self.assertEqual(agent["status"], "ERROR")
        self.assertIn("No API key", agent["problem"])

    def test_the_status_endpoint_is_reachable_and_scoped_to_the_caller(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.owner)

        response = client.get("/api/v1/ai/status/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["whatsapp"]["connected"], 1)
        self.assertEqual(response.data["knowledge"]["ready"], 1)
        self.assertEqual(response.data["knowledge"]["retrieval_mode"], "lexical")

    def test_the_last_call_and_its_cost_are_reported(self):

        with patch("apps.ai.services.complete", return_value=_reply()):
            _deliver(self.account, "what time do you open?", "wamid.in.usage")

        usage = status.usage_status(self.tenant.pk)

        self.assertEqual(usage["calls"], 1)
        self.assertEqual(usage["input_tokens"], 120)
        self.assertEqual(usage["last_call"]["model"], "gpt-4o-mini")
        self.assertEqual(usage["last_answer"]["question"], "what time do you open?")

    def test_a_failed_document_is_surfaced_with_its_reason(self):
        KnowledgeDocument.objects.create(
            tenant=self.tenant, knowledge_base=self.base, title="Scan",
            source_type=KnowledgeDocument.Source.FILE, original_filename="scan.pdf",
            status=KnowledgeDocument.Status.FAILED, error="No readable text was found in that file.",
        )

        knowledge_status = status.knowledge_status(self.tenant.pk, knowledge_base_id=self.base.pk)

        self.assertEqual(knowledge_status["failed"], 1)
        self.assertEqual(knowledge_status["failed_documents"][0]["error"], "No readable text was found in that file.")
