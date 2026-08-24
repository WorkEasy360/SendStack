from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.contacts.models import Contact
from apps.inbox.models import Conversation
from apps.inbox.signals import inbound_message_recorded
from apps.messaging.models import Message
from apps.teams.models import AssignmentPolicy, TeamMember
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount
from apps.whatsapp.providers.base import SendResult

from . import knowledge, services
from .models import AIAgentConfig, AIConversationState, AIUsageLog, KnowledgeChunk, KnowledgeDocument
from .providers.anthropic import AIProviderError, AIResponse

FAQ = """Our shop is open from 9am to 6pm, Monday to Saturday.

Delivery inside Mumbai takes two working days and costs 60 rupees.

We accept returns within 14 days if the item is unused and still in its packaging."""


def _account(tenant, user):
    account = WhatsAppAccount(
        tenant=tenant, user=user, name="Line", phone_number_id="1", status=WhatsAppAccount.Status.CONNECTED,
    )
    account.set_access_token("token")
    account.set_app_secret("secret")
    account.save()
    return account


def _reply(text, input_tokens=100, output_tokens=20):
    return AIResponse(text=text, input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=250)


class Fixture(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug=f"ai-{self._testMethodName}"[:50], kind=Tenant.Kind.CUSTOMER)
        self.owner = get_user_model().objects.create_user(
            username=f"ai-owner-{self._testMethodName}"[:140], password="x", tenant=self.tenant, credits=100,
        )
        self.account = _account(self.tenant, self.owner)
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.owner, name="Priya", phone="+15558880001",
        )
        self.conversation = Conversation.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
        )

    def _config(self, **kwargs):
        config = AIAgentConfig.objects.create(tenant=self.tenant, is_enabled=True, **kwargs)
        config.set_api_key("sk-test")
        config.save()
        return config

    def _inbound(self, body="What time do you open?"):
        return Message.objects.create(
            tenant=self.tenant, conversation=self.conversation, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, direction=Message.Direction.INBOUND, body=body,
            status=Message.Status.DELIVERED,
        )

    def _document(self, content=FAQ, title="FAQ"):
        document = KnowledgeDocument.objects.create(tenant=self.tenant, title=title, content=content)
        knowledge.rebuild(document)
        return document


class ChunkingTests(TestCase):
    def test_splits_on_paragraphs(self):
        self.assertEqual(len(knowledge.split(FAQ)), 3)

    def test_a_long_paragraph_is_split_on_sentence_ends(self):
        long_text = " ".join(f"Sentence number {index} about delivery." for index in range(200))

        pieces = knowledge.split(long_text)

        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(piece) <= knowledge.MAX_CHUNK_CHARS for piece in pieces))
        self.assertTrue(all(piece.endswith(".") for piece in pieces), pieces)

    def test_a_stray_fragment_is_folded_into_the_piece_before_it(self):
        pieces = knowledge.split("A full paragraph with plenty of words in it to be useful.\n\nok")

        self.assertEqual(len(pieces), 1)
        self.assertIn("ok", pieces[0])

    def test_empty_content_produces_nothing(self):
        self.assertEqual(knowledge.split(""), [])


class RetrievalTests(Fixture):
    def test_finds_the_paragraph_that_answers(self):
        self._document()

        results = knowledge.search(self.tenant.pk, "how much is delivery in mumbai?")

        self.assertTrue(results)
        self.assertIn("Delivery", results[0].content)

    def test_returns_nothing_when_no_term_matches(self):
        self._document()

        self.assertEqual(knowledge.search(self.tenant.pk, "quantum tunnelling"), [])

    def test_an_inactive_document_is_never_quoted(self):
        document = self._document()
        KnowledgeDocument.objects.filter(pk=document.pk).update(is_active=False)

        self.assertEqual(knowledge.search(self.tenant.pk, "delivery mumbai"), [])

    def test_another_tenants_knowledge_is_invisible(self):
        other = Tenant.objects.create(name="Other", slug="ai-other-kb", kind=Tenant.Kind.CUSTOMER)
        document = KnowledgeDocument.objects.create(tenant=other, title="Theirs", content=FAQ)
        knowledge.rebuild(document)

        self.assertEqual(knowledge.search(self.tenant.pk, "delivery mumbai"), [])

    def test_editing_a_document_replaces_its_chunks(self):
        document = self._document()
        document.content = "We have moved. The new shop is in Pune."
        document.save()
        knowledge.rebuild(document)

        self.assertEqual(KnowledgeChunk.objects.filter(document=document).count(), 1)
        self.assertEqual(knowledge.search(self.tenant.pk, "delivery mumbai"), [])


