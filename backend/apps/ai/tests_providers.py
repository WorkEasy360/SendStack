"""
The provider layer: one internal shape, more than one vendor behind it.

Most of what is worth testing here is the translation, because that is where a
second provider can break the first one. The voice agent's tool loop is the
part that actually depends on it -- it echoes an assistant turn back verbatim
and pairs every tool_use with a tool_result -- so the round trip through
OpenRouter's OpenAI shape is tested end to end rather than one direction at a
time.
"""

import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.voice.models import VoiceAgentConfig
from apps.voice.serializers import VoiceAgentConfigSerializer

from . import services
from .models import AIAgentConfig
from .providers import anthropic, custom, openrouter, registry
from .providers.base import AIProviderError, ToolCall, tool_result_block
from .serializers import AIAgentConfigSerializer


class _Response:
    """Just enough of a `requests` response for the clients to read."""

    def __init__(self, payload, status_code=200, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


def _openrouter_reply(content="Yes, we deliver.", tool_calls=None, finish_reason="stop", usage=None):
    message = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": usage if usage is not None else {"prompt_tokens": 120, "completion_tokens": 30},
    }


def _capture(payload=None, status_code=200):
    """A fake `requests.post` that records what it was sent."""
    seen = {}

    def _post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers or {}
        seen["payload"] = json or {}
        seen["timeout"] = timeout
        return _Response(payload if payload is not None else _openrouter_reply(), status_code=status_code)

    return seen, _post


class OpenRouterRequestTests(TestCase):
    def test_it_authenticates_the_way_openrouter_does(self):
        seen, post = _capture()

        with patch.object(openrouter.requests, "post", post):
            openrouter.complete(
                api_key="sk-or-v1-abc", model="openai/gpt-4o-mini", system="",
                messages=[{"role": "user", "content": "hello"}],
            )

        self.assertEqual(seen["url"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer sk-or-v1-abc")
        # The Anthropic header would be ignored here, but sending a key in two
        # places is how a key ends up somewhere it was not meant to go.
        self.assertNotIn("x-api-key", seen["headers"])

    def test_the_system_prompt_becomes_a_leading_turn(self):
        seen, post = _capture()

        with patch.object(openrouter.requests, "post", post):
            openrouter.complete(
                api_key="k", model="openai/gpt-4o-mini", system="You sell bicycles.",
                messages=[{"role": "user", "content": "do you deliver"}],
            )

        self.assertEqual(seen["payload"]["messages"], [
            {"role": "system", "content": "You sell bicycles."},
            {"role": "user", "content": "do you deliver"},
        ])

    def test_temperature_and_effort_are_sent_in_this_vendors_words(self):
        seen, post = _capture()

        with patch.object(openrouter.requests, "post", post):
            openrouter.converse(
                api_key="k", model="anthropic/claude-sonnet-4.5", system="",
                messages=[{"role": "user", "content": "hi"}], temperature=0.3, effort="max",
            )

        self.assertEqual(seen["payload"]["temperature"], 0.3)
        # "max" is not a step OpenRouter has; asking for more thinking should
        # get the most it offers rather than the default.
        self.assertEqual(seen["payload"]["reasoning"], {"effort": "high"})

    def test_temperature_is_never_dropped(self):
        # The Anthropic client keeps a prefix list of models that reject
        # sampling. OpenRouter drops what the routed model cannot take, so
        # there is nothing here to keep current.
        self.assertTrue(openrouter.supports_sampling("anthropic/claude-sonnet-4.5"))
        self.assertTrue(openrouter.supports_sampling("openai/gpt-4o-mini"))

    def test_tools_are_translated_to_functions(self):
        seen, post = _capture()
        schema = {"type": "object", "properties": {"farewell": {"type": "string"}}}

        with patch.object(openrouter.requests, "post", post):
            openrouter.converse(
                api_key="k", model="openai/gpt-4o-mini", system="",
                messages=[{"role": "user", "content": "bye"}],
                tools=[{"name": "end_call", "description": "Hang up.", "input_schema": schema}],
            )

        self.assertEqual(seen["payload"]["tools"], [{
            "type": "function",
            "function": {"name": "end_call", "description": "Hang up.", "parameters": schema},
        }])

    def test_an_assistant_tool_use_turn_goes_back_as_tool_calls(self):
        seen, post = _capture()
        messages = [
            {"role": "user", "content": "what time do you open"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "One moment."},
                {"type": "tool_use", "id": "call_7", "name": "search_knowledge", "input": {"query": "hours"}},
            ]},
        ]

        with patch.object(openrouter.requests, "post", post):
            openrouter.converse(api_key="k", model="openai/gpt-4o-mini", system="", messages=messages)

        assistant = seen["payload"]["messages"][-1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["content"], "One moment.")
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_7")
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "search_knowledge")
        # Arguments cross the wire as a JSON string; the object form is a 400.
        self.assertEqual(json.loads(assistant["tool_calls"][0]["function"]["arguments"]), {"query": "hours"})

    def test_tool_results_become_their_own_turns_and_keep_the_pairing(self):
        seen, post = _capture()
        call = ToolCall(id="call_7", name="search_knowledge", input={"query": "hours"})
        messages = [
            {"role": "user", "content": "what time do you open"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call_7", "name": "search_knowledge", "input": {"query": "hours"}},
            ]},
            {"role": "user", "content": [tool_result_block(call, "We open at nine.")]},
        ]

        with patch.object(openrouter.requests, "post", post):
            openrouter.converse(api_key="k", model="openai/gpt-4o-mini", system="", messages=messages)

        answer = seen["payload"]["messages"][-1]
        self.assertEqual(answer, {"role": "tool", "tool_call_id": "call_7", "content": "We open at nine."})

    def test_a_tool_result_turn_that_also_has_text_answers_the_tool_first(self):
        # An unanswered tool_call is a 400 on the next request. A user turn
        # wedged between the assistant turn and its results is how that
        # happens by accident.
        translated = openrouter.to_openai_messages("", [
            {"role": "user", "content": [
                {"type": "text", "text": "and also, are you open Sunday?"},
                {"type": "tool_result", "tool_use_id": "call_1", "content": "nine to six"},
            ]},
        ])

        self.assertEqual([turn["role"] for turn in translated], ["tool", "user"])


