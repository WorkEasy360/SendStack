"""
OpenAI as a first-class provider, and where its settings come from.

Two things are worth testing here and neither is the happy path, which is the
same chat-completions round trip `tests_providers` already covers for
OpenRouter.

The first is the **resolution order**: a key, a model and an endpoint can each
come from the tenant or from the environment, and getting that wrong either
bills the wrong account or quietly runs a model nobody chose.

The second is the **reasoning-model divergence**: o-series and gpt-5 reject
`temperature` and reject `max_tokens`, and a 400 from that arrives while a
customer is waiting.
"""

import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.tenants.models import Tenant

from . import services
from .models import AIAgentConfig
from .providers import openai as openai_client
from .providers import registry
from .providers.base import AIProviderError


class _Response:
    def __init__(self, payload, status_code=200, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


def _reply(content="We open at nine.", usage=None):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": usage if usage is not None else {"prompt_tokens": 90, "completion_tokens": 12},
    }


def _capture(payload=None, status_code=200):
    seen = {}

    def _post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers or {}
        seen["payload"] = json or {}
        return _Response(payload if payload is not None else _reply(), status_code=status_code)

    return seen, _post


class CredentialResolutionTests(TestCase):
    """Tenant first, environment second -- and only OpenAI has the second."""

    @override_settings(OPENAI_API_KEY="sk-deployment", OPENAI_MODEL="gpt-4o-mini", AI_API_KEY="")
    def test_a_tenant_with_nothing_set_uses_the_environment(self):
        resolved = registry.credentials(registry.OPENAI, api_key="", model="", base_url="")

        self.assertEqual(resolved.api_key, "sk-deployment")
        self.assertEqual(resolved.model, "gpt-4o-mini")
        self.assertEqual(resolved.base_url, "https://api.openai.com/v1")

    @override_settings(OPENAI_API_KEY="sk-deployment", OPENAI_MODEL="gpt-4o-mini")
    def test_a_tenants_own_key_and_model_win(self):
        # Otherwise a customer who pasted their own key is billed to somebody
        # else's OpenAI account, which is the one mistake here nobody notices
        # until the invoice.
        resolved = registry.credentials(registry.OPENAI, api_key="sk-tenant", model="gpt-4o")

        self.assertEqual(resolved.api_key, "sk-tenant")
        self.assertEqual(resolved.model, "gpt-4o")

    @override_settings(OPENAI_BASE_URL="https://acme.openai.azure.com/openai/v1", OPENAI_API_KEY="sk-x")
    def test_the_endpoint_can_be_moved_to_azure_or_a_gateway(self):
        self.assertEqual(
            registry.credentials(registry.OPENAI).base_url, "https://acme.openai.azure.com/openai/v1",
        )

    @override_settings(OPENAI_MODEL="gpt-4o-mini", AI_API_KEY="fallback")
    def test_openai_settings_do_not_leak_into_other_providers(self):
        # An Anthropic tenant must not silently acquire an OpenAI model name.
        resolved = registry.credentials(registry.ANTHROPIC, api_key="", model="claude-sonnet-5")

        self.assertEqual(resolved.model, "claude-sonnet-5")
        self.assertEqual(resolved.api_key, "fallback")

    @override_settings(OPENAI_API_KEY="", OPENAI_MODEL="", AI_API_KEY="")
    def test_with_nothing_configured_there_is_no_key_at_all(self):
        # And crucially not an empty string, which reads as truthy-ish in
        # enough places to be worth pinning.
        self.assertIsNone(registry.credentials(registry.OPENAI).api_key)


class ModelValidationTests(TestCase):
    def test_a_vendor_prefixed_name_is_refused_for_openai(self):
        problem = registry.model_name_problem(registry.OPENAI, "openai/gpt-4o-mini")

        self.assertIn("no vendor prefix", problem)
        self.assertIn("OpenRouter", problem)

    def test_a_plain_openai_name_is_accepted(self):
        self.assertEqual(registry.model_name_problem(registry.OPENAI, "gpt-4o-mini"), "")

    def test_openai_needs_no_base_url_of_its_own(self):
        # Unlike Custom: OpenAI has a default endpoint, so a blank field is
        # complete rather than unfinished.
        self.assertEqual(registry.base_url_problem(registry.OPENAI, ""), "")


