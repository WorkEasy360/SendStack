"""
Tests for the live-verification command itself.

There is an obvious circularity to be avoided here: `verify_openai` exists to
prove things a mocked test cannot, so mocking it out and asserting it passes
would prove nothing about OpenAI. That is not what these are for.

What they check is the **harness** -- the part that has to be right before the
real run is worth trusting, and the part that is dangerous if it is wrong:

* that the end-to-end check really drives the inbound ladder and really
  produces an outbound message, rather than reporting a pass from an empty
  code path;
* that it writes nothing that survives, so running it against a production
  database does not leave a scratch tenant behind;
* and above all that **no WhatsApp message can escape**, including via the
  Celery worker running in another process, which does not share this
  transaction and would happily send to a real number.

That last one is why the command is tested at all. Everything else about it
fails loudly in front of the person who ran it; that one fails silently, on
somebody's phone.
"""

from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from io import StringIO

from apps.messaging.models import Message
from apps.tenants.models import Tenant

from .providers.base import AIResponse


def _answer(text):
    return AIResponse(text=text, input_tokens=100, output_tokens=20, latency_ms=150)


class _Response:
    """A stand-in for one `requests` response."""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


@override_settings(
    OPENAI_API_KEY="sk-test-not-a-real-key", OPENAI_MODEL="gpt-4o-mini",
    OPENAI_BASE_URL="https://api.openai.com/v1", AI_EMBEDDINGS_ENABLED=False, AI_API_KEY="",
)
class VerifyOpenAICommandTests(TestCase):
    """The command's plumbing, with every network call intercepted."""

    def setUp(self):
        # The two HTTP surfaces the command uses: `requests.get` for the
        # /models probes it makes directly, and the provider module's own
        # `requests.post` for everything routed through our client.
        listing = patch("apps.ai.management.commands.verify_openai.requests.get")
        self.mock_get = listing.start()
        self.addCleanup(listing.stop)
        self.mock_get.return_value = _Response(payload={"data": [{"id": "gpt-4o-mini"}]})

        completion = patch("apps.ai.providers.openai.requests.post")
        self.mock_post = completion.start()
        self.addCleanup(completion.stop)
        self.mock_post.return_value = _Response(payload={
            "choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        })

    def _run(self, *args, **options):
        out = StringIO()
        call_command("verify_openai", *args, stdout=out, stderr=out, **options)
        return out.getvalue()

    # ------------------------------------------------------------------

    def test_it_reports_the_configuration_without_printing_the_key(self):
        output = self._run("--skip-errors")

        self.assertIn("gpt-4o-mini", output)
        self.assertIn("https://api.openai.com/v1", output)
        # The whole point: the key is identifiable but not usable.
        self.assertNotIn("sk-test-not-a-real-key", output)
        self.assertIn("sk-tes", output)
        self.assertIn("length 22", output)

    def test_it_stops_before_any_network_call_when_no_key_is_set(self):
        with override_settings(OPENAI_API_KEY="", AI_API_KEY=""):
            output = self._run()

        self.assertIn("No API key is configured", output)
        self.mock_get.assert_not_called()
        self.mock_post.assert_not_called()

    def test_the_end_to_end_check_drives_the_real_ladder(self):
        """A pass here must mean an outbound message was genuinely produced."""
        output = self._run("--skip-errors")

        self.assertIn("End-to-end reply", output)
        self.assertIn("PASS  End-to-end reply", output)
        # The reply text came from the (stubbed) model and reached a queued
        # message, which is the claim the check makes.
        self.assertIn("webhook -> reply", output)

    def test_nothing_it_writes_survives_the_run(self):
        self._run("--skip-errors")

        self.assertFalse(Tenant.objects.filter(slug__startswith="verify-openai").exists())
        self.assertFalse(Message.objects.exists())

    def test_no_whatsapp_message_can_leave_even_via_celery(self):
        """
        The check that matters. `send_message_task.delay` must never be
        reached: a queued send is executed by a worker in another process,
        which neither the transaction rollback nor the in-process transport
        patch can reach.
        """
        from apps.messaging import tasks as messaging_tasks

        with patch.object(messaging_tasks.send_message_task, "delay") as delay:
            output = self._run("--skip-errors")

        delay.assert_not_called()
        self.assertIn("PASS  Nothing was actually sent", output)

    def test_the_rag_check_fails_when_the_answer_is_not_grounded(self):
        """
        The planted fact is nineteen days. A model that says fourteen -- the
        figure it would produce from its own weights -- must be reported as a
        failure, or the check is decorative.
        """
        self.mock_post.return_value = _Response(payload={
            "choices": [{"message": {"content": "You have fourteen days to return an item."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8},
        })

        output = self._run("--skip-errors")

        self.assertIn("FAIL  RAG answering", output)
        self.assertIn("may not be grounded", output)

    def test_the_rag_check_passes_on_the_planted_fact(self):
        self.mock_post.return_value = _Response(payload={
            "choices": [{"message": {"content": "You have nineteen days from delivery to return it."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 9},
        })

        output = self._run("--skip-errors")

        self.assertIn("PASS  RAG answering", output)

    def test_the_memory_check_fails_when_earlier_turns_are_not_recalled(self):
        output = self._run("--skip-errors")

        # The default stub answers "ready", which recalls neither the name nor
        # the item, so the check must say so rather than pass on a reply.
        self.assertIn("FAIL  Conversation memory", output)
        self.assertIn("did not recall", output)

    def test_the_memory_check_passes_when_both_facts_come_back(self):
        def _per_call(*args, **kwargs):
            messages = (kwargs.get("json") or {}).get("messages") or []
            if any("my name is" in str(turn.get("content", "")).lower() for turn in messages):
                return _Response(payload={
                    "choices": [{"message": {"content": "Your name is Farhan and you wanted a blue kettle."},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                })
            return _Response(payload={
                "choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            })

        self.mock_post.side_effect = _per_call
        output = self._run("--skip-errors")

        self.assertIn("PASS  Conversation memory", output)
        self.assertIn("recalled both", output)

    def test_a_model_the_account_cannot_call_is_reported(self):
        self.mock_get.return_value = _Response(
            status_code=404, payload={"error": {"message": "The model does not exist"}},
        )

        output = self._run("--skip-errors")

        self.assertIn("FAIL  Model reachable", output)
        self.assertIn("not available to this key", output)

    def test_the_model_override_does_not_touch_stored_configuration(self):
        from .models import AIAgentConfig

        output = self._run("--skip-errors", model="gpt-4.1-mini")

        self.assertIn("gpt-4.1-mini", output)
        self.assertFalse(AIAgentConfig.objects.exists())


@override_settings(
    OPENAI_API_KEY="sk-test-not-a-real-key", OPENAI_MODEL="gpt-4o-mini", AI_EMBEDDINGS_ENABLED=False, AI_API_KEY="",
)
class VerifyOpenAIErrorChecksTests(TestCase):
    """The failure checks, which are the reason the command is worth running twice."""

    def setUp(self):
        listing = patch("apps.ai.management.commands.verify_openai.requests.get")
        self.mock_get = listing.start()
        self.addCleanup(listing.stop)
        self.mock_get.return_value = _Response(payload={"data": [{"id": "gpt-4o-mini"}]})

    def _run(self):
        out = StringIO()
        call_command("verify_openai", stdout=out, stderr=out)
        return out.getvalue()

    def test_each_failure_mode_is_reported_and_never_leaks_the_key(self):
        import requests as requests_module

        def _by_request(url, **kwargs):
            payload = kwargs.get("json") or {}
            headers = kwargs.get("headers") or {}
            if kwargs.get("timeout", 30) < 0.01:
                raise requests_module.Timeout("timed out")
            if "invalid" in url:
                raise requests_module.ConnectionError("name resolution failed")
            if "notreal" in headers.get("Authorization", ""):
                return _Response(401, {"error": {"message": "Incorrect API key provided"}})
            if payload.get("model") == "gpt-0-does-not-exist":
                return _Response(404, {"error": {"message": "The model does not exist"}})
            return _Response(payload={
                "choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            })

        with patch("apps.ai.providers.openai.requests.post", side_effect=_by_request):
            output = self._run()

        self.assertIn("PASS  Invalid API key rejected", output)
        self.assertIn("PASS  Unavailable model rejected", output)
        self.assertIn("PASS  Timeout surfaces as a provider error", output)
        self.assertIn("PASS  Unreachable endpoint surfaces as a provider error", output)
        self.assertIn("PASS  Rate limit handled", output)
        # Four error messages were printed; none of them may carry the key.
        self.assertNotIn("sk-test-not-a-real-key", output)