class OpenRouterResponseTests(TestCase):
    def test_an_answer_is_read_back_with_its_token_counts(self):
        _, post = _capture(_openrouter_reply("Yes, we deliver.", usage={
            "prompt_tokens": 120, "completion_tokens": 30,
        }))

        with patch.object(openrouter.requests, "post", post):
            response = openrouter.complete(
                api_key="k", model="openai/gpt-4o-mini", system="",
                messages=[{"role": "user", "content": "do you deliver"}],
            )

        self.assertEqual(response.text, "Yes, we deliver.")
        self.assertEqual(response.input_tokens, 120)
        self.assertEqual(response.output_tokens, 30)
        # Usage is what the tenant is billed on and what the Usage tab shows,
        # so it has to survive this vendor calling it something else.
        self.assertEqual(response.stop_reason, "end_turn")

    def test_tool_calls_come_back_as_anthropic_blocks(self):
        _, post = _capture(_openrouter_reply(
            content="One moment.",
            tool_calls=[{
                "id": "call_9",
                "type": "function",
                "function": {"name": "end_call", "arguments": '{"farewell": "Bye."}'},
            }],
            finish_reason="tool_calls",
        ))

        with patch.object(openrouter.requests, "post", post):
            response = openrouter.converse(
                api_key="k", model="openai/gpt-4o-mini", system="",
                messages=[{"role": "user", "content": "bye"}],
            )

        self.assertEqual(response.stop_reason, "tool_use")
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].name, "end_call")
        self.assertEqual(response.tool_calls[0].input, {"farewell": "Bye."})
        # `content` is echoed back verbatim as the next assistant turn, so it
        # has to be in the shape this package speaks, not OpenAI's.
        self.assertEqual(response.content, [
            {"type": "text", "text": "One moment."},
            {"type": "tool_use", "id": "call_9", "name": "end_call", "input": {"farewell": "Bye."}},
        ])

    def test_a_tool_round_survives_the_trip_out_and_back(self):
        """The voice agent's loop, in miniature: answer, echo, result, ask again."""
        first = _openrouter_reply(
            content="",
            tool_calls=[{
                "id": "call_3", "type": "function",
                "function": {"name": "search_knowledge", "arguments": '{"query": "hours"}'},
            }],
            finish_reason="tool_calls",
        )
        seen, post = _capture(first)

        with patch.object(openrouter.requests, "post", post):
            response = openrouter.converse(
                api_key="k", model="openai/gpt-4o-mini", system="",
                messages=[{"role": "user", "content": "what time do you open"}],
            )

            # Exactly what `agent.run_turn` does with the answer.
            messages = [
                {"role": "user", "content": "what time do you open"},
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [tool_result_block(response.tool_calls[0], "Nine to six.")]},
            ]
            openrouter.converse(api_key="k", model="openai/gpt-4o-mini", system="", messages=messages)

        sent = seen["payload"]["messages"]
        self.assertEqual(sent[1]["tool_calls"][0]["id"], "call_3")
        self.assertEqual(sent[2], {"role": "tool", "tool_call_id": "call_3", "content": "Nine to six."})

    def test_arguments_that_are_not_json_do_not_take_the_turn_down(self):
        _, post = _capture(_openrouter_reply(
            content="",
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "end_call", "arguments": "{oops"}}],
            finish_reason="tool_calls",
        ))

        with patch.object(openrouter.requests, "post", post):
            response = openrouter.converse(
                api_key="k", model="openai/gpt-4o-mini", system="",
                messages=[{"role": "user", "content": "x"}],
            )

        # The tool refuses an empty call and the agent still says something,
        # which on a live call is the difference from silence.
        self.assertEqual(response.tool_calls[0].input, {})

    def test_the_vendors_own_message_survives_a_failure(self):
        _, post = _capture({"error": {"message": "No endpoints found for openai/nope."}}, status_code=404)

        with patch.object(openrouter.requests, "post", post):
            with self.assertRaises(AIProviderError) as caught:
                openrouter.complete(
                    api_key="k", model="openai/nope", system="",
                    messages=[{"role": "user", "content": "x"}],
                )

        self.assertIn("No endpoints found", str(caught.exception))

    def test_a_401_says_which_key_this_field_wanted(self):
        _, post = _capture({"error": {"message": "User not found."}}, status_code=401)

        with patch.object(openrouter.requests, "post", post):
            with self.assertRaises(AIProviderError) as caught:
                openrouter.complete(
                    api_key="sk-ant-wrong-vendor", model="openai/gpt-4o-mini", system="",
                    messages=[{"role": "user", "content": "x"}],
                )

        self.assertIn("sk-or-", str(caught.exception))

    def test_the_anthropic_client_says_the_same_thing_the_other_way_round(self):
        _, post = _capture({"error": {"message": "invalid x-api-key"}}, status_code=401)

        with patch.object(anthropic.requests, "post", post):
            with self.assertRaises(AIProviderError) as caught:
                anthropic.complete(
                    api_key="sk-or-v1-wrong-vendor", model="claude-sonnet-5", system="",
                    messages=[{"role": "user", "content": "x"}],
                )

        self.assertIn("sk-ant-", str(caught.exception))

    def test_a_failure_hidden_inside_a_200_is_still_a_failure(self):
        # OpenRouter answers 200 when *it* succeeded and the model it routed to
        # did not. Unhandled, that is an empty reply and an agent that says
        # nothing at all.
        _, post = _capture({"error": {"message": "Upstream is out of credit."}})

        with patch.object(openrouter.requests, "post", post):
            with self.assertRaises(AIProviderError) as caught:
                openrouter.complete(
                    api_key="k", model="openai/gpt-4o-mini", system="",
                    messages=[{"role": "user", "content": "x"}],
                )

        self.assertIn("out of credit", str(caught.exception))

    def test_no_key_is_refused_before_any_request(self):
        with self.assertRaises(AIProviderError):
            openrouter.complete(api_key="", model="openai/gpt-4o-mini", system="", messages=[])


