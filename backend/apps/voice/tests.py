import base64
import hashlib
import hmac
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.ai.models import KnowledgeDocument
from apps.ai import knowledge
from apps.ai.providers.anthropic import AIProviderError, AIResponse, ToolCall, supports_sampling
from apps.contacts.models import Contact
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount

from . import agent, services, tools
from .models import CallSession, CallTurn, VoiceAgentConfig
from .providers.base import Hangup, Listen, Pause, Speak, Transfer
from .providers.twilio import TwilioVoiceProvider

BASE_URL = "https://tunnel.test"


def _reply(text="", tool_calls=None, content=None):
    """A model response, shaped the way `converse` returns one."""
    return AIResponse(
        text=text, input_tokens=120, output_tokens=30, latency_ms=400,
        tool_calls=tool_calls or [], content=content or ([{"type": "text", "text": text}] if text else []),
    )


def _tool(name, payload, call_id="tu_1"):
    return ToolCall(id=call_id, name=name, input=payload)


class _TwilioResponse:
    """
    A carrier reply.

    Used instead of a bare MagicMock because the provider compares
    `status_code` against an int, and a MagicMock is not orderable.
    """

    def __init__(self, status_code=201, payload=None):
        self.status_code = status_code
        self._payload = {"sid": "CA123", "status": "queued"} if payload is None else payload
        self.text = ""

    def json(self):
        return self._payload


@override_settings(VOICE_PUBLIC_BASE_URL=BASE_URL, VOICE_VALIDATE_SIGNATURES=False, AI_API_KEY="test-key")
class Fixture(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Acme", slug=f"voice-{self._testMethodName}"[:50], kind=Tenant.Kind.CUSTOMER,
        )
        self.owner = get_user_model().objects.create_user(
            username=f"voice-owner-{self._testMethodName}"[:140], password="x", tenant=self.tenant, credits=100,
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.owner, phone="+918429577706", name="",
        )
        self.config = VoiceAgentConfig.objects.create(
            tenant=self.tenant, is_enabled=True, from_number="+14155550100",
            transfer_number="+14155550199", greeting="Hello, this is Acme.",
            business_context="Acme sells bicycles.", max_turns=5, max_silent_turns=2,
        )
        self.config.set_account_sid("AC" + "0" * 30)
        self.config.set_auth_token("secret-token")
        self.config.save()

        self.session = CallSession.objects.create(
            tenant=self.tenant, contact=self.contact, created_by=self.owner,
            to_number="+918429577706", from_number="+14155550100",
            status=CallSession.Status.IN_PROGRESS, answered_at=timezone.now(),
        )

    def client_for(self, user=None):
        client = APIClient()
        client.force_authenticate(user or self.owner)
        return client

    def turns(self, role=None):
        queryset = CallTurn.objects.filter(session=self.session).order_by("ordinal", "id")
        if role:
            queryset = queryset.filter(role=role)
        return list(queryset)


# ---------------------------------------------------------------------------
# The model client
# ---------------------------------------------------------------------------

class ModelClientTests(TestCase):
    def test_sampling_is_dropped_for_models_that_reject_it(self):
        # The current generation returns a 400 for `temperature` rather than
        # ignoring it, so sending it is the difference between an answer and
        # an error.
        self.assertFalse(supports_sampling("claude-opus-5"))
        self.assertFalse(supports_sampling("claude-sonnet-5"))
        self.assertTrue(supports_sampling("claude-haiku-4-5"))

    def test_request_payload_omits_temperature_and_sets_adaptive_thinking(self):
        from apps.ai.providers import anthropic

        captured = {}

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"content": [{"type": "text", "text": "hi"}], "usage": {}, "stop_reason": "end_turn"}

        def _post(url, headers=None, json=None, timeout=None):
            captured.update(json)
            return _Response()

        with patch.object(anthropic.requests, "post", _post):
            anthropic.converse(
                api_key="k", model="claude-opus-5", system="s",
                messages=[{"role": "user", "content": "hi"}], temperature=0.3, effort="low",
            )

        self.assertNotIn("temperature", captured)
        self.assertEqual(captured["thinking"], {"type": "adaptive"})
        self.assertEqual(captured["output_config"], {"effort": "low"})

    def test_older_models_keep_temperature_and_lose_thinking(self):
        from apps.ai.providers import anthropic

        captured = {}

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"content": [], "usage": {}}

        def _post(url, headers=None, json=None, timeout=None):
            captured.update(json)
            return _Response()

        with patch.object(anthropic.requests, "post", _post):
            anthropic.converse(
                api_key="k", model="claude-haiku-4-5", system="s",
                messages=[{"role": "user", "content": "hi"}], temperature=0.3, effort="low",
            )

        self.assertEqual(captured["temperature"], 0.3)
        self.assertNotIn("thinking", captured)
        self.assertNotIn("output_config", captured)

    def test_tool_calls_are_decoded(self):
        from apps.ai.providers import anthropic

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "content": [
                        {"type": "text", "text": "One moment."},
                        {"type": "tool_use", "id": "tu_9", "name": "end_call", "input": {"farewell": "Bye."}},
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 7},
                    "stop_reason": "tool_use",
                }

        with patch.object(anthropic.requests, "post", lambda *a, **kw: _Response()):
            response = anthropic.converse(
                api_key="k", model="claude-opus-5", system="", messages=[{"role": "user", "content": "x"}],
            )

        self.assertEqual(response.text, "One moment.")
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].name, "end_call")
        self.assertEqual(response.tool_calls[0].input, {"farewell": "Bye."})
        # The raw blocks have to survive so the tool_results can pair with them.
        self.assertEqual(len(response.content), 2)