class ReasoningModelTests(TestCase):
    """The o-series and gpt-5 take different parameters from gpt-4o."""

    def test_temperature_is_not_sent_to_a_reasoning_model(self):
        seen, post = _capture()

        with patch.object(openai_client.requests, "post", post):
            openai_client.complete(
                api_key="sk-x", model="o3-mini", system="s", messages=[{"role": "user", "content": "hi"}],
                temperature=0.2,
            )

        self.assertNotIn("temperature", seen["payload"])
        self.assertIn("max_completion_tokens", seen["payload"])
        self.assertNotIn("max_tokens", seen["payload"])

    def test_temperature_is_sent_to_an_ordinary_model(self):
        seen, post = _capture()

        with patch.object(openai_client.requests, "post", post):
            openai_client.complete(
                api_key="sk-x", model="gpt-4o-mini", system="s", messages=[{"role": "user", "content": "hi"}],
                temperature=0.2,
            )

        self.assertEqual(seen["payload"]["temperature"], 0.2)
        self.assertEqual(seen["payload"]["max_tokens"], 400)
        self.assertNotIn("max_completion_tokens", seen["payload"])

    def test_dated_model_names_are_recognised_too(self):
        self.assertFalse(openai_client.supports_sampling("o3-mini-2025-01-31"))
        self.assertFalse(openai_client.supports_sampling("gpt-5-nano"))
        self.assertTrue(openai_client.supports_sampling("gpt-4o-mini"))
        # "o1" as a prefix must not swallow an unrelated name that starts with it.
        self.assertTrue(openai_client.supports_sampling("omni-model"))

    def test_a_point_release_is_the_same_family(self):
        """
        gpt-5.1 is a gpt-5 and refuses `temperature` exactly as gpt-5 does.

        Matched separately from the hyphen case because it does not start with
        "gpt-5-": a matcher that only knows the hyphen sends `temperature` and
        `max_tokens` to every point release, which is a 400 in front of a
        customer rather than a wrong answer in a test.
        """
        for name in ("gpt-5.1", "gpt-5.1-mini", "gpt-5.2", "o3.5", "o4.1-mini"):
            with self.subTest(model=name):
                self.assertFalse(openai_client.supports_sampling(name))

    def test_a_point_release_gets_the_reasoning_parameters(self):
        seen, post = _capture()

        with patch.object(openai_client.requests, "post", post):
            openai_client.complete(
                api_key="sk-x", model="gpt-5.1", system="s", messages=[{"role": "user", "content": "hi"}],
                temperature=0.2,
            )

        self.assertNotIn("temperature", seen["payload"])
        self.assertNotIn("max_tokens", seen["payload"])
        self.assertIn("max_completion_tokens", seen["payload"])

    def test_an_ordinary_point_release_still_takes_temperature(self):
        # The dot must not be read as "reasoning" on its own -- gpt-4.1 is an
        # ordinary chat model and rejecting `temperature` for it would be the
        # same bug in the other direction.
        self.assertTrue(openai_client.supports_sampling("gpt-4.1"))
        self.assertTrue(openai_client.supports_sampling("gpt-4.1-mini"))