class CustomProviderTests(TestCase):
    """The bring-your-own-endpoint provider: same OpenAI shape, a configurable URL."""

    def test_the_endpoint_is_the_base_url_plus_chat_completions(self):
        seen, post = _capture()

        with patch.object(custom.requests, "post", post):
            custom.complete(
                api_key="k", model="llama3.3", system="", base_url="https://api.example.com/v1",
                messages=[{"role": "user", "content": "hello"}],
            )

        self.assertEqual(seen["url"], "https://api.example.com/v1/chat/completions")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer k")

    def test_a_trailing_slash_on_the_base_url_does_not_double_up(self):
        seen, post = _capture()

        with patch.object(custom.requests, "post", post):
            custom.complete(
                api_key="k", model="llama3.3", system="", base_url="https://api.example.com/v1/",
                messages=[{"role": "user", "content": "hello"}],
            )

        self.assertEqual(seen["url"], "https://api.example.com/v1/chat/completions")

    def test_no_base_url_is_refused_before_any_request(self):
        with self.assertRaises(AIProviderError) as caught:
            custom.complete(api_key="k", model="llama3.3", system="", base_url="", messages=[])

        self.assertIn("base URL", str(caught.exception))

    def test_no_key_is_refused_before_any_request(self):
        with self.assertRaises(AIProviderError):
            custom.complete(api_key="", model="llama3.3", system="", base_url="https://api.example.com/v1", messages=[])

    def test_tools_and_replies_translate_the_same_way_as_openrouter(self):
        """The shared translator, exercised once through this client rather than re-proven."""
        _, post = _capture(_openrouter_reply("We're open till nine."))

        with patch.object(custom.requests, "post", post):
            response = custom.complete(
                api_key="k", model="llama3.3", system="", base_url="https://api.example.com/v1",
                messages=[{"role": "user", "content": "when do you close"}],
            )

        self.assertEqual(response.text, "We're open till nine.")

    def test_supports_sampling_is_always_true(self):
        # There is no fixed vendor here to keep a prefix list for.
        self.assertTrue(custom.supports_sampling("anything"))