# ---------------------------------------------------------------------------
# TwiML
# ---------------------------------------------------------------------------

class TwiMLTests(TestCase):
    def setUp(self):
        self.provider = TwilioVoiceProvider()

    def render(self, directives, voice="Polly.Aditi", language="en-IN"):
        return self.provider.render(directives, voice=voice, language=language)

    def test_speak_escapes_model_output(self):
        # An ampersand in a product name would otherwise be malformed XML,
        # which the carrier turns into a dropped call.
        xml = self.render([Speak("Bikes & Parts costs <2000>")])
        self.assertIn("Bikes &amp; Parts costs &lt;2000&gt;", xml)
        self.assertNotIn("<2000>", xml)

    def test_listen_arms_the_recogniser_with_the_prompt_inside(self):
        xml = self.render([Listen(action_url="https://x.test/turn/", lines=["Hello."], hints="Acme, bicycle")])
        self.assertIn('<Gather input="speech"', xml)
        self.assertIn('action="https://x.test/turn/"', xml)
        self.assertIn('speechTimeout="auto"', xml)
        # Without this, silence falls through to the next verb and the engine
        # never learns nobody spoke.
        self.assertIn('actionOnEmptyResult="true"', xml)
        self.assertIn('hints="Acme, bicycle"', xml)
        # The prompt is nested so the caller can interrupt it.
        self.assertIn("<Gather", xml.split("<Say")[0])

    def test_polly_voice_does_not_get_a_language_override(self):
        self.assertNotIn("language=", self.render([Speak("Hi")], voice="Polly.Aditi"))
        self.assertIn('language="en-IN"', self.render([Speak("Hi")], voice="woman"))

    def test_transfer_and_hangup(self):
        xml = self.render([Transfer(number="+14155550199", caller_id="+14155550100", lines=["Connecting you."])])
        self.assertIn("<Say", xml)
        self.assertIn('callerId="+14155550100"', xml)
        self.assertIn("<Dial", xml)
        self.assertIn("+14155550199</Dial>", xml)

        xml = self.render([Hangup(lines=["Goodbye."])])
        self.assertIn("Goodbye.", xml)
        self.assertTrue(xml.endswith("<Hangup/></Response>"))

    def test_pause(self):
        self.assertIn('<Pause length="2"/>', self.render([Pause(seconds=2)]))