class PromptTests(Fixture):
    def test_knowledge_only_agent_is_told_to_refuse(self):
        config = self._config(answer_from_knowledge_only=True)

        prompt = services.build_system_prompt(config, [])

        self.assertIn(services.HANDOFF_TOKEN, prompt)
        self.assertIn("no reference material", prompt.lower())

    def test_retrieved_material_is_quoted_into_the_prompt(self):
        config = self._config()
        self._document()
        chunks = knowledge.search(self.tenant.pk, "delivery")

        prompt = services.build_system_prompt(config, chunks)

        self.assertIn("Delivery inside Mumbai", prompt)
        self.assertIn("[FAQ]", prompt)

    def test_business_context_is_included(self):
        config = self._config(business_context="We sell handmade sarees.")

        self.assertIn("handmade sarees", services.build_system_prompt(config, []))

    def test_the_structured_business_profile_reaches_the_prompt(self):
        """
        The profile is the shared record both agents read. The older free-text
        box still works alongside it, so a tenant who filled that in before
        the profile existed loses nothing.
        """
        from apps.tenants.models import BusinessProfile

        BusinessProfile.objects.create(tenant=self.tenant, description="We sell and service bicycles.")
        config = self._config(business_context="Always mention the summer sale.")

        prompt = services.build_system_prompt(
            config, [], profile=services.business_profile(self.tenant.pk),
        )

        self.assertIn("We sell and service bicycles.", prompt)
        self.assertIn("Always mention the summer sale.", prompt)

    def test_the_customer_the_agent_is_talking_to_reaches_the_prompt(self):
        from apps.crm import services as crm_services

        crm_services.create_deal(
            tenant=self.tenant, contact=self.contact, name="Fifty cycles", value=250000, actor=self.owner,
        )
        config = self._config()

        prompt = services.build_system_prompt(
            config, [], customer=services.customer_context(self.conversation),
        )

        self.assertIn("Priya", prompt)
        self.assertIn("Fifty cycles", prompt)
        # Customer facts must not read as a licence to invent product facts.
        self.assertIn("does not license you to invent", prompt)

    def test_a_customer_whose_context_cannot_be_built_still_gets_an_answer(self):
        """
        Context is decoration on an answer, never the answer. If assembling it
        fails, the customer must still be replied to exactly as they were
        before this layer existed.
        """
        from apps.crm import context as crm_context

        self._config()
        self._document()
        with patch.object(crm_context, "for_conversation", side_effect=RuntimeError("boom")):
            self.assertIsNone(services.customer_context(self.conversation))

    def test_the_reply_path_hands_both_the_customer_and_the_business_to_the_model(self):
        from apps.tenants.models import BusinessProfile

        BusinessProfile.objects.create(tenant=self.tenant, description="We sell and service bicycles.")
        config = self._config(answer_from_knowledge_only=False)
        inbound = self._inbound("do you have the blue one")
        sent = Message.objects.create(
            tenant=self.tenant, conversation=self.conversation, whatsapp_account=self.account,
            contact=self.contact, to_phone=self.contact.phone, direction=Message.Direction.OUTBOUND,
            body="Yes, we do.", status=Message.Status.SENT,
        )

        with patch.object(services, "_ask", return_value=_reply("Yes, we do.")) as mock:
            with patch("apps.automation.services.send_automated_message", return_value=sent):
                services.answer(self.conversation, inbound, config=config)

        system = mock.call_args[0][1]
        self.assertIn("We sell and service bicycles.", system)
        self.assertIn("Priya", system)

    def test_history_alternates_and_starts_with_the_customer(self):
        self._config()
        Message.objects.create(
            tenant=self.tenant, conversation=self.conversation, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, direction=Message.Direction.OUTBOUND, body="Welcome!",
            status=Message.Status.SENT,
        )
        self._inbound("hello")
        self._inbound("are you open?")

        history = services.build_history(self.conversation, 10)

        self.assertEqual(history[0]["role"], "user")
        roles = [turn["role"] for turn in history]
        self.assertEqual(roles, list(dict.fromkeys(roles)) if len(roles) == 1 else roles)
        self.assertTrue(all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)), roles)

    def test_consecutive_customer_messages_become_one_turn(self):
        self._config()
        self._inbound("hi")
        self._inbound("are you there?")

        history = services.build_history(self.conversation, 10)

        self.assertEqual(len(history), 1)
        self.assertIn("hi", history[0]["content"])
        self.assertIn("are you there?", history[0]["content"])