class RegistryTests(TestCase):
    def test_each_name_reaches_its_own_client(self):
        self.assertIs(registry.get_client("ANTHROPIC"), anthropic)
        self.assertIs(registry.get_client("OPENROUTER"), openrouter)
        self.assertIs(registry.get_client("CUSTOM"), custom)

    def test_nothing_configured_still_means_anthropic(self):
        self.assertIs(registry.get_client(""), anthropic)
        self.assertIs(registry.get_client(None), anthropic)

    def test_an_unknown_provider_is_refused_rather_than_guessed(self):
        # Falling back would send a tenant's key to a vendor they did not pick.
        with self.assertRaises(AIProviderError):
            registry.get_client("MISTRAL")

    def test_converse_dispatches_on_the_provider(self):
        _, post = _capture()

        with patch.object(openrouter.requests, "post", post):
            response = registry.converse(
                provider="OPENROUTER", api_key="k", model="openai/gpt-4o-mini", system="",
                messages=[{"role": "user", "content": "x"}],
            )

        self.assertEqual(response.text, "Yes, we deliver.")

    def test_converse_dispatches_to_a_custom_endpoint(self):
        seen, post = _capture()

        with patch.object(custom.requests, "post", post):
            response = registry.converse(
                provider="CUSTOM", api_key="k", model="llama3.3", system="",
                base_url="https://api.example.com/v1", messages=[{"role": "user", "content": "x"}],
            )

        self.assertEqual(seen["url"], "https://api.example.com/v1/chat/completions")
        self.assertEqual(response.text, "Yes, we deliver.")

    def test_a_model_name_from_the_wrong_vendor_is_named_as_the_problem(self):
        self.assertIn("vendor/model", registry.model_name_problem("OPENROUTER", "claude-sonnet-5"))
        self.assertIn("no vendor prefix", registry.model_name_problem("ANTHROPIC", "anthropic/claude-sonnet-4.5"))
        self.assertEqual(registry.model_name_problem("OPENROUTER", "anthropic/claude-sonnet-4.5"), "")
        self.assertEqual(registry.model_name_problem("ANTHROPIC", "claude-sonnet-5"), "")
        # An empty box is somebody who has not finished, not somebody wrong.
        self.assertEqual(registry.model_name_problem("OPENROUTER", ""), "")
        # A custom endpoint has no vocabulary of its own to check a name against.
        self.assertEqual(registry.model_name_problem("CUSTOM", "anything-at-all"), "")

    def test_a_custom_provider_without_a_base_url_is_named_as_the_problem(self):
        self.assertIn("base URL", registry.base_url_problem("CUSTOM", ""))
        self.assertIn("base URL", registry.base_url_problem("CUSTOM", "   "))
        self.assertEqual(registry.base_url_problem("CUSTOM", "https://api.example.com/v1"), "")
        # The other two providers have a fixed endpoint; nothing to check.
        self.assertEqual(registry.base_url_problem("ANTHROPIC", ""), "")
        self.assertEqual(registry.base_url_problem("OPENROUTER", ""), "")


