"""
Native tool calling in the WhatsApp agent.

Every test here starts where a real message starts -- a stored webhook event
-- and goes through the real receiver, the real trigger ladder, the real
retrieval and the real send helper. Only two things are stubbed: the model
provider, because the thing under test is what we ask it and what we do with
its answer rather than its weights, and the WhatsApp transport, because it
would message a real person.

The stub is a scripted model. It is given a list of turns and returns them in
order, recording what it was sent each time. That is deliberately more work
than returning a fixed reply: most of what can break in tool calling is in
the *second* request -- whether the assistant turn was echoed back with its
tool_use blocks, whether every one of them was paired with a tool_result --
and a stub that only ever answers once cannot see any of it.

The twelve scenarios named in the brief each have a test below, and several
have more than one, because "tenant isolation" and "unauthorized tool" are
each two different failures wearing one name.
"""

import itertools
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.contacts.models import Contact, ContactTag
from apps.crm.models import Activity
from apps.inbox import services as inbox_services
from apps.inbox.models import Conversation
from apps.inbox.signals import inbound_message_recorded
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount, WhatsAppWebhookEvent
from apps.whatsapp.providers.base import SendResult

from . import agent, knowledge, services, status, tools
from .models import AIAgentConfig, AIToolCall, KnowledgeBase, KnowledgeDocument
from .providers.base import AIResponse, ToolCall

FAQ = """Our shop is open from nine in the morning until six in the evening, Monday to Saturday.

Delivery anywhere inside Mumbai takes two working days and costs sixty rupees per parcel.

We accept returns within fourteen days of purchase if the item is unused and still in its packaging."""


# ---------------------------------------------------------------------------
# A scripted model
# ---------------------------------------------------------------------------

def text_turn(text):
    """A plain answer with no tools."""
    return AIResponse(text=text, input_tokens=100, output_tokens=20, latency_ms=150,
                      content=[{"type": "text", "text": text}])


def tool_turn(*calls, text=""):
    """
    An assistant turn that asks for tools.

    `content` carries the tool_use blocks as well as any text, because that is
    what has to be echoed back on the next request for the tool_results to
    pair with anything.
    """
    tool_calls = [ToolCall(id=f"call_{index}", name=name, input=payload)
                  for index, (name, payload) in enumerate(calls, start=1)]
    content = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input} for call in tool_calls
    ]
    return AIResponse(text=text, input_tokens=100, output_tokens=20, latency_ms=150,
                      stop_reason="tool_use", tool_calls=tool_calls, content=content)


class ScriptedModel:
    """
    Returns prepared turns in order and remembers every request.

    Running off the end is an explicit failure rather than a repeat of the
    last turn: a loop that asks one more time than expected is exactly the bug
    these tests exist to catch, and a stub that quietly answers again would
    hide it.
    """

    def __init__(self, *turns):
        self.turns = list(turns)
        self.requests = []

    def __call__(self, **kwargs):
        self.requests.append(kwargs)
        if not self.turns:
            raise AssertionError(f"the model was called {len(self.requests)} times; the script had fewer turns")
        return self.turns.pop(0)

    @property
    def calls(self):
        return len(self.requests)

    def tools_offered(self, index=0):
        return [tool["name"] for tool in (self.requests[index].get("tools") or [])]

    def messages(self, index):
        return self.requests[index]["messages"]


def _unique_sends(prefix="wamid.out"):
    counter = itertools.count(1)
    return lambda *args, **kwargs: SendResult(success=True, provider_message_id=f"{prefix}.{next(counter)}")