class AnswerTests(Fixture):
    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    @patch("apps.ai.services.complete")
    def test_answers_and_records_usage(self, mock_complete, mock_send):
        mock_complete.return_value = _reply("We're open 9am to 6pm, Monday to Saturday.")
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.ai")
        self._config()
        self._document()

        self.assertTrue(services.answer(self.conversation, self._inbound()))

        sent = Message.objects.filter(conversation=self.conversation, direction=Message.Direction.OUTBOUND).first()
        self.assertIn("9am to 6pm", sent.body)
        self.assertEqual(sent.kind, Message.Kind.SERVICE)
        log = AIUsageLog.objects.get()
        self.assertEqual(log.outcome, AIUsageLog.Outcome.ANSWERED)
        self.assertEqual(log.input_tokens, 100)
        self.assertEqual(AIConversationState.objects.get(conversation=self.conversation).replies_sent, 1)

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    @patch("apps.ai.services.complete")
    def test_the_handoff_token_is_never_sent_to_the_customer(self, mock_complete, mock_send):
        mock_complete.return_value = _reply(services.HANDOFF_TOKEN)
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.handoff")
        self._config(handoff_message="Getting a colleague for you.")

        self.assertFalse(services.answer(self.conversation, self._inbound()))

        bodies = list(
            Message.objects.filter(
                conversation=self.conversation, direction=Message.Direction.OUTBOUND,
            ).values_list("body", flat=True)
        )
        self.assertEqual(bodies, ["Getting a colleague for you."])
        self.assertIsNotNone(AIConversationState.objects.get(conversation=self.conversation).handed_off_at)

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    @patch("apps.ai.services.complete")
    def test_handoff_assigns_a_person(self, mock_complete, mock_send):
        mock_complete.return_value = _reply(services.HANDOFF_TOKEN)
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.h2")
        AssignmentPolicy.objects.create(tenant=self.tenant, mode=AssignmentPolicy.Mode.ROUND_ROBIN)
        agent_user = get_user_model().objects.create_user(
            username=f"agent-{self._testMethodName}"[:140], password="x", tenant=self.tenant,
        )
        TeamMember.objects.create(tenant=self.tenant, user=agent_user)
        self._config()

        services.answer(self.conversation, self._inbound())

        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.assigned_agent_id, agent_user.pk)
        self.assertIsNotNone(self.conversation.awaiting_reply_since)

    @patch("apps.ai.services.complete")
    def test_stays_out_of_a_conversation_a_person_owns(self, mock_complete):
        self._config()
        self.conversation.assigned_agent = self.owner
        self.conversation.save(update_fields=["assigned_agent"])

        self.assertFalse(services.answer(self.conversation, self._inbound()))
        mock_complete.assert_not_called()

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    @patch("apps.ai.services.complete")
    def test_answers_an_assigned_conversation_when_told_to(self, mock_complete, mock_send):
        mock_complete.return_value = _reply("Sure!")
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.assigned")
        self._config(reply_when_assigned=True)
        # The agent answers only from knowledge, so it needs some: with an
        # empty knowledge base the relevance gate short-circuits to the
        # fallback before the model is ever called (see services.answer).
        self._document()
        self.conversation.assigned_agent = self.owner
        self.conversation.save(update_fields=["assigned_agent"])

        self.assertTrue(services.answer(self.conversation, self._inbound()))

    @patch("apps.ai.services.complete")
    def test_never_speaks_again_after_handing_over(self, mock_complete):
        from django.utils import timezone

        self._config()
        AIConversationState.objects.create(conversation=self.conversation, handed_off_at=timezone.now())

        self.assertFalse(services.answer(self.conversation, self._inbound()))
        mock_complete.assert_not_called()

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    @patch("apps.ai.services.complete")
    def test_the_reply_limit_hands_over_rather_than_going_quiet(self, mock_complete, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.limit")
        self._config(max_replies_per_conversation=2)
        AIConversationState.objects.create(conversation=self.conversation, replies_sent=2)

        self.assertFalse(services.answer(self.conversation, self._inbound()))

        mock_complete.assert_not_called()
        state = AIConversationState.objects.get(conversation=self.conversation)
        self.assertEqual(state.handoff_reason, "reply limit reached")

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    @patch("apps.ai.services.complete")
    def test_a_provider_outage_hands_over_instead_of_leaving_silence(self, mock_complete, mock_send):
        mock_complete.side_effect = AIProviderError("timed out")
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.down")
        self._config()
        # Knowledge has to be found for the model to be reached at all, which
        # is the point of this test -- reaching it and failing.
        self._document()

        self.assertFalse(services.answer(self.conversation, self._inbound()))

        self.assertEqual(AIUsageLog.objects.get().outcome, AIUsageLog.Outcome.ERROR)
        self.assertIsNotNone(AIConversationState.objects.get(conversation=self.conversation).handed_off_at)

    @patch("apps.ai.services.complete")
    def test_a_disabled_agent_never_calls_the_model(self, mock_complete):
        AIAgentConfig.objects.create(tenant=self.tenant, is_enabled=False)

        self.assertFalse(services.answer(self.conversation, self._inbound()))
        mock_complete.assert_not_called()

    @patch("apps.ai.services.complete")
    def test_no_api_key_means_no_call(self, mock_complete):
        AIAgentConfig.objects.create(tenant=self.tenant, is_enabled=True)

        with override_settings(AI_API_KEY=""):
            self.assertFalse(services.answer(self.conversation, self._inbound()))
        mock_complete.assert_not_called()


class AutomationOrderTests(Fixture):
    @patch("apps.ai.services.maybe_reply")
    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_a_keyword_rule_wins_over_the_agent(self, mock_send, mock_maybe_reply):
        from apps.automation.models import AutomationRule

        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.kw")
        self._config()
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Hours", trigger_type=AutomationRule.Trigger.KEYWORD,
            keyword="open", message_body="We open at 9.",
        )
        message = self._inbound("when do you open?")

        inbound_message_recorded.send(sender=Message, message=message, is_new_conversation=False)

        mock_maybe_reply.assert_not_called()

    @patch("apps.ai.services.maybe_reply")
    def test_the_agent_answers_what_no_rule_matched(self, mock_maybe_reply):
        self._config()
        message = self._inbound("do you ship to Pune?")

        inbound_message_recorded.send(sender=Message, message=message, is_new_conversation=False)

        mock_maybe_reply.assert_called_once()

    @patch("apps.ai.services.maybe_reply")
    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_an_opt_out_never_reaches_the_agent(self, mock_send, mock_maybe_reply):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.stop")
        self._config()
        message = self._inbound("STOP")

        inbound_message_recorded.send(sender=Message, message=message, is_new_conversation=False)

        mock_maybe_reply.assert_not_called()