class AgentRoutingTests(TestCase):
    """The tenant's choice is what decides which vendor is billed."""

    @override_settings(AI_API_KEY="deployment-key")
    def test_the_whatsapp_agent_asks_the_provider_its_tenant_chose(self):
        config = AIAgentConfig(provider="OPENROUTER", model="openai/gpt-4o-mini")
        seen, post = _capture()

        with patch.object(openrouter.requests, "post", post):
            response = services._ask(config, "You sell bicycles.", [{"role": "user", "content": "hi"}])

        self.assertEqual(seen["payload"]["model"], "openai/gpt-4o-mini")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer deployment-key")
        self.assertEqual(response.text, "Yes, we deliver.")

    @override_settings(AI_API_KEY="deployment-key")
    def test_the_default_tenant_still_goes_to_anthropic(self):
        config = AIAgentConfig()
        seen, post = _capture({"content": [{"type": "text", "text": "Hello."}], "usage": {}})

        with patch.object(anthropic.requests, "post", post):
            services._ask(config, "", [{"role": "user", "content": "hi"}])

        self.assertEqual(seen["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(seen["headers"]["x-api-key"], "deployment-key")


class ProviderSettingsTests(TestCase):
    """Saving a model name the chosen provider cannot use is refused on the form."""

    def test_the_ai_agent_refuses_a_bare_model_name_on_openrouter(self):
        serializer = AIAgentConfigSerializer(
            AIAgentConfig(provider="ANTHROPIC", model="claude-sonnet-5"),
            data={"provider": "OPENROUTER"}, partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("vendor/model", str(serializer.errors["model"]))

    def test_the_ai_agent_accepts_a_matching_pair(self):
        serializer = AIAgentConfigSerializer(
            AIAgentConfig(provider="ANTHROPIC", model="claude-sonnet-5"),
            data={"provider": "OPENROUTER", "model": "anthropic/claude-sonnet-4.5"}, partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_the_calling_agent_is_checked_even_while_it_is_switched_off(self):
        # Finding out mid-call is finding out too late, and the agent can be
        # configured long before it is switched on.
        serializer = VoiceAgentConfigSerializer(
            VoiceAgentConfig(is_enabled=False, model_provider="ANTHROPIC", model="claude-opus-5"),
            data={"model_provider": "OPENROUTER"}, partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("vendor/model", str(serializer.errors["model"]))

    def test_the_ai_agent_refuses_a_custom_provider_with_no_base_url(self):
        serializer = AIAgentConfigSerializer(
            AIAgentConfig(provider="ANTHROPIC", model="claude-sonnet-5"),
            data={"provider": "CUSTOM", "model": "llama3.3"}, partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("base URL", str(serializer.errors["base_url"]))

    def test_the_ai_agent_accepts_a_custom_provider_with_a_base_url(self):
        serializer = AIAgentConfigSerializer(
            AIAgentConfig(provider="ANTHROPIC", model="claude-sonnet-5"),
            data={"provider": "CUSTOM", "model": "llama3.3", "base_url": "https://api.example.com/v1"},
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_the_calling_agent_refuses_a_custom_provider_with_no_base_url(self):
        serializer = VoiceAgentConfigSerializer(
            VoiceAgentConfig(is_enabled=False, model_provider="ANTHROPIC", model="claude-opus-5"),
            data={"model_provider": "CUSTOM", "model": "llama3.3"}, partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("base URL", str(serializer.errors["base_url"]))