def _account(tenant, user, name="Line", phone="+15551110000", **extra):
    account = WhatsAppAccount(
        tenant=tenant, user=user, name=name, phone_number=phone, phone_number_id=f"pnid-{name}",
        status=WhatsAppAccount.Status.CONNECTED, **extra,
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


def _deliver(account, body, message_id, from_number="15551234567"):
    """One inbound message, taken all the way through the real receiver."""
    message = inbox_services.record_inbound_message(
        _event(account, body=body, message_id=message_id, from_number=from_number)
    )
    inbound_message_recorded.send(
        sender=Message, message=message, is_new_conversation=getattr(message, "is_new_conversation", False),
    )
    return message


@override_settings(OPENAI_API_KEY="sk-deployment", OPENAI_MODEL="gpt-4o-mini", AI_EMBEDDINGS_ENABLED=False)
class ToolBase(TestCase):
    """A tenant with one number, one knowledge base and a tool-enabled agent."""

    #: Overridden per class to grant more than read.
    write_tools = False
    send_tools = False
    tools_on = True

    def setUp(self):
        transport = patch("apps.messaging.services.ConnectionManager.send_text_message")
        self.mock_send = transport.start()
        self.addCleanup(transport.stop)
        self.mock_send.side_effect = _unique_sends()

        self.tenant = Tenant.objects.create(
            name="Acme", slug=f"tools-{self._testMethodName}"[:50], kind=Tenant.Kind.CUSTOMER,
        )
        self.owner = get_user_model().objects.create_user(
            username=f"tools-owner-{self._testMethodName}"[:140], password="x", tenant=self.tenant, credits=500,
        )
        self.account = _account(self.tenant, self.owner, provider_session_id=f"dev-{self._testMethodName}"[:60])
        self.base = KnowledgeBase.objects.create(tenant=self.tenant, name="General", is_default=True)
        self.config = AIAgentConfig.objects.create(
            tenant=self.tenant, is_enabled=True, provider=AIAgentConfig.Provider.OPENAI, model="",
            knowledge_base=self.base, reply_scope=AIAgentConfig.ReplyScope.EVERY,
            tools_enabled=self.tools_on, allow_write_tools=self.write_tools, allow_send_tools=self.send_tools,
        )
        self._document(FAQ)

    def _document(self, content, title="FAQ", tenant=None, base=None):
        document = KnowledgeDocument.objects.create(
            tenant=tenant or self.tenant, knowledge_base=base or self.base, title=title, content=content,
        )
        knowledge.rebuild(document)
        return document

    def _outbound(self, tenant=None):
        rows = Message.objects.filter(direction=Message.Direction.OUTBOUND)
        if tenant is not None:
            rows = rows.filter(tenant=tenant)
        return list(rows.order_by("id"))

    def _run(self, model, body="what time do you open?", message_id="wamid.in.1"):
        with patch("apps.ai.agent.converse", side_effect=model), \
             patch("apps.ai.services.complete", side_effect=AssertionError("the no-tool path should not run")):
            _deliver(self.account, body, message_id)
        return model


# ---------------------------------------------------------------------------
# 1-2. Normal question, RAG question
# ---------------------------------------------------------------------------

class PlainAnswerTests(ToolBase):
    """A question answered without reaching for anything."""

    def test_a_normal_question_is_answered_in_one_round(self):
        model = self._run(ScriptedModel(text_turn("We open at nine in the morning.")))

        self.assertEqual(model.calls, 1)
        self.assertEqual(self._outbound()[-1].body, "We open at nine in the morning.")
        self.assertFalse(AIToolCall.objects.exists())

    def test_a_rag_question_still_gets_its_retrieved_material(self):
        """Tools do not replace the retrieval that already happens per message."""
        model = self._run(
            ScriptedModel(text_turn("Delivery in Mumbai takes two working days.")),
            body="how long is delivery?",
        )

        system = model.requests[0]["system"]
        self.assertIn("Reference material", system)
        self.assertIn("two working days", system)
        self.assertEqual(self._outbound()[-1].body, "Delivery in Mumbai takes two working days.")

    def test_the_tools_are_offered_to_the_model(self):
        model = self._run(ScriptedModel(text_turn("We open at nine.")))

        offered = model.tools_offered()
        self.assertIn("search_knowledge", offered)
        self.assertIn("lookup_customer", offered)
        self.assertIn("hand_to_human", offered)
        # Read-only tenant: the write and send tools are not even described.
        self.assertNotIn("save_customer_details", offered)
        self.assertNotIn("send_whatsapp_message", offered)

    def test_no_tool_response_is_sent_verbatim(self):
        """The 'no-tool response' case: the model answers, nothing else happens."""
        model = self._run(ScriptedModel(text_turn("Yes, we are open on Saturdays.")))

        self.assertEqual(model.calls, 1)
        self.assertEqual(AIToolCall.objects.count(), 0)
        self.assertEqual(len(self._outbound()), 1)


# ---------------------------------------------------------------------------
# 3-4. Single and multiple tool calls
# ---------------------------------------------------------------------------

class SingleToolTests(ToolBase):
    def test_one_tool_call_then_an_answer(self):
        model = self._run(ScriptedModel(
            tool_turn(("search_knowledge", {"query": "returns policy"})),
            text_turn("You can return anything within fourteen days."),
        ))

        self.assertEqual(model.calls, 2)
        self.assertEqual(self._outbound()[-1].body, "You can return anything within fourteen days.")

        call = AIToolCall.objects.get()
        self.assertEqual(call.name, "search_knowledge")
        self.assertFalse(call.is_error)
        self.assertIn("fourteen days", call.result)

    def test_the_tool_result_is_returned_to_the_model(self):
        """Requirement 6: the model must see what the tool found."""
        model = self._run(ScriptedModel(
            tool_turn(("search_knowledge", {"query": "returns"})),
            text_turn("Fourteen days."),
        ))

        second = model.messages(1)
        # The assistant turn goes back with its tool_use block intact...
        assistant = [turn for turn in second if turn["role"] == "assistant"][-1]
        self.assertTrue(any(block.get("type") == "tool_use" for block in assistant["content"]))
        # ...and is answered by a tool_result carrying the retrieved text.
        results = [
            block for turn in second if isinstance(turn.get("content"), list)
            for block in turn["content"] if block.get("type") == "tool_result"
        ]
        self.assertEqual(len(results), 1)
        self.assertIn("fourteen days", results[0]["content"])

    def test_every_tool_use_is_paired_with_a_tool_result(self):
        """An unpaired tool_use is a 400 on the next request, so this is load-bearing."""
        model = self._run(ScriptedModel(
            tool_turn(
                ("search_knowledge", {"query": "delivery"}),
                ("lookup_customer", {}),
            ),
            text_turn("Two working days, and I can see your last order."),
        ))

        second = model.messages(1)
        uses = [
            block for turn in second if isinstance(turn.get("content"), list)
            for block in turn["content"] if block.get("type") == "tool_use"
        ]
        results = [
            block for turn in second if isinstance(turn.get("content"), list)
            for block in turn["content"] if block.get("type") == "tool_result"
        ]
        self.assertEqual(len(uses), 2)
        self.assertEqual(len(results), 2)
        self.assertEqual({use["id"] for use in uses}, {result["tool_use_id"] for result in results})


class MultipleToolTests(ToolBase):
    write_tools = True

    def test_several_sequential_rounds_each_run_their_tool(self):
        model = self._run(ScriptedModel(
            tool_turn(("search_knowledge", {"query": "delivery"})),
            tool_turn(("lookup_customer", {})),
            tool_turn(("save_customer_details", {"name": "Priya"})),
            text_turn("Two working days, Priya."),
        ))

        self.assertEqual(model.calls, 4)
        self.assertEqual(
            list(AIToolCall.objects.order_by("id").values_list("name", flat=True)),
            ["search_knowledge", "lookup_customer", "save_customer_details"],
        )
        self.assertEqual(self._outbound()[-1].body, "Two working days, Priya.")

    def test_two_tools_in_one_round_both_run(self):
        model = self._run(ScriptedModel(
            tool_turn(
                ("search_knowledge", {"query": "returns"}),
                ("tag_contact", {"tag": "Returns enquiry"}),
            ),
            text_turn("Fourteen days."),
        ))

        self.assertEqual(model.calls, 2)
        self.assertEqual(AIToolCall.objects.count(), 2)
        self.assertTrue(ContactTag.objects.filter(tenant=self.tenant, name="Returns enquiry").exists())

    def test_a_repeated_tool_call_is_not_run_twice(self):
        """
        The model asking again for something it already got is a known failure
        mode. Running it twice would mean two CRM writes for one message.
        """
        model = self._run(ScriptedModel(
            tool_turn(("tag_contact", {"tag": "Pricing"})),
            tool_turn(("tag_contact", {"tag": "Pricing"})),
        ))

        # Stopped after the repeat rather than burning the rest of the cap.
        self.assertEqual(model.calls, 2)
        self.assertEqual(AIToolCall.objects.filter(name="tag_contact").count(), 1)


# ---------------------------------------------------------------------------
# 5. Tool failure
# ---------------------------------------------------------------------------

class ToolFailureTests(ToolBase):
    write_tools = True

    def test_a_tool_that_raises_still_produces_a_tool_result(self):
        # Patched at the tool's real dependency rather than at the handler, so
        # this exercises the genuine "the database refused" path.
        with patch("apps.contacts.models.ContactTag.objects.get_or_create",
                   side_effect=RuntimeError("db exploded")):
            model = self._run(ScriptedModel(
                tool_turn(("tag_contact", {"tag": "VIP"})),
                text_turn("Noted, thank you."),
            ))

        self.assertEqual(model.calls, 2)
        # The customer still gets an answer.
        self.assertEqual(self._outbound()[-1].body, "Noted, thank you.")

        call = AIToolCall.objects.get(name="tag_contact")
        self.assertTrue(call.is_error)
        self.assertIn("RuntimeError", call.detail)

    def test_the_internal_error_never_reaches_the_model_or_the_customer(self):
        """Requirement 16, checked at both boundaries."""
        with patch("apps.contacts.models.ContactTag.objects.get_or_create",
                   side_effect=RuntimeError("password=hunter2 at row 44")):
            model = self._run(ScriptedModel(
                tool_turn(("tag_contact", {"tag": "VIP"})),
                text_turn("Noted."),
            ))

        results = [
            block for turn in model.messages(1) if isinstance(turn.get("content"), list)
            for block in turn["content"] if block.get("type") == "tool_result"
        ]
        self.assertEqual(len(results), 1)
        self.assertNotIn("hunter2", results[0]["content"])
        self.assertNotIn("RuntimeError", results[0]["content"])
        self.assertNotIn("hunter2", self._outbound()[-1].body)

        # It is kept where somebody debugging can find it.
        self.assertIn("hunter2", AIToolCall.objects.get(name="tag_contact").detail)

    def test_an_unknown_tool_is_refused_without_breaking_the_turn(self):
        model = self._run(ScriptedModel(
            tool_turn(("delete_everything", {})),
            text_turn("I cannot do that, but I can help another way."),
        ))

        self.assertEqual(model.calls, 2)
        self.assertEqual(self._outbound()[-1].body, "I cannot do that, but I can help another way.")


# ---------------------------------------------------------------------------
# 6. Unauthorized tool
# ---------------------------------------------------------------------------

class PermissionTests(ToolBase):
    """Read-only tenant: write and send are neither offered nor runnable."""

    def test_an_ungranted_tool_is_not_offered(self):
        self.assertEqual(
            tools.available(self.config), ["search_knowledge", "lookup_customer", "hand_to_human"],
        )

    def test_an_ungranted_tool_is_refused_even_if_asked_for(self):
        """
        Offering is a hint; the check is the boundary. A model working from a
        cached prefix, or inventing a plausible name, gets here and not there.
        """
        model = self._run(ScriptedModel(
            tool_turn(("save_customer_details", {"name": "Priya", "email": "p@example.com"})),
            text_turn("Thanks."),
        ))

        results = [
            block for turn in model.messages(1) if isinstance(turn.get("content"), list)
            for block in turn["content"] if block.get("type") == "tool_result"
        ]
        self.assertIn("not able to do that", results[0]["content"])

        self.contact = Contact.objects.get(tenant=self.tenant)
        self.assertEqual(self.contact.email, "")

        call = AIToolCall.objects.get(name="save_customer_details")
        self.assertTrue(call.is_error)
        self.assertIn("WRITE not granted", call.detail)

    def test_the_send_tool_is_refused_without_the_send_capability(self):
        before = len(self._outbound())
        self._run(ScriptedModel(
            tool_turn(("send_whatsapp_message", {"message": "here is our address"})),
            text_turn("Sure."),
        ))

        # The reply itself went out; the tool's extra message did not.
        self.assertEqual(len(self._outbound()), before + 1)
        self.assertTrue(AIToolCall.objects.get(name="send_whatsapp_message").is_error)

    def test_tools_off_entirely_makes_the_old_path_run(self):
        self.config.tools_enabled = False
        self.config.save(update_fields=["tools_enabled"])

        self.assertEqual(tools.available(self.config), [])
        self.assertEqual(tools.granted(self.config), set())

        with patch("apps.ai.services.complete", return_value=text_turn("We open at nine.")) as plain, \
             patch("apps.ai.agent.converse", side_effect=AssertionError("the tool loop must not run")):
            _deliver(self.account, "what time do you open?", "wamid.in.plain")

        plain.assert_called_once()
        self.assertEqual(self._outbound()[-1].body, "We open at nine.")

    def test_granting_write_widens_the_set_but_not_to_send(self):
        self.config.allow_write_tools = True
        self.config.save(update_fields=["allow_write_tools"])

        available = tools.available(self.config)
        self.assertIn("save_customer_details", available)
        self.assertIn("tag_contact", available)
        self.assertNotIn("send_whatsapp_message", available)
        self.assertEqual(tools.granted(self.config), {tools.READ, tools.WRITE})


# ---------------------------------------------------------------------------
# 7. Tenant isolation
# ---------------------------------------------------------------------------

class TenantIsolationTests(ToolBase):
    write_tools = True

    def setUp(self):
        super().setUp()
        # A second tenant, with its own everything and a distinctive document.
        self.other_tenant = Tenant.objects.create(
            name="Other", slug=f"other-{self._testMethodName}"[:50], kind=Tenant.Kind.CUSTOMER,
        )
        self.other_owner = get_user_model().objects.create_user(
            username=f"other-owner-{self._testMethodName}"[:140], password="x",
            tenant=self.other_tenant, credits=500,
        )
        self.other_account = _account(self.other_tenant, self.other_owner, name="Other", phone="+15559990000")
        self.other_base = KnowledgeBase.objects.create(tenant=self.other_tenant, name="General", is_default=True)
        self._document(
            "Our secret warehouse code is ZEBRA-9 and only staff may know it.",
            title="Internal", tenant=self.other_tenant, base=self.other_base,
        )

    def test_a_tool_never_reads_another_tenants_documents(self):
        self._run(ScriptedModel(
            tool_turn(("search_knowledge", {"query": "secret warehouse code ZEBRA"})),
            text_turn("I do not have that."),
        ))

        call = AIToolCall.objects.get(name="search_knowledge")
        self.assertNotIn("ZEBRA", call.result)
        self.assertEqual(call.tenant_id, self.tenant.pk)

    def test_a_tag_created_by_one_tenant_is_not_visible_to_another(self):
        self._run(ScriptedModel(
            tool_turn(("tag_contact", {"tag": "VIP"})),
            text_turn("Noted."),
        ))

        self.assertTrue(ContactTag.objects.filter(tenant=self.tenant, name="VIP").exists())
        self.assertFalse(ContactTag.objects.filter(tenant=self.other_tenant, name="VIP").exists())

    def test_the_context_is_built_from_the_conversation_not_the_model(self):
        """
        The isolation guarantee in one assertion: no tool schema accepts an
        owning identifier, so there is no argument the model can emit that
        points at another tenant.
        """
        for definition in tools.definitions(self.config):
            properties = set((definition["input_schema"].get("properties") or {}).keys())
            for forbidden in ("tenant_id", "conversation_id", "contact_id", "whatsapp_account_id", "account_id"):
                self.assertNotIn(forbidden, properties, f"{definition['name']} exposes {forbidden}")

    def test_the_audit_row_records_the_owning_tenant(self):
        self._run(ScriptedModel(
            tool_turn(("lookup_customer", {})),
            text_turn("Here is what I have."),
        ))

        call = AIToolCall.objects.get(name="lookup_customer")
        self.assertEqual(call.tenant_id, self.tenant.pk)
        self.assertNotEqual(call.tenant_id, self.other_tenant.pk)


# ---------------------------------------------------------------------------
# 8-9. Multiple accounts, a new device
# ---------------------------------------------------------------------------

class MultipleAccountTests(ToolBase):
    send_tools = True

    def setUp(self):
        super().setUp()
        # A second number on the same tenant. The customer is talking to the
        # first one; nothing may go out on this.
        self.support = _account(
            self.tenant, self.owner, name="Support", phone="+15552220000",
            provider_session_id="dev-support",
        )

    def test_a_tool_send_uses_the_conversations_own_account(self):
        self._run(ScriptedModel(
            tool_turn(("send_whatsapp_message", {"message": "Our address is 12 Main Street."})),
            text_turn("I have sent you the address."),
        ))

        sent = self._outbound()
        self.assertEqual(len(sent), 2)
        # Both the tool's message and the reply are on the number the customer
        # actually messaged, not the tenant's other line.
        for message in sent:
            self.assertEqual(message.whatsapp_account_id, self.account.pk)
            self.assertNotEqual(message.whatsapp_account_id, self.support.pk)

    def test_the_context_carries_the_right_account_and_device(self):
        conversation = None
        captured = {}

        original = tools.execute

        def _capture(name, payload, config, context):
            captured["context"] = context
            return original(name, payload, config, context)

        with patch("apps.ai.agent.tools.execute", side_effect=_capture):
            self._run(ScriptedModel(
                tool_turn(("lookup_customer", {})),
                text_turn("Right."),
            ))

        conversation = Conversation.objects.get(whatsapp_account=self.account)
        context = captured["context"]
        self.assertEqual(context.tenant_id, self.tenant.pk)
        self.assertEqual(context.user_id, self.owner.pk)
        self.assertEqual(context.whatsapp_account_id, self.account.pk)
        self.assertEqual(context.conversation_id, conversation.pk)
        self.assertEqual(context.contact_id, conversation.contact_id)
        self.assertEqual(context.device_id, self.account.provider_session_id)
        self.assertTrue(context.is_complete)

    def test_a_conversation_on_the_second_number_answers_on_that_number(self):
        self._run(ScriptedModel(
            tool_turn(("send_whatsapp_message", {"message": "Support details."})),
            text_turn("Sent."),
        ), body="hello", message_id="wamid.in.first")

        # Now the same customer messages the support line instead.
        with patch("apps.ai.agent.converse", side_effect=ScriptedModel(
            tool_turn(("send_whatsapp_message", {"message": "Support address."})),
            text_turn("Sent on support."),
        )):
            _deliver(self.support, "hello support", "wamid.in.support")

        support_messages = [m for m in self._outbound() if m.whatsapp_account_id == self.support.pk]
        self.assertEqual(len(support_messages), 2)
        self.assertEqual(support_messages[-1].body, "Sent on support.")

    def test_a_newly_connected_device_is_covered_with_no_enrolment(self):
        """
        Requirement 'new WhatsApp device': a number connected after the agent
        was configured is answered by it, because the account is read off the
        conversation rather than from any list the AI app keeps.
        """
        fresh = _account(
            self.tenant, self.owner, name="Fresh", phone="+15553330000", provider_session_id="dev-fresh",
        )

        with patch("apps.ai.agent.converse", side_effect=ScriptedModel(
            tool_turn(("lookup_customer", {})),
            text_turn("Welcome."),
        )):
            _deliver(fresh, "hi there", "wamid.in.fresh", from_number="15557778888")

        call = AIToolCall.objects.filter(name="lookup_customer").latest("id")
        self.assertEqual(call.whatsapp_account_id, fresh.pk)
        self.assertEqual(call.device_id, "dev-fresh")

        replies = [m for m in self._outbound() if m.whatsapp_account_id == fresh.pk]
        self.assertEqual(replies[-1].body, "Welcome.")

    def test_a_cloud_api_number_reports_its_phone_number_id_as_the_device(self):
        cloud = _account(
            self.tenant, self.owner, name="Cloud", phone="+15554440000",
            connection_method=WhatsAppAccount.ConnectionMethod.CLOUD_API,
        )
        self.assertEqual(tools.device_id_for(cloud), "pnid-Cloud")


# ---------------------------------------------------------------------------
# 10. RAG + tool calling together
# ---------------------------------------------------------------------------

class RagWithToolsTests(ToolBase):
    write_tools = True

    def test_retrieved_material_and_a_tool_result_are_both_available(self):
        model = self._run(ScriptedModel(
            tool_turn(("lookup_customer", {})),
            text_turn("Delivery is two working days, and I can see your account."),
        ), body="how long is delivery for my order?")

        # The per-message retrieval is in the system prompt...
        self.assertIn("two working days", model.requests[0]["system"])
        # ...and the tool result arrives as a tool_result on the second request.
        results = [
            block for turn in model.messages(1) if isinstance(turn.get("content"), list)
            for block in turn["content"] if block.get("type") == "tool_result"
        ]
        self.assertEqual(len(results), 1)
        self.assertEqual(self._outbound()[-1].body, "Delivery is two working days, and I can see your account.")

    def test_search_knowledge_finds_what_the_first_retrieval_missed(self):
        """
        The case tools exist for: the customer's message retrieves one topic
        and then asks about another.
        """
        model = self._run(ScriptedModel(
            tool_turn(("search_knowledge", {"query": "returns policy"})),
            text_turn("Fourteen days for returns."),
        ), body="what time do you open?")

        call = AIToolCall.objects.get(name="search_knowledge")
        self.assertIn("fourteen days", call.result)
        self.assertNotIn("fourteen days", model.requests[0]["system"].lower().split("reference material")[0])

    def test_knowledge_only_still_forbids_inventing(self):
        system = services.build_system_prompt(self.config, [], with_tools=True)
        self.assertIn("what your tools return", system)
        self.assertIn("hand_to_human", system)


# ---------------------------------------------------------------------------
# 11. The loop limit
# ---------------------------------------------------------------------------

class LoopLimitTests(ToolBase):
    write_tools = True

    def test_the_loop_stops_at_the_cap(self):
        # Five distinct tool turns, but the default cap is four rounds.
        model = ScriptedModel(*[
            tool_turn(("search_knowledge", {"query": f"thing {index}"})) for index in range(6)
        ])
        with patch("apps.ai.agent.converse", side_effect=model):
            _deliver(self.account, "tell me everything", "wamid.in.loop")

        self.assertEqual(model.calls, agent.DEFAULT_MAX_TOOL_ROUNDS)

    def test_a_tenant_may_lower_the_cap(self):
        self.config.max_tool_rounds = 2
        self.config.save(update_fields=["max_tool_rounds"])

        model = ScriptedModel(*[
            tool_turn(("search_knowledge", {"query": f"thing {index}"})) for index in range(6)
        ])
        with patch("apps.ai.agent.converse", side_effect=model):
            _deliver(self.account, "tell me everything", "wamid.in.loop2")

        self.assertEqual(model.calls, 2)

    def test_a_tenant_may_not_raise_the_cap_past_the_ceiling(self):
        self.config.max_tool_rounds = 500
        self.assertEqual(agent.max_rounds(self.config), agent.MAX_TOOL_ROUNDS_CEILING)

    def test_whatever_was_said_before_the_cap_still_reaches_the_customer(self):
        model = ScriptedModel(*(
            [tool_turn(("search_knowledge", {"query": "a"}), text="Let me check that.")]
            + [tool_turn(("search_knowledge", {"query": f"b{index}"})) for index in range(5)]
        ))
        with patch("apps.ai.agent.converse", side_effect=model):
            _deliver(self.account, "tell me everything", "wamid.in.loop3")

        self.assertEqual(self._outbound()[-1].body, "Let me check that.")


# ---------------------------------------------------------------------------
# 12. Loop prevention beyond the round cap
# ---------------------------------------------------------------------------

class InfiniteLoopPreventionTests(ToolBase):
    send_tools = True

    def test_the_agent_refuses_to_message_one_of_its_own_numbers(self):
        """
        The real infinite-loop vector. A message to our own connected number
        arrives back as an inbound message, which the ladder answers, which
        sends another -- billed, quota'd, and invisible until somebody reads
        the invoice.
        """
        # The "customer" on this conversation is in fact our own support line.
        _account(self.tenant, self.owner, name="Support", phone="+15551234567")

        self._run(ScriptedModel(
            tool_turn(("send_whatsapp_message", {"message": "hello"})),
            text_turn("Done."),
        ))

        call = AIToolCall.objects.get(name="send_whatsapp_message")
        self.assertTrue(call.is_error)
        self.assertIn("our own number", call.detail)
        # Only the reply went out; the tool sent nothing.
        self.assertEqual(len(self._outbound()), 1)

    def test_tool_sends_are_capped_per_turn(self):
        model = self._run(ScriptedModel(
            tool_turn(("send_whatsapp_message", {"message": "one"})),
            tool_turn(("send_whatsapp_message", {"message": "two"})),
            tool_turn(("send_whatsapp_message", {"message": "three"})),
            text_turn("All sent."),
        ))

        self.assertEqual(model.calls, 4)
        sent = AIToolCall.objects.filter(name="send_whatsapp_message").order_by("id")
        self.assertEqual(sent.count(), 3)
        self.assertFalse(sent[0].is_error)
        self.assertFalse(sent[1].is_error)
        self.assertTrue(sent[2].is_error)
        self.assertIn("send cap", sent[2].detail)

    def test_an_outbound_tool_message_does_not_re_enter_the_ladder(self):
        """
        The tool's message is outbound, and the ladder only runs on inbound.
        Asserted rather than assumed, because a tool that reached the ladder
        would answer itself for as long as the credits held out.
        """
        model = self._run(ScriptedModel(
            tool_turn(("send_whatsapp_message", {"message": "extra"})),
            text_turn("Sent."),
        ))

        # One customer message in, two ours out, and the model consulted
        # exactly twice -- once before the tool and once after. A message that
        # had re-entered the ladder would show up as a third round here.
        self.assertEqual(model.calls, 2)
        self.assertEqual(Message.objects.filter(direction=Message.Direction.INBOUND).count(), 1)
        self.assertEqual(len(self._outbound()), 2)
        self.assertEqual(AIToolCall.objects.filter(name="send_whatsapp_message").count(), 1)


# ---------------------------------------------------------------------------
# Handoff through a tool
# ---------------------------------------------------------------------------

class HandoffToolTests(ToolBase):
    def test_the_handoff_tool_takes_the_agent_off_the_conversation(self):
        self._run(ScriptedModel(
            tool_turn(("hand_to_human", {"reason": "they are upset"})),
        ), body="this is unacceptable")

        conversation = Conversation.objects.get(whatsapp_account=self.account)
        state = conversation.ai_state
        self.assertIsNotNone(state.handed_off_at)
        self.assertIn("upset", state.handoff_reason)

        # The tenant's handoff message is what the customer gets, not the
        # model's idea of one.
        self.assertEqual(self._outbound()[-1].body, self.config.handoff_message)

    def test_the_agent_stops_answering_after_a_tool_handoff(self):
        self._run(ScriptedModel(
            tool_turn(("hand_to_human", {"reason": "beyond me"})),
        ), body="I want a refund now")

        before = len(self._outbound())
        with patch("apps.ai.agent.converse", side_effect=AssertionError("must not run after handoff")):
            _deliver(self.account, "are you there?", "wamid.in.after")

        self.assertEqual(len(self._outbound()), before)


# ---------------------------------------------------------------------------
# The whole stack, with nothing above HTTP stubbed
# ---------------------------------------------------------------------------

class WireFormatTests(ToolBase):
    """
    The loop through the real provider translation.

    Every other test here patches `agent.converse`, which is right for testing
    the loop but means the translation underneath is never exercised from this
    direction. `tests_providers` covers that translation in isolation. This
    covers the join: a tool call arriving in OpenAI's wire shape, being
    decoded, run, and sent back as the `role: "tool"` turn the API requires,
    with the assistant turn ahead of it carrying its `tool_calls`.

    An unpaired tool_call is a 400 on the second request. This is the test
    that would catch it.
    """

    write_tools = True

    def _openai_tool_call(self, name, arguments):
        return {
            "choices": [{
                "message": {"content": None, "tool_calls": [{
                    "id": "call_abc", "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }]},
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }

    def _openai_text(self, text):
        return {
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }

    def test_a_tool_round_trip_survives_the_real_openai_translation(self):
        payloads = [
            self._openai_tool_call("search_knowledge", '{"query": "returns policy"}'),
            self._openai_text("You have fourteen days to return anything."),
        ]
        sent = []

        class _Response:
            status_code = 200

            def __init__(self, body):
                self._body = body

            def json(self):
                return self._body

        def _post(url, **kwargs):
            sent.append(kwargs.get("json") or {})
            return _Response(payloads.pop(0))

        with patch("apps.ai.providers.openai.requests.post", side_effect=_post):
            _deliver(self.account, "what is your returns policy?", "wamid.in.wire")

        self.assertEqual(len(sent), 2)

        # The tools reached the vendor in its own function shape.
        offered = [tool["function"]["name"] for tool in sent[0]["tools"]]
        self.assertIn("search_knowledge", offered)

        # The second request echoes the assistant turn with its tool_calls...
        assistant = [turn for turn in sent[1]["messages"] if turn["role"] == "assistant"][-1]
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_abc")
        # ...and answers it with a role:"tool" turn carrying the retrieved text.
        answer = [turn for turn in sent[1]["messages"] if turn["role"] == "tool"][-1]
        self.assertEqual(answer["tool_call_id"], "call_abc")
        self.assertIn("fourteen days", answer["content"])

        # And the customer got the reply.
        self.assertEqual(self._outbound()[-1].body, "You have fourteen days to return anything.")
        self.assertFalse(AIToolCall.objects.get(name="search_knowledge").is_error)


# ---------------------------------------------------------------------------
# Logging and status
# ---------------------------------------------------------------------------

class ToolLoggingTests(ToolBase):
    write_tools = True

    def test_the_audit_row_carries_every_owning_identifier(self):
        self._run(ScriptedModel(
            tool_turn(("tag_contact", {"tag": "Pricing"})),
            text_turn("Noted."),
        ))

        conversation = Conversation.objects.get(whatsapp_account=self.account)
        call = AIToolCall.objects.get(name="tag_contact")
        self.assertEqual(call.tenant_id, self.tenant.pk)
        self.assertEqual(call.conversation_id, conversation.pk)
        self.assertEqual(call.contact_id, conversation.contact_id)
        self.assertEqual(call.whatsapp_account_id, self.account.pk)
        self.assertEqual(call.device_id, self.account.provider_session_id)
        self.assertEqual(call.arguments, {"tag": "Pricing"})
        self.assertGreaterEqual(call.duration_ms, 0)

    def test_secret_shaped_arguments_are_redacted(self):
        outcome = tools._redact({"tag": "VIP", "api_key": "sk-live-123", "password": "hunter2"})
        self.assertEqual(outcome["tag"], "VIP")
        self.assertEqual(outcome["api_key"], "[redacted]")
        self.assertEqual(outcome["password"], "[redacted]")

    def test_the_usage_log_still_counts_tokens_across_every_round(self):
        from .models import AIUsageLog

        self._run(ScriptedModel(
            tool_turn(("search_knowledge", {"query": "returns"})),
            text_turn("Fourteen days."),
        ))

        log = AIUsageLog.objects.filter(outcome=AIUsageLog.Outcome.ANSWERED).latest("id")
        # Two rounds at 100 in / 20 out each.
        self.assertEqual(log.input_tokens, 200)
        self.assertEqual(log.output_tokens, 40)

    def test_the_status_panel_reports_the_live_tool_set(self):
        panel = status.agent_status(self.config)
        self.assertTrue(panel["tools"]["enabled"])
        self.assertIn("tag_contact", panel["tools"]["available"])
        self.assertEqual(panel["tools"]["capabilities"], ["READ", "WRITE"])
        self.assertEqual(panel["tools"]["max_rounds"], agent.DEFAULT_MAX_TOOL_ROUNDS)

    def test_a_tool_action_lands_on_the_crm_timeline(self):
        self._run(ScriptedModel(
            tool_turn(("tag_contact", {"tag": "Pricing"})),
            text_turn("Noted."),
        ))

        entry = Activity.objects.filter(kind=Activity.Kind.AUTOMATION, summary__icontains="tagged").first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.tenant_id, self.tenant.pk)