class SignatureTests(Fixture):
    def _sign(self, url, params, token="secret-token"):
        payload = url + "".join(key + params[key] for key in sorted(params))
        return base64.b64encode(
            hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
        ).decode()

    @override_settings(VOICE_VALIDATE_SIGNATURES=True)
    def test_a_correctly_signed_webhook_is_accepted(self):
        url = services.callback_url(self.session, "turn")
        params = {"CallSid": "CA1", "SpeechResult": "hello there"}

        with patch.object(agent, "converse", return_value=_reply("Hello.")):
            response = self.client.post(
                f"/voice/turn/{self.session.webhook_token}/", params,
                HTTP_X_TWILIO_SIGNATURE=self._sign(url, params),
            )

        self.assertEqual(response.status_code, 200)

    @override_settings(VOICE_VALIDATE_SIGNATURES=True)
    def test_an_unsigned_webhook_is_rejected(self):
        response = self.client.post(
            f"/voice/turn/{self.session.webhook_token}/", {"CallSid": "CA1", "SpeechResult": "hello"},
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(VOICE_VALIDATE_SIGNATURES=True)
    def test_a_signature_from_the_wrong_token_is_rejected(self):
        url = services.callback_url(self.session, "turn")
        params = {"CallSid": "CA1", "SpeechResult": "hello"}
        response = self.client.post(
            f"/voice/turn/{self.session.webhook_token}/", params,
            HTTP_X_TWILIO_SIGNATURE=self._sign(url, params, token="not-the-token"),
        )
        self.assertEqual(response.status_code, 403)

    def test_an_unknown_call_token_is_a_404(self):
        self.assertEqual(self.client.post("/voice/turn/nope/").status_code, 404)


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

class AnswerTests(Fixture):
    def test_answering_greets_and_listens(self):
        directives = services.on_answer(self.session, TwilioVoiceProvider().parse_turn(_Post({"CallSid": "CA1"})))

        self.assertEqual(len(directives), 1)
        self.assertIsInstance(directives[0], Listen)
        self.assertEqual(directives[0].lines, ["Hello, this is Acme."])
        # The greeting is in the transcript, so the next turn's history starts
        # with what the agent actually said.
        self.assertEqual([turn.text for turn in self.turns(CallTurn.Role.AGENT)], ["Hello, this is Acme."])

    def test_an_answering_machine_gets_the_voicemail_message_and_a_hangup(self):
        self.config.voicemail_message = "Please call Acme back."
        self.config.save()

        directives = services.on_answer(
            self.session, TwilioVoiceProvider().parse_turn(_Post({"AnsweredBy": "machine_end_beep"})),
        )

        self.assertIsInstance(directives[0], Pause)
        self.assertIsInstance(directives[1], Hangup)
        self.assertEqual(directives[1].lines, ["Please call Acme back."])
        self.session.refresh_from_db()
        self.assertEqual(self.session.outcome, CallSession.Outcome.VOICEMAIL)

    def test_a_machine_with_no_message_configured_just_hangs_up(self):
        directives = services.on_answer(
            self.session, TwilioVoiceProvider().parse_turn(_Post({"AnsweredBy": "machine_start"})),
        )
        self.assertEqual(len(directives), 1)
        self.assertIsInstance(directives[0], Hangup)
        self.assertEqual(directives[0].lines, [])


class _Post:
    """The bits of an HttpRequest the providers read."""

    def __init__(self, post):
        self.POST = post
        self.headers = {}


class TurnTests(Fixture):
    def turn(self, speech, confidence=0.9):
        return TwilioVoiceProvider().parse_turn(
            _Post({"SpeechResult": speech, "Confidence": str(confidence), "CallSid": "CA1"})
        )

    def test_a_normal_exchange_answers_and_keeps_listening(self):
        with patch.object(agent, "converse", return_value=_reply("We open at nine.")) as mock:
            directives = services.on_turn(self.session, self.turn("what time do you open"))

        self.assertIsInstance(directives[0], Listen)
        self.assertEqual(directives[0].lines, ["We open at nine."])

        roles = [(turn.role, turn.text) for turn in self.turns()]
        self.assertEqual(roles, [
            (CallTurn.Role.CALLER, "what time do you open"),
            (CallTurn.Role.AGENT, "We open at nine."),
        ])

        self.session.refresh_from_db()
        self.assertEqual(self.session.turns_used, 1)
        self.assertEqual(self.session.input_tokens, 120)
        self.assertEqual(self.session.output_tokens, 30)

        # The caller's words reach the model as the last user turn.
        messages = mock.call_args.kwargs["messages"]
        self.assertEqual(messages[-1], {"role": "user", "content": "what time do you open"})

    def test_the_business_context_and_goal_reach_the_prompt(self):
        self.session.goal = "Remind them their bicycle service is due."
        self.session.context = {"order": "A-91"}
        self.session.save()

        with patch.object(agent, "converse", return_value=_reply("Sure.")) as mock:
            services.on_turn(self.session, self.turn("hello"))

        system = mock.call_args.kwargs["system"]
        self.assertIn("Acme sells bicycles.", system)
        self.assertIn("Remind them their bicycle service is due.", system)
        self.assertIn("order: A-91", system)
        # The speech rules are what stop it reading markdown down a phone line.
        self.assertIn("No markdown", system)

    def test_end_call_hangs_up_and_says_goodbye_first(self):
        response = _reply(tool_calls=[_tool("end_call", {"farewell": "Thanks, goodbye."})])
        with patch.object(agent, "converse", return_value=response):
            directives = services.on_turn(self.session, self.turn("no thanks, bye"))

        self.assertIsInstance(directives[0], Hangup)
        self.assertEqual(directives[0].lines, ["Thanks, goodbye."])
        self.session.refresh_from_db()
        self.assertEqual(self.session.outcome, CallSession.Outcome.AGENT_ENDED)

    def test_transfer_bridges_the_call_to_a_person(self):
        response = _reply(tool_calls=[_tool("transfer_to_human", {"farewell": "Putting you through."})])
        with patch.object(agent, "converse", return_value=response):
            directives = services.on_turn(self.session, self.turn("let me speak to someone"))

        self.assertIsInstance(directives[0], Transfer)
        self.assertEqual(directives[0].number, "+14155550199")
        self.assertEqual(directives[0].lines, ["Putting you through."])
        self.session.refresh_from_db()
        self.assertEqual(self.session.outcome, CallSession.Outcome.TRANSFERRED)

    def test_a_tool_result_goes_back_for_a_second_round(self):
        document = KnowledgeDocument.objects.create(
            tenant=self.tenant, created_by=self.owner, title="Hours",
            content="Our opening hours are nine in the morning until six in the evening.",
        )
        knowledge.rebuild(document)

        first = _reply(
            tool_calls=[_tool("search_knowledge", {"query": "opening hours"})],
            content=[{"type": "tool_use", "id": "tu_1", "name": "search_knowledge", "input": {}}],
        )
        second = _reply("We're open nine to six.")
        with patch.object(agent, "converse", side_effect=[first, second]) as mock:
            directives = services.on_turn(self.session, self.turn("when are you open"))

        self.assertEqual(mock.call_count, 2)
        self.assertEqual(directives[0].lines, ["We're open nine to six."])

        # The second request carries the assistant turn and the tool_result.
        messages = mock.call_args_list[1].kwargs["messages"]
        self.assertEqual(messages[-2]["role"], "assistant")
        self.assertEqual(messages[-1]["content"][0]["type"], "tool_result")
        self.assertIn("nine in the morning", messages[-1]["content"][0]["content"])

        self.assertIn("search_knowledge", [turn.text for turn in self.turns(CallTurn.Role.TOOL)][0])

    def test_a_repeated_tool_call_is_only_run_once(self):
        # A model that loops on a tool is a known failure mode. Left alone it
        # would send the same WhatsApp message three times and read the same
        # sentence out three times.
        looping = _reply(
            "Sending that over now.",
            tool_calls=[_tool("save_details", {"name": "Aman"})],
            content=[{"type": "tool_use", "id": "tu_1", "name": "save_details", "input": {"name": "Aman"}}],
        )
        with patch.object(agent, "converse", return_value=looping) as mock:
            directives = services.on_turn(self.session, self.turn("my name is Aman"))

        # Round one runs the tool; round two asks for the same thing and is
        # answered from the first result, at which point the loop stops.
        self.assertEqual(mock.call_count, 2)
        self.assertEqual(len(self.turns(CallTurn.Role.TOOL)), 1)
        self.assertEqual(directives[0].lines, ["Sending that over now."])

    def test_a_repeated_side_effect_only_happens_once(self):
        account = WhatsAppAccount(
            tenant=self.tenant, user=self.owner, name="Line", phone_number_id="1",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        account.set_access_token("token")
        account.set_app_secret("secret")
        account.save()

        looping = _reply(
            tool_calls=[_tool("send_whatsapp", {"message": "12 Main Street."})],
            content=[{"type": "tool_use", "id": "tu_1", "name": "send_whatsapp", "input": {"message": "x"}}],
        )
        with patch("apps.messaging.tasks.send_message_task.delay"):
            with patch.object(agent, "converse", return_value=looping):
                services.on_turn(self.session, self.turn("send me the address"))

        self.assertEqual(Message.objects.filter(contact=self.contact).count(), 1)

    def test_a_model_outage_keeps_the_call_alive(self):
        with patch.object(agent, "converse", side_effect=AIProviderError("boom")):
            directives = services.on_turn(self.session, self.turn("hello"))

        # Still listening: the person is holding a phone, so the failure has to
        # end in something being said rather than in a 500.
        self.assertIsInstance(directives[0], Listen)
        self.assertEqual(directives[0].lines, [services.RECOVERY_LINE])
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, CallSession.Status.IN_PROGRESS)
        self.assertFalse(self.session.outcome)
        self.assertIn("Model error", [turn.text for turn in self.turns(CallTurn.Role.SYSTEM)][0])

    def test_silence_is_re_prompted_once_and_then_given_up_on(self):
        directives = services.on_turn(self.session, self.turn(""))
        self.assertIsInstance(directives[0], Listen)
        self.assertEqual(directives[0].lines, [services.SILENCE_LINE])

        services.on_turn(self.session, self.turn(""))
        directives = services.on_turn(self.session, self.turn(""))

        self.assertIsInstance(directives[0], Hangup)
        self.session.refresh_from_db()
        self.assertEqual(self.session.outcome, CallSession.Outcome.NO_SPEECH)
        # Silence never counts as a turn of conversation.
        self.assertEqual(self.session.turns_used, 0)

    def test_speech_after_silence_resets_the_counter(self):
        services.on_turn(self.session, self.turn(""))
        with patch.object(agent, "converse", return_value=_reply("Yes?")):
            services.on_turn(self.session, self.turn("sorry, I'm here"))

        self.session.refresh_from_db()
        self.assertEqual(self.session.silent_turns, 0)

    def test_the_turn_limit_ends_the_call(self):
        self.session.turns_used = 5
        self.session.save()

        with patch.object(agent, "converse") as mock:
            directives = services.on_turn(self.session, self.turn("and another thing"))

        mock.assert_not_called()
        self.assertIsInstance(directives[0], Hangup)
        self.session.refresh_from_db()
        self.assertEqual(self.session.outcome, CallSession.Outcome.LIMIT_REACHED)

    def test_the_wall_clock_limit_ends_the_call(self):
        self.config.max_call_seconds = 60
        self.config.save()
        self.session.answered_at = timezone.now() - timedelta(seconds=120)
        self.session.save()

        with patch.object(agent, "converse") as mock:
            directives = services.on_turn(self.session, self.turn("still talking"))

        mock.assert_not_called()
        self.assertIsInstance(directives[0], Hangup)

    def test_history_is_rebuilt_from_the_transcript(self):
        services.record(self.session, CallTurn.Role.AGENT, "Hello, this is Acme.")
        services.record(self.session, CallTurn.Role.CALLER, "Hi.")
        services.record(self.session, CallTurn.Role.AGENT, "How can I help?")
        services.record(self.session, CallTurn.Role.TOOL, "save_details: Saved.")
        services.record(self.session, CallTurn.Role.CALLER, "I need a service")

        history = agent.build_history(self.session)

        # Tool turns are transcript-only, and the conversation must start with
        # the other party.
        self.assertEqual(history, [
            {"role": "user", "content": "Hi."},
            {"role": "assistant", "content": "How can I help?"},
            {"role": "user", "content": "I need a service"},
        ])


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class ToolTests(Fixture):
    def test_save_details_fills_blank_contact_fields_only(self):
        self.contact.name = "Existing Name"
        self.contact.save()

        outcome = tools.execute(
            "save_details", {"name": "Misheard Name", "email": "a@b.test", "interest": "road bike"},
            self.config, self.session,
        )

        self.assertFalse(outcome.is_error)
        self.contact.refresh_from_db()
        # A name typed by the business beats one heard down a phone line.
        self.assertEqual(self.contact.name, "Existing Name")
        self.assertEqual(self.contact.email, "a@b.test")
        self.assertEqual(self.contact.custom_fields["call_name"], "Misheard Name")

        self.session.refresh_from_db()
        self.assertEqual(self.session.collected["interest"], "road bike")

    def test_notes_accumulate_rather_than_overwrite(self):
        tools.execute("save_details", {"notes": "Wants a quote."}, self.config, self.session)
        tools.execute("save_details", {"notes": "Prefers email."}, self.config, self.session)

        self.session.refresh_from_db()
        self.assertEqual(self.session.collected["notes"], "Wants a quote.\nPrefers email.")

    def test_search_knowledge_returns_the_tenants_own_material(self):
        document = KnowledgeDocument.objects.create(
            tenant=self.tenant, created_by=self.owner, title="Delivery",
            content="Delivery inside Mumbai takes two working days and costs sixty rupees.",
        )
        knowledge.rebuild(document)

        outcome = tools.execute("search_knowledge", {"query": "delivery Mumbai"}, self.config, self.session)
        self.assertIn("two working days", outcome.result)

    def test_search_knowledge_says_so_when_nothing_matches(self):
        outcome = tools.execute("search_knowledge", {"query": "helicopters"}, self.config, self.session)
        self.assertIn("Nothing in the reference material", outcome.result)

    def test_send_whatsapp_queues_a_service_message(self):
        account = WhatsAppAccount(
            tenant=self.tenant, user=self.owner, name="Line", phone_number_id="1",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        account.set_access_token("token")
        account.set_app_secret("secret")
        account.save()

        with patch("apps.messaging.tasks.send_message_task.delay"):
            outcome = tools.execute(
                "send_whatsapp", {"message": "Here is our address: 12 Main Street."}, self.config, self.session,
            )

        self.assertFalse(outcome.is_error)
        message = Message.objects.get(contact=self.contact)
        self.assertEqual(message.body, "Here is our address: 12 Main Street.")
        # SERVICE, so it goes out now rather than waiting for a sending window.
        self.assertEqual(message.kind, Message.Kind.SERVICE)

    def test_send_whatsapp_reports_when_no_number_is_connected(self):
        outcome = tools.execute("send_whatsapp", {"message": "hi"}, self.config, self.session)
        self.assertTrue(outcome.is_error)
        self.assertIn("No WhatsApp number is connected", outcome.result)

    def test_schedule_callback_books_a_second_call(self):
        when = (timezone.now() + timedelta(days=1)).isoformat()

        with patch("apps.voice.tasks.place_call_task.apply_async") as dispatch:
            outcome = tools.execute(
                "schedule_callback", {"when": when, "note": "Discuss the quote."}, self.config, self.session,
            )

        self.assertFalse(outcome.is_error)
        dispatch.assert_called_once()
        callback = CallSession.objects.exclude(pk=self.session.pk).get()
        self.assertEqual(callback.to_number, self.session.to_number)
        self.assertEqual(callback.goal, "Discuss the quote.")

    def test_schedule_callback_refuses_a_time_in_the_past(self):
        when = (timezone.now() - timedelta(hours=1)).isoformat()
        with patch("apps.voice.tasks.place_call_task.apply_async") as dispatch:
            outcome = tools.execute("schedule_callback", {"when": when}, self.config, self.session)

        self.assertTrue(outcome.is_error)
        dispatch.assert_not_called()

    def test_transfer_is_refused_when_there_is_nobody_to_transfer_to(self):
        self.config.transfer_number = ""
        outcome = tools.execute("transfer_to_human", {"farewell": "One moment."}, self.config, self.session)

        self.assertTrue(outcome.is_error)
        self.assertFalse(outcome.terminal)

    def test_an_unknown_tool_produces_a_result_rather_than_an_exception(self):
        # An unanswered tool_use is a 400 on the next request, which mid-call
        # is a dropped call.
        outcome = tools.execute("launch_rocket", {}, self.config, self.session)
        self.assertTrue(outcome.is_error)
        self.assertIn("no tool called", outcome.result)

    def test_a_crashing_tool_still_produces_a_result(self):
        def _boom(payload, config, session):
            raise RuntimeError("boom")

        with patch.dict(tools._HANDLERS, {"save_details": _boom}):
            outcome = tools.execute("save_details", {"name": "x"}, self.config, self.session)
        self.assertTrue(outcome.is_error)

    def test_the_tool_list_follows_the_config(self):
        names = {tool["name"] for tool in tools.definitions(self.config)}
        self.assertEqual(names, {
            "end_call", "save_details", "search_knowledge",
            "transfer_to_human", "send_whatsapp", "schedule_callback",
        })

        self.config.allow_transfer = False
        self.config.allow_whatsapp_followup = False
        self.config.allow_callback_scheduling = False
        names = {tool["name"] for tool in tools.definitions(self.config)}
        self.assertEqual(names, {"end_call", "save_details", "search_knowledge"})


# ---------------------------------------------------------------------------
# Placing calls and lifecycle
# ---------------------------------------------------------------------------

class PlaceCallTests(Fixture):
    def test_placing_a_call_asks_the_carrier_to_dial(self):
        session = CallSession.objects.create(
            tenant=self.tenant, contact=self.contact, to_number="+918429577706", provider="TWILIO",
        )

        class _Response:
            status_code = 201

            @staticmethod
            def json():
                return {"sid": "CA123", "status": "queued"}

        with patch("apps.voice.providers.twilio.requests.post", return_value=_Response()) as post:
            services.place_call(session)

        payload = dict(post.call_args.kwargs["data"])
        self.assertEqual(payload["To"], "+918429577706")
        self.assertEqual(payload["From"], "+14155550100")
        self.assertEqual(payload["Url"], f"{BASE_URL}/voice/answer/{session.webhook_token}/")
        self.assertEqual(payload["StatusCallback"], f"{BASE_URL}/voice/status/{session.webhook_token}/")
        # No voicemail message configured, so no answering-machine detection
        # and no dead air on the calls a human picks up.
        self.assertNotIn("MachineDetection", payload)

        session.refresh_from_db()
        self.assertEqual(session.provider_call_id, "CA123")
        self.assertEqual(session.status, CallSession.Status.RINGING)

    def test_machine_detection_is_only_asked_for_when_there_is_a_message_to_leave(self):
        self.config.voicemail_message = "Call us back."
        self.config.save()
        session = CallSession.objects.create(tenant=self.tenant, to_number="+918429577706")

        class _Response:
            status_code = 201

            @staticmethod
            def json():
                return {"sid": "CA1"}

        with patch("apps.voice.providers.twilio.requests.post", return_value=_Response()) as post:
            services.place_call(session)

        self.assertEqual(dict(post.call_args.kwargs["data"])["MachineDetection"], "DetectMessageEnd")

    def test_a_refused_call_is_recorded_and_not_retried(self):
        session = CallSession.objects.create(tenant=self.tenant, to_number="+918429577706")

        class _Response:
            status_code = 400

            @staticmethod
            def json():
                return {"message": "The 'To' number is not valid."}

        with patch("apps.voice.providers.twilio.requests.post", return_value=_Response()):
            services.place_call(session)

        session.refresh_from_db()
        self.assertEqual(session.status, CallSession.Status.FAILED)
        self.assertEqual(session.outcome, CallSession.Outcome.ERROR)
        self.assertIn("not valid", session.error)

    def test_start_call_refuses_without_a_public_callback_url(self):
        with override_settings(VOICE_PUBLIC_BASE_URL="", BACKEND_PUBLIC_URL=""):
            with self.assertRaises(services.VoiceNotConfigured) as caught:
                services.start_call(self.tenant, "+918429577706")
        self.assertIn("VOICE_PUBLIC_BASE_URL", str(caught.exception))

    def test_start_call_respects_the_daily_limit(self):
        self.config.daily_call_limit = 1
        self.config.save()

        with self.assertRaises(services.VoiceNotConfigured) as caught:
            services.start_call(self.tenant, "+918429577706")
        self.assertIn("daily limit", str(caught.exception))

    def test_a_duplicate_task_delivery_does_not_dial_twice(self):
        from .tasks import place_call_task

        self.session.status = CallSession.Status.RINGING
        self.session.save()

        with patch("apps.voice.providers.twilio.requests.post") as post:
            place_call_task(self.session.pk)

        post.assert_not_called()


class StatusTests(Fixture):
    def status_update(self, payload):
        return TwilioVoiceProvider().parse_status(_Post(payload))

    def test_completion_finishes_the_call_and_queues_a_summary(self):
        self.session.turns_used = 3
        self.session.save()

        with patch("apps.voice.tasks.summarise_call_task.delay") as summarise:
            services.on_status(self.session, self.status_update({"CallStatus": "completed", "CallDuration": "42"}))

        self.session.refresh_from_db()
        self.assertEqual(self.session.status, CallSession.Status.COMPLETED)
        self.assertEqual(self.session.duration_seconds, 42)
        self.assertEqual(self.session.outcome, CallSession.Outcome.CALLER_ENDED)
        self.assertIsNotNone(self.session.ended_at)
        summarise.assert_called_once_with(self.session.pk)

    def test_the_agents_own_outcome_survives_the_carriers_report(self):
        services.finish(self.session, CallSession.Outcome.TRANSFERRED)

        with patch("apps.voice.tasks.summarise_call_task.delay"):
            services.on_status(self.session, self.status_update({"CallStatus": "completed"}))

        self.session.refresh_from_db()
        self.assertEqual(self.session.outcome, CallSession.Outcome.TRANSFERRED)

    def test_no_answer_is_recorded(self):
        services.on_status(self.session, self.status_update({"CallStatus": "no-answer"}))
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, CallSession.Status.NO_ANSWER)
        self.assertEqual(self.session.outcome, CallSession.Outcome.NO_SPEECH)

    def test_a_recording_url_is_stored(self):
        services.on_status(self.session, self.status_update({"RecordingUrl": "https://api.twilio.test/r/1"}))
        self.session.refresh_from_db()
        self.assertEqual(self.session.recording_url, "https://api.twilio.test/r/1")


# ---------------------------------------------------------------------------
# Webhooks end to end
# ---------------------------------------------------------------------------

class WebhookTests(Fixture):
    def test_the_whole_exchange_over_http(self):
        answer = self.client.post(f"/voice/answer/{self.session.webhook_token}/", {"CallSid": "CA1"})
        self.assertEqual(answer.status_code, 200)
        self.assertIn("text/xml", answer["Content-Type"])
        body = answer.content.decode()
        self.assertIn("Hello, this is Acme.", body)
        self.assertIn("<Gather", body)
        self.assertIn(f"/voice/turn/{self.session.webhook_token}/", body)

        with patch.object(agent, "converse", return_value=_reply("We sell bicycles.")):
            turn = self.client.post(
                f"/voice/turn/{self.session.webhook_token}/",
                {"SpeechResult": "what do you sell", "Confidence": "0.94", "CallSid": "CA1"},
            )

        self.assertEqual(turn.status_code, 200)
        self.assertIn("We sell bicycles.", turn.content.decode())

        with patch("apps.voice.tasks.summarise_call_task.delay"):
            done = self.client.post(
                f"/voice/status/{self.session.webhook_token}/", {"CallStatus": "completed", "CallDuration": "31"},
            )
        self.assertEqual(done.status_code, 204)

        self.session.refresh_from_db()
        self.assertEqual(self.session.status, CallSession.Status.COMPLETED)
        self.assertEqual(self.session.duration_seconds, 31)

    def test_a_webhook_after_the_call_ended_does_not_restart_the_conversation(self):
        self.session.status = CallSession.Status.COMPLETED
        self.session.save()

        with patch.object(agent, "converse") as mock:
            response = self.client.post(
                f"/voice/turn/{self.session.webhook_token}/", {"SpeechResult": "hello", "CallSid": "CA1"},
            )

        mock.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<Gather", response.content.decode())

    def test_a_render_failure_still_returns_playable_xml(self):
        with patch.object(services, "render", side_effect=RuntimeError("boom")):
            response = self.client.post(f"/voice/answer/{self.session.webhook_token}/", {"CallSid": "CA1"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("<Hangup/>", response.content.decode())

    def test_an_incoming_call_creates_a_session_and_greets(self):
        self.config.answer_inbound = True
        self.config.inbound_greeting = "Acme Cycles, how can I help?"
        self.config.save()

        response = self.client.post(
            f"/voice/incoming/{self.config.inbound_token}/",
            {"From": "+919812345678", "To": "+14155550100", "CallSid": "CA-IN-1"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Acme Cycles, how can I help?", body)
        self.assertIn("<Gather", body)

        session = CallSession.objects.get(direction=CallSession.Direction.INBOUND)
        # `to_number` is the person on the other end, which is what every other
        # part of the app means by it -- not the number they dialled.
        self.assertEqual(session.to_number, "+919812345678")
        self.assertEqual(session.provider_call_id, "CA-IN-1")
        self.assertEqual(session.status, CallSession.Status.IN_PROGRESS)
        # The turn webhook for this new call is what the Gather points at.
        self.assertIn(f"/voice/turn/{session.webhook_token}/", body)

    def test_an_unknown_caller_gets_a_contact_record(self):
        self.config.answer_inbound = True
        self.config.save()

        self.client.post(
            f"/voice/incoming/{self.config.inbound_token}/",
            {"From": "+919812345678", "CallSid": "CA-IN-2"},
        )

        contact = Contact.objects.get(phone="+919812345678")
        self.assertEqual(contact.tenant, self.tenant)
        self.assertEqual(contact.source, "voice")

    def test_a_known_caller_is_matched_rather_than_duplicated(self):
        self.config.answer_inbound = True
        self.config.save()

        self.client.post(
            f"/voice/incoming/{self.config.inbound_token}/",
            {"From": self.contact.phone, "CallSid": "CA-IN-3"},
        )

        self.assertEqual(Contact.objects.filter(phone=self.contact.phone).count(), 1)
        session = CallSession.objects.get(direction=CallSession.Direction.INBOUND)
        self.assertEqual(session.contact, self.contact)

    def test_incoming_calls_hang_up_quietly_when_answering_is_off(self):
        response = self.client.post(
            f"/voice/incoming/{self.config.inbound_token}/", {"From": "+919812345678", "CallSid": "CA-IN-4"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("<Hangup/>", response.content.decode())
        self.assertNotIn("<Gather", response.content.decode())
        self.assertFalse(CallSession.objects.filter(direction=CallSession.Direction.INBOUND).exists())

    def test_the_inbound_webhook_url_is_exposed_for_pasting_into_twilio(self):
        response = self.client_for().get("/api/v1/voice/agent/")
        self.assertEqual(
            response.data["inbound_webhook_url"],
            f"{BASE_URL}/voice/incoming/{self.config.inbound_token}/",
        )

    def test_webhooks_reject_get(self):
        self.assertEqual(self.client.get(f"/voice/answer/{self.session.webhook_token}/").status_code, 405)


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------

class ConfigAPITests(Fixture):
    def test_secrets_go_in_but_never_come_out(self):
        response = self.client_for().get("/api/v1/voice/agent/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("auth_token", response.data)
        self.assertNotIn("account_sid", response.data)
        self.assertTrue(response.data["has_carrier_credentials"])
        self.assertEqual(response.data["account_sid_hint"], "...0000")

    def test_a_blank_secret_leaves_the_stored_one_alone(self):
        response = self.client_for().patch(
            "/api/v1/voice/agent/", {"account_sid": "", "greeting": "New greeting."}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.config.refresh_from_db()
        self.assertEqual(self.config.get_account_sid(), "AC" + "0" * 30)
        self.assertEqual(self.config.greeting, "New greeting.")

    def test_the_agent_cannot_be_switched_on_without_credentials(self):
        self.config.set_account_sid(None)
        self.config.set_auth_token(None)
        self.config.is_enabled = False
        self.config.save()

        response = self.client_for().patch("/api/v1/voice/agent/", {"is_enabled": True}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Twilio Account SID", str(response.data))

    def test_transfers_need_a_number_to_transfer_to(self):
        response = self.client_for().patch(
            "/api/v1/voice/agent/", {"allow_transfer": True, "transfer_number": ""}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("transfer_number", response.data)

    def test_numbers_must_be_in_international_format(self):
        response = self.client_for().patch("/api/v1/voice/agent/", {"from_number": "8429577706"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("+", str(response.data["from_number"]))

    def test_clearing_the_carrier_credentials_also_switches_the_agent_off(self):
        response = self.client_for().post(
            "/api/v1/voice/agent/clear-secret/", {"target": "carrier"}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.config.refresh_from_db()
        self.assertIsNone(self.config.get_account_sid())
        self.assertFalse(self.config.is_enabled)

    def test_readiness_names_what_is_missing(self):
        with override_settings(VOICE_PUBLIC_BASE_URL="", BACKEND_PUBLIC_URL=""):
            response = self.client_for().get("/api/v1/voice/readiness/")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["ready"])
        self.assertFalse(response.data["checks"]["public_callback_url"])
        self.assertIn("VOICE_PUBLIC_BASE_URL", " ".join(response.data["blocking"]))

    def test_readiness_is_green_when_everything_is_set(self):
        response = self.client_for().get("/api/v1/voice/readiness/")
        self.assertTrue(response.data["ready"])


class CallAPITests(Fixture):
    def test_placing_a_call_from_the_api(self):
        with patch("apps.voice.tasks.place_call_task.delay") as dispatch:
            response = self.client_for().post(
                "/api/v1/voice/calls/call/",
                {"to_number": "+918429577706", "goal": "Confirm the appointment.", "contact": self.contact.pk},
                format="json",
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["to_number"], "+918429577706")
        self.assertEqual(response.data["status"], CallSession.Status.QUEUED)
        dispatch.assert_called_once()

    def test_a_local_number_is_refused_before_it_costs_anything(self):
        response = self.client_for().post(
            "/api/v1/voice/calls/call/", {"to_number": "8429577706"}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_calling_a_list_of_contacts_reports_per_contact(self):
        opted_out = Contact.objects.create(
            tenant=self.tenant, created_by=self.owner, phone="+919000000001", subscribed=False,
        )

        with patch("apps.voice.tasks.place_call_task.delay"):
            response = self.client_for().post(
                "/api/v1/voice/calls/call-contacts/",
                {"contacts": [self.contact.pk, opted_out.pk, 99999], "goal": "Service reminder."},
                format="json",
            )

        self.assertEqual(response.status_code, 207)
        self.assertEqual(response.data["succeeded"], 1)
        self.assertEqual(response.data["failed"], 2)
        reasons = {error["id"]: error["detail"] for error in response.data["errors"]}
        self.assertIn("opted out", reasons[opted_out.pk])
        self.assertEqual(reasons[99999], "Not found.")

    def test_a_call_cannot_be_read_across_tenants(self):
        other = Tenant.objects.create(name="Other", slug=f"other-{self._testMethodName}"[:50], kind=Tenant.Kind.CUSTOMER)
        intruder = get_user_model().objects.create_user(
            username=f"intruder-{self._testMethodName}"[:140], password="x", tenant=other,
        )
        response = self.client_for(intruder).get(f"/api/v1/voice/calls/{self.session.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_the_transcript_comes_back_with_the_call(self):
        services.record(self.session, CallTurn.Role.AGENT, "Hello.")
        services.record(self.session, CallTurn.Role.CALLER, "Hi there.")

        response = self.client_for().get(f"/api/v1/voice/calls/{self.session.pk}/")
        self.assertEqual([turn["text"] for turn in response.data["turns"]], ["Hello.", "Hi there."])

    def test_hanging_up_a_live_call(self):
        # Only a call the carrier has given us an id for can be hung up there.
        self.session.provider_call_id = "CA123"
        self.session.save()

        with patch("apps.voice.providers.twilio.requests.post", return_value=_TwilioResponse(200)) as post:
            response = self.client_for().post(f"/api/v1/voice/calls/{self.session.pk}/hangup/")

        self.assertEqual(response.status_code, 200)
        post.assert_called_once()
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, CallSession.Status.CANCELED)

    def test_hanging_up_a_finished_call_is_refused(self):
        self.session.status = CallSession.Status.COMPLETED
        self.session.save()
        response = self.client_for().post(f"/api/v1/voice/calls/{self.session.pk}/hangup/")
        self.assertEqual(response.status_code, 400)

    def test_bulk_cancel_reports_per_call(self):
        finished = CallSession.objects.create(
            tenant=self.tenant, to_number="+919000000002", status=CallSession.Status.COMPLETED,
        )

        with patch("apps.voice.providers.twilio.requests.post", return_value=_TwilioResponse(200)):
            response = self.client_for().post(
                "/api/v1/voice/calls/bulk/",
                {"action": "cancel", "ids": [self.session.pk, finished.pk]}, format="json",
            )

        self.assertEqual(response.status_code, 207)
        self.assertEqual(response.data["succeeded"], 1)
        self.assertIn("already finished", response.data["errors"][0]["detail"])

    def test_the_summary_endpoint_counts_calls(self):
        self.session.status = CallSession.Status.COMPLETED
        self.session.outcome = CallSession.Outcome.AGENT_ENDED
        self.session.duration_seconds = 60
        self.session.turns_used = 4
        self.session.save()

        response = self.client_for().get("/api/v1/voice/calls/summary/")
        self.assertEqual(response.data["calls"], 1)
        self.assertEqual(response.data["talk_time_seconds"], 60)
        self.assertEqual(response.data["by_outcome"], {CallSession.Outcome.AGENT_ENDED: 1})

    def test_the_api_needs_authentication(self):
        self.assertEqual(APIClient().get("/api/v1/voice/calls/").status_code, 401)