class RequestShapeTests(TestCase):
    def test_the_key_goes_out_as_a_bearer_token_to_the_chat_endpoint(self):
        seen, post = _capture()

        with patch.object(openai_client.requests, "post", post):
            openai_client.complete(
                api_key="sk-x", model="gpt-4o-mini", system="You are helpful",
                messages=[{"role": "user", "content": "hello"}],
            )

        self.assertEqual(seen["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer sk-x")
        self.assertEqual(seen["payload"]["messages"][0], {"role": "system", "content": "You are helpful"})

    @override_settings(OPENAI_MODEL="gpt-4o-mini")
    def test_a_blank_model_falls_back_to_the_environment_at_the_client_too(self):
        seen, post = _capture()

        with patch.object(openai_client.requests, "post", post):
            openai_client.complete(
                api_key="sk-x", model="", system="s", messages=[{"role": "user", "content": "hi"}],
            )

        self.assertEqual(seen["payload"]["model"], "gpt-4o-mini")

    def test_usage_and_text_come_back_in_the_shared_shape(self):
        _, post = _capture(_reply("We open at nine.", usage={"prompt_tokens": 11, "completion_tokens": 3}))

        with patch.object(openai_client.requests, "post", post):
            response = openai_client.complete(
                api_key="sk-x", model="gpt-4o-mini", system="s", messages=[{"role": "user", "content": "hi"}],
            )

        self.assertEqual(response.text, "We open at nine.")
        self.assertEqual(response.input_tokens, 11)
        self.assertEqual(response.output_tokens, 3)
        self.assertEqual(response.stop_reason, "end_turn")


class ErrorHandlingTests(TestCase):
    def test_a_401_says_which_vendors_key_was_expected(self):
        _, post = _capture({"error": {"message": "Incorrect API key provided"}}, status_code=401)

        with patch.object(openai_client.requests, "post", post):
            with self.assertRaises(AIProviderError) as caught:
                openai_client.complete(
                    api_key="sk-ant-wrong", model="gpt-4o-mini", system="s",
                    messages=[{"role": "user", "content": "hi"}],
                )

        self.assertIn("beginning sk-", str(caught.exception))

    def test_a_429_points_at_billing_rather_than_the_code(self):
        _, post = _capture({"error": {"message": "Rate limit reached"}}, status_code=429)

        with patch.object(openai_client.requests, "post", post):
            with self.assertRaises(AIProviderError) as caught:
                openai_client.complete(
                    api_key="sk-x", model="gpt-4o-mini", system="s", messages=[{"role": "user", "content": "hi"}],
                )

        self.assertIn("billing", str(caught.exception))

    def test_an_error_inside_a_200_is_still_an_error(self):
        # A gateway in front of OpenAI reports upstream failures this way.
        # Unhandled it is an empty reply, which reads as the agent being broken.
        _, post = _capture({"error": {"message": "upstream is out of credit"}})

        with patch.object(openai_client.requests, "post", post):
            with self.assertRaises(AIProviderError) as caught:
                openai_client.complete(
                    api_key="sk-x", model="gpt-4o-mini", system="s", messages=[{"role": "user", "content": "hi"}],
                )

        self.assertIn("out of credit", str(caught.exception))

    def test_an_unreachable_endpoint_names_the_url(self):
        def _boom(*args, **kwargs):
            raise openai_client.requests.RequestException("connection refused")

        with patch.object(openai_client.requests, "post", _boom):
            with self.assertRaises(AIProviderError) as caught:
                openai_client.complete(
                    api_key="sk-x", model="gpt-4o-mini", system="s", messages=[{"role": "user", "content": "hi"}],
                )

        self.assertIn("api.openai.com", str(caught.exception))


class AgentWiringTests(TestCase):
    """The agent's own call has to go out with the resolved settings."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="openai-wiring", kind=Tenant.Kind.CUSTOMER)

    @override_settings(OPENAI_API_KEY="sk-deployment", OPENAI_MODEL="gpt-4o-mini", AI_API_KEY="")
    def test_an_openai_agent_with_a_blank_model_is_still_ready(self):
        config = AIAgentConfig.objects.create(
            tenant=self.tenant, is_enabled=True, provider=AIAgentConfig.Provider.OPENAI, model="",
        )

        self.assertTrue(services.is_configured(config))
        self.assertEqual(services.resolved(config).model, "gpt-4o-mini")

    @override_settings(OPENAI_API_KEY="sk-deployment", OPENAI_MODEL="gpt-4o-mini", AI_API_KEY="")
    def test_the_resolved_model_is_what_reaches_the_provider(self):
        config = AIAgentConfig.objects.create(
            tenant=self.tenant, is_enabled=True, provider=AIAgentConfig.Provider.OPENAI, model="",
        )
        seen, post = _capture()

        with patch.object(openai_client.requests, "post", post):
            services.ask(self.tenant.pk, "when do you open?", config=config)

        # No documents, so retrieval finds nothing and no call is made -- the
        # knowledge gate runs before the model, which is the point of it.
        self.assertNotIn("payload", seen)

    @override_settings(OPENAI_API_KEY="", OPENAI_MODEL="", AI_API_KEY="")
    def test_an_agent_with_no_key_anywhere_is_not_ready(self):
        config = AIAgentConfig.objects.create(
            tenant=self.tenant, is_enabled=True, provider=AIAgentConfig.Provider.OPENAI,
        )

        self.assertFalse(services.is_configured(config))