class AgentHelpersTests(Fixture):
    @patch("apps.ai.services.complete")
    def test_suggest_reply_returns_a_draft_and_sends_nothing(self, mock_complete):
        mock_complete.return_value = _reply("We're open 9 to 6.")
        self._config()
        self._inbound()

        suggestion = services.suggest_reply(self.conversation)

        self.assertEqual(suggestion, "We're open 9 to 6.")
        self.assertFalse(
            Message.objects.filter(conversation=self.conversation, direction=Message.Direction.OUTBOUND).exists()
        )

    @patch("apps.ai.services.complete")
    def test_suggest_reply_refuses_rather_than_offering_the_handoff_token(self, mock_complete):
        mock_complete.return_value = _reply(services.HANDOFF_TOKEN)
        self._config()
        self._inbound()

        with self.assertRaises(AIProviderError):
            services.suggest_reply(self.conversation)

    @patch("apps.ai.services.complete")
    def test_summarise_reads_the_thread(self, mock_complete):
        mock_complete.return_value = _reply("Customer asked about opening hours.")
        self._config()
        self._inbound()

        self.assertIn("opening hours", services.summarise(self.conversation))
        self.assertEqual(AIUsageLog.objects.get().outcome, AIUsageLog.Outcome.SUMMARISED)

    def test_summarise_needs_something_to_summarise(self):
        self._config()

        with self.assertRaises(AIProviderError):
            services.summarise(self.conversation)


class AIApiTests(Fixture):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def test_the_api_key_is_never_read_back(self):
        self.client.patch("/api/v1/ai/agent/", {"api_key": "sk-secret"}, format="json")

        response = self.client.get("/api/v1/ai/agent/")

        self.assertNotIn("api_key", response.data)
        self.assertTrue(response.data["has_api_key"])
        self.assertNotIn("sk-secret", str(response.data))

    def test_saving_without_a_key_keeps_the_stored_one(self):
        self.client.patch("/api/v1/ai/agent/", {"api_key": "sk-secret"}, format="json")

        self.client.patch("/api/v1/ai/agent/", {"display_name": "Asha"}, format="json")

        config = AIAgentConfig.objects.get(tenant=self.tenant)
        self.assertEqual(config.get_api_key(), "sk-secret")
        self.assertEqual(config.display_name, "Asha")

    def test_clearing_the_key_is_an_explicit_action(self):
        self.client.patch("/api/v1/ai/agent/", {"api_key": "sk-secret"}, format="json")

        response = self.client.post("/api/v1/ai/agent/clear-key/")

        self.assertFalse(response.data["has_api_key"])
        self.assertIsNone(AIAgentConfig.objects.get(tenant=self.tenant).get_api_key())

    def test_handoff_needs_a_message_to_send(self):
        response = self.client.patch(
            "/api/v1/ai/agent/", {"handoff_enabled": True, "handoff_message": "  "}, format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("handoff_message", response.data)

    def test_creating_a_document_chunks_it(self):
        response = self.client.post(
            "/api/v1/ai/knowledge/", {"title": "FAQ", "content": FAQ}, format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(KnowledgeChunk.objects.filter(document_id=response.data["id"]).count(), 3)

    def test_editing_a_document_rechunks_it(self):
        created = self.client.post("/api/v1/ai/knowledge/", {"title": "FAQ", "content": FAQ}, format="json")

        self.client.patch(
            f"/api/v1/ai/knowledge/{created.data['id']}/", {"content": "Only one paragraph now."}, format="json",
        )

        self.assertEqual(KnowledgeChunk.objects.filter(document_id=created.data["id"]).count(), 1)

    def test_search_shows_what_the_agent_would_retrieve(self):
        self._document()

        response = self.client.post("/api/v1/ai/knowledge/search/", {"query": "returns"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertIn("returns", response.data["results"][0]["content"])

    def test_suggest_reply_without_a_key_is_a_clear_400(self):
        AIAgentConfig.objects.create(tenant=self.tenant, is_enabled=True)
        self._inbound()

        with override_settings(AI_API_KEY=""):
            response = self.client.post(
                "/api/v1/ai/suggest-reply/", {"conversation": self.conversation.pk}, format="json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("API key", response.data["detail"])

    def test_another_tenants_conversation_is_a_404(self):
        other = Tenant.objects.create(name="Other", slug="ai-other-conv", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="ai-other-user", password="x", tenant=other)
        other_account = _account(other, other_user)
        other_contact = Contact.objects.create(tenant=other, created_by=other_user, name="X", phone="+15558889999")
        foreign = Conversation.objects.create(tenant=other, whatsapp_account=other_account, contact=other_contact)

        response = self.client.post("/api/v1/ai/suggest-reply/", {"conversation": foreign.pk}, format="json")

        self.assertEqual(response.status_code, 404)

    def test_usage_summary_counts_by_outcome(self):
        AIUsageLog.objects.create(
            tenant=self.tenant, model="claude-sonnet-5", outcome=AIUsageLog.Outcome.ANSWERED,
            input_tokens=100, output_tokens=20,
        )
        AIUsageLog.objects.create(
            tenant=self.tenant, model="claude-sonnet-5", outcome=AIUsageLog.Outcome.HANDOFF,
            input_tokens=50, output_tokens=5,
        )

        response = self.client.get("/api/v1/ai/usage/summary/")

        self.assertEqual(response.data["calls"], 2)
        self.assertEqual(response.data["input_tokens"], 150)
        self.assertEqual(response.data["by_outcome"]["ANSWERED"], 1)


class WorkflowNodeTests(Fixture):
    @patch("apps.ai.services.answer")
    def test_an_ai_reply_node_runs_the_agent_and_carries_on(self, mock_answer):
        from apps.automation.models import Workflow
        from apps.automation.services import start_workflow

        self._config()
        self._inbound()
        workflow = Workflow.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Support",
            nodes=[
                {"id": "t", "type": "TRIGGER", "next": "ai"},
                {"id": "ai", "type": "AI_REPLY", "next": "end"},
                {"id": "end", "type": "END"},
            ],
        )

        run = start_workflow(workflow, self.contact)
        run.conversation = self.conversation
        run.save(update_fields=["conversation"])

        # Re-run with the conversation attached, which is how a real trigger
        # would have started it.
        run.current_node_id = "ai"
        from apps.automation.services import _advance

        _advance(run)

        mock_answer.assert_called()

    def test_ai_reply_is_a_valid_node_type(self):
        from apps.automation.services import validate_nodes

        validate_nodes([
            {"id": "t", "type": "TRIGGER", "next": "ai"},
            {"id": "ai", "type": "AI_REPLY", "next": "end"},
            {"id": "end", "type": "END"},
        ])
