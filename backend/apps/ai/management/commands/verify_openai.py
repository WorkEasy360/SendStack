"""
Prove the OpenAI integration works against the real API, not against a mock.

Every other test of this feature stubs the provider, which is correct -- a test
suite that spent money and needed the internet would stop being run. But a
stubbed provider cannot tell you that *your* key is live, that *your* model
name is one this account may call, or that the reasoning-model parameter
divergence was handled the way the vendor actually wants it this month. Those
are exactly the things that fail in production and cannot fail in CI.

So this is the other half: one command, run by hand, that spends a few hundred
tokens to answer the questions a mock cannot.

    python manage.py verify_openai

What it does **not** do is send a WhatsApp message. The end-to-end check runs
the genuine inbound ladder and the genuine send helper, with only the
transport itself intercepted -- so the reply is composed, billed, rendered and
queued for real, and then stopped one layer above the network. Nothing reaches
a phone. Everything it writes is rolled back unless ``--keep`` is passed.

The key is read from the environment (OPENAI_API_KEY) via the same resolver
the live send path uses, and is never printed, logged or stored -- only ever
its length and its first few characters, which is enough to tell "the wrong
key" from "no key" and not enough to use.
"""

import time
from contextlib import ExitStack
from unittest.mock import patch

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.ai import knowledge, services
from apps.ai.models import AIAgentConfig, KnowledgeBase, KnowledgeDocument
from apps.ai.providers import openai as openai_client
from apps.ai.providers.registry import OPENAI, AIProviderError, credentials

#: Kept small on purpose. This command is run by hand, possibly often, against
#: a real billed account; every check below asks for the fewest tokens that
#: can still demonstrate the thing it is checking.
PROBE_TOKENS = 30
ANSWER_TOKENS = 120

#: What the RAG check plants and then asks about. Deliberately unguessable --
#: a model that answers this correctly can only have read the document, which
#: is the entire claim being tested. A plausible-sounding fact would prove
#: nothing, because the model could produce it from its own weights.
RAG_DOCUMENT = """Nasreen Traders order handling

Every order placed with us is given a tracking code beginning QX7. Orders are
picked the same working day if they arrive before two in the afternoon.

Our returns window is nineteen days from delivery, not the fourteen days most
of our suppliers offer. Returned items must still carry the orange seal.

The warehouse in Bhiwandi is closed on the second Tuesday of every month."""

RAG_QUESTION = "how many days do I have to return something?"
RAG_EXPECTED = ("nineteen", "19")

#: The memory check. The second question is unanswerable without the first
#: turn, which is what makes it a test of history rather than of the model.
MEMORY_NAME = "Farhan"
MEMORY_ITEM = "blue kettle"


class Result:
    """One check, its verdict, and what it saw."""

    __slots__ = ("name", "ok", "detail", "skipped")

    def __init__(self, name, ok, detail="", skipped=False):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.skipped = skipped


def mask(key: str) -> str:
    """
    Enough of a key to identify it, never enough to use it.

    Shows the vendor prefix because that is the actual mistake being diagnosed
    -- an sk-ant- or sk-or- key pasted into the OpenAI field -- and the length,
    because a truncated paste is the other one.
    """
    key = (key or "").strip()
    if not key:
        return "(none)"
    return f"{key[:6]}...{'*' * 6} (length {len(key)})"


class Command(BaseCommand):
    help = "Verify the OpenAI integration against the real API, without sending any WhatsApp message."

    def add_arguments(self, parser):
        parser.add_argument(
            "--model", default="",
            help="Model to verify instead of OPENAI_MODEL. Does not change any stored configuration.",
        )
        parser.add_argument(
            "--keep", action="store_true",
            help="Keep the scratch tenant and its rows instead of rolling them back.",
        )
        parser.add_argument(
            "--skip-errors", action="store_true",
            help="Skip the failure checks, which deliberately make requests that are refused.",
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        self.results = []
        self.run_id = str(int(time.time()))
        override = (options["model"] or "").strip()

        resolved = credentials(OPENAI, model=override)
        key, model, base_url = resolved.api_key, resolved.model, resolved.base_url

        self._heading("Configuration")
        self.stdout.write(f"  API key      : {mask(key)}")
        self.stdout.write(f"  Model        : {model or '(none)'}")
        self.stdout.write(f"  Base URL     : {base_url}")
        self.stdout.write(f"  Key source   : {'environment (OPENAI_API_KEY)' if key else 'not configured'}")
        self.stdout.write(f"  Sampling     : {'temperature is sent' if openai_client.supports_sampling(model) else 'reasoning model -- temperature omitted, max_completion_tokens sent'}")
        self.stdout.write(f"  Timeout      : {getattr(settings, 'AI_REQUEST_TIMEOUT_SECONDS', 30)}s")

        if not key:
            # Which file depends on how this is being run, and naming the
            # wrong one is the whole of the confusion: the container gets its
            # environment from the compose `env_file` at the repository root,
            # while a local `manage.py` reads `backend/.env` through dotenv.
            self.stderr.write(self.style.ERROR(
                "\nNo API key is configured, so nothing below could be verified.\n"
                "  Set OPENAI_API_KEY (and OPENAI_MODEL) in:\n"
                "    .env          -- for the Docker stack, then: docker compose up -d backend celery-worker celery-beat\n"
                "    backend/.env  -- for running manage.py directly\n"
                "  Then run this command again."
            ))
            return

        self._heading("1. Is the key live?")
        self._check_key(key, base_url)

        self._heading("2. Is the model reachable on this account?")
        self._check_model(key, model, base_url)

        self._heading("3. A real completion through the provider module")
        self._check_completion(key, model, base_url)

        self._heading("4. WhatsApp -> AI -> WhatsApp, with the real model")
        self._check_end_to_end(model, keep=options["keep"])

        self._heading("5. Conversation memory")
        self._check_memory(key, model, base_url)

        self._heading("6. Retrieval-augmented answering")
        self._check_rag(model, keep=options["keep"])

        if options["skip_errors"]:
            self._record("Error handling", True, "skipped at request", skipped=True)
        else:
            self._heading("7. Failure handling")
            self._check_errors(key, model, base_url)

        self._summary()

    # ------------------------------------------------------------------
    # Live API checks
    # ------------------------------------------------------------------

    def _check_key(self, key, base_url):
        """
        A real authenticated request that costs nothing.

        `/models` is used rather than a completion because a key can be live
        and still be refused by a model, and telling those two apart is the
        entire value of checking them separately: "your key is wrong" and
        "your key is fine but you cannot call gpt-5" need different fixes.
        """
        url = f"{base_url.rstrip('/')}/models"
        try:
            response = requests.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=30)
        except requests.RequestException as exc:
            self._record("API key", False, f"could not reach {url}: {exc}")
            return

        if response.status_code == 200:
            try:
                count = len(response.json().get("data") or [])
            except ValueError:
                count = 0
            self._record("API key", True, f"authenticated; {count} models visible to this account")
            return

        self._record("API key", False, f"HTTP {response.status_code}: {self._error_of(response)}")

    def _check_model(self, key, model, base_url):
        url = f"{base_url.rstrip('/')}/models/{model}"
        try:
            response = requests.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=30)
        except requests.RequestException as exc:
            self._record("Model reachable", False, f"could not reach {url}: {exc}")
            return

        if response.status_code == 200:
            self._record("Model reachable", True, f"{model} is available to this account")
            return
        if response.status_code == 404:
            self._record(
                "Model reachable", False,
                f"{model} is not available to this key. Set OPENAI_MODEL to one this account may call.",
            )
            return
        self._record("Model reachable", False, f"HTTP {response.status_code}: {self._error_of(response)}")

    def _check_completion(self, key, model, base_url):
        """
        The first request that goes through our own code rather than requests.

        This is where the reasoning-model divergence is actually proved: the
        provider module decides between `max_tokens` and
        `max_completion_tokens` and whether to send `temperature` at all, and
        a 400 here means that decision was wrong for this model.
        """
        try:
            response = openai_client.complete(
                api_key=key, model=model, base_url=base_url,
                system="Answer with a single word and nothing else.",
                messages=[{"role": "user", "content": "Reply with the word: ready"}],
                max_tokens=PROBE_TOKENS, temperature=0.0,
            )
        except AIProviderError as exc:
            self._record("Real completion", False, str(exc))
            return

        text = (response.text or "").strip()
        self._record(
            "Real completion", bool(text),
            f"replied {text!r} in {response.latency_ms}ms "
            f"({response.input_tokens} in / {response.output_tokens} out, stop={response.stop_reason or 'n/a'})"
            if text else "the model returned an empty reply",
        )

    def _check_memory(self, key, model, base_url):
        """
        Whether earlier turns actually reach the model.

        Asked as a two-turn history rather than by concatenating the turns
        into one message, because concatenation is what a broken
        `build_history` would silently degrade to and it would still pass.
        """
        history = [
            {"role": "user", "content": f"Hello, my name is {MEMORY_NAME} and I want to buy a {MEMORY_ITEM}."},
            {"role": "assistant", "content": "Certainly. I can help you with that."},
            {"role": "user", "content": "What is my name, and what did I say I wanted?"},
        ]
        try:
            response = openai_client.complete(
                api_key=key, model=model, base_url=base_url,
                system="You are a shop assistant. Answer in one short sentence.",
                messages=history, max_tokens=ANSWER_TOKENS, temperature=0.0,
            )
        except AIProviderError as exc:
            self._record("Conversation memory", False, str(exc))
            return

        text = (response.text or "").strip()
        remembered = MEMORY_NAME.lower() in text.lower() and "kettle" in text.lower()
        self._record(
            "Conversation memory", remembered,
            f"answered {text!r} -- "
            + ("recalled both the name and the item from earlier turns" if remembered
               else "did not recall the earlier turns; history may not be reaching the model"),
        )

    # ------------------------------------------------------------------
    # Checks that need the database
    # ------------------------------------------------------------------

    def _scratch(self, *, slug):
        """
        A throwaway tenant with one connected number and a funded owner.

        Built through the real models so the paths under test see exactly what
        they see in production. Rolled back by the caller.

        The slug carries the run's start time so that `--keep`, which is the
        only way any of this survives, can be used twice without colliding
        with the tenant the previous run left behind.
        """
        from django.contrib.auth import get_user_model

        from apps.tenants.models import Tenant
        from apps.whatsapp.models import WhatsAppAccount

        slug = f"{slug}-{self.run_id}"
        tenant = Tenant.objects.create(name="OpenAI verification", slug=slug, kind=Tenant.Kind.CUSTOMER)
        owner = get_user_model().objects.create_user(
            username=f"{slug}-owner", password="unused-scratch-account", tenant=tenant, credits=100,
        )
        account = WhatsAppAccount(
            tenant=tenant, user=owner, name="Verify", phone_number="+15550009999",
            phone_number_id=f"pnid-{slug}", status=WhatsAppAccount.Status.CONNECTED,
        )
        account.set_access_token("scratch")
        account.set_app_secret("scratch")
        account.save()
        return tenant, owner, account

    def _stop_the_network(self, stack):
        """
        Two interceptions, and both are load-bearing.

        The transport patch stops this process from calling WhatsApp. The task
        patch stops it from *asking another process to* -- a Celery worker is
        running beside this command, it does not share this transaction, and a
        queued send would be a real message to a real number. Rolling back is
        not sufficient on its own for exactly that reason.
        """
        from apps.messaging import tasks as messaging_tasks

        sent = []
        transport = stack.enter_context(patch("apps.messaging.services.ConnectionManager.send_text_message"))
        transport.side_effect = lambda *a, **kw: sent.append((a, kw)) or _ok_send()
        stack.enter_context(patch.object(messaging_tasks.send_message_task, "delay", lambda *a, **kw: None))
        return sent

    def _agent(self, tenant, base, model):
        """The tenant's agent, pointed at OpenAI, answering only from documents."""
        return AIAgentConfig.objects.create(
            tenant=tenant, is_enabled=True, provider=AIAgentConfig.Provider.OPENAI,
            model=model, knowledge_base=base,
            reply_scope=AIAgentConfig.ReplyScope.EVERY,
            answer_from_knowledge_only=True, max_output_tokens=ANSWER_TOKENS,
        )

    def _check_end_to_end(self, model, *, keep):
        """
        A webhook in, an outbound row out, and a real model in the middle.

        Deliberately the whole ladder -- `record_inbound_message` plus the
        signal the webhook view sends -- rather than calling `services.answer`
        directly. Calling answer() directly would skip the part most likely to
        be broken by a change elsewhere: whether the AI is reached at all.
        """
        from apps.inbox import services as inbox_services
        from apps.inbox.signals import inbound_message_recorded
        from apps.messaging.models import Message
        from apps.whatsapp.models import WhatsAppWebhookEvent

        with ExitStack() as stack:
            stack.enter_context(self._rollback(keep))
            sent = self._stop_the_network(stack)
            tenant, owner, account = self._scratch(slug="verify-openai-e2e")
            base = KnowledgeBase.objects.create(tenant=tenant, name="General", is_default=True)
            self._agent(tenant, base, model)
            document = KnowledgeDocument.objects.create(
                tenant=tenant, knowledge_base=base, title="Order handling", content=RAG_DOCUMENT,
            )
            knowledge.rebuild(document)

            event = WhatsAppWebhookEvent.objects.create(
                account=account, event_type=WhatsAppWebhookEvent.EventType.INBOUND_MESSAGE,
                provider_event_id="wamid.verify.1",
                payload={
                    "message": {
                        "id": "wamid.verify.1", "from": "15551234567", "type": "text",
                        "text": {"body": "hi, when do you pick orders?"},
                    },
                    "profile_name": "Verification",
                },
                related_message_id="wamid.verify.1",
            )

            started = time.monotonic()
            inbound = inbox_services.record_inbound_message(event)
            inbound_message_recorded.send(
                sender=Message, message=inbound,
                is_new_conversation=getattr(inbound, "is_new_conversation", False),
            )
            elapsed = int((time.monotonic() - started) * 1000)

            outbound = list(
                Message.objects.filter(tenant=tenant, direction=Message.Direction.OUTBOUND).order_by("id")
            )
            if not outbound:
                self._record("End-to-end reply", False, "the ladder produced no outbound message")
                return

            reply = outbound[-1]
            ok = bool((reply.body or "").strip())
            self._record(
                "End-to-end reply", ok,
                f"webhook -> reply in {elapsed}ms on {account.phone_number}, queued as message {reply.pk}: "
                f"{(reply.body or '')[:200]!r}",
            )
            self._record(
                "Nothing was actually sent", not sent,
                "the transport was intercepted, so no WhatsApp message left this machine",
            )

    def _check_rag(self, model, *, keep):
        """
        Whether the answer came from the document or from the model's memory.

        The planted fact contradicts the common one on purpose: nineteen days
        where the world says fourteen. A model answering from its own weights
        says fourteen and fails this, which a plausible fact would have let it
        pass.
        """
        with ExitStack() as stack:
            stack.enter_context(self._rollback(keep))
            self._stop_the_network(stack)
            tenant, owner, account = self._scratch(slug="verify-openai-rag")
            base = KnowledgeBase.objects.create(tenant=tenant, name="General", is_default=True)
            config = self._agent(tenant, base, model)
            document = KnowledgeDocument.objects.create(
                tenant=tenant, knowledge_base=base, title="Order handling", content=RAG_DOCUMENT,
            )
            knowledge.rebuild(document)

            result = services.ask(tenant.pk, RAG_QUESTION, knowledge_base_id=base.pk, config=config)
            answer = (result.get("answer") or "").strip()
            grounded = any(token in answer.lower() for token in RAG_EXPECTED)

            if not result.get("answered"):
                self._record("RAG answering", False, result.get("detail") or "the agent declined to answer")
                return

            self._record(
                "RAG answering", grounded,
                f"answered {answer[:200]!r} from {[s['title'] for s in result.get('sources') or []]} "
                f"(relevance {result.get('relevance')}) -- "
                + ("the planted nineteen-day figure came back, so the answer is grounded in the document"
                   if grounded else
                   "the planted figure did not come back; the answer may not be grounded in the document"),
            )

            # The refusal path is the other half of RAG and the one that keeps
            # the agent honest, so it is worth the second request: a question
            # the document cannot answer must not be answered anyway.
            off_topic = services.ask(
                tenant.pk, "what is the share price of Reliance Industries today?",
                knowledge_base_id=base.pk, config=config,
            )
            self._record(
                "Refuses what the documents do not cover", not off_topic.get("answered"),
                off_topic.get("detail") or f"answered anyway: {(off_topic.get('answer') or '')[:160]!r}",
            )

    # ------------------------------------------------------------------
    # Failure handling
    # ------------------------------------------------------------------

    def _check_errors(self, key, model, base_url):
        """
        The four ways this fails in production, each provoked for real where
        provoking it is possible.

        Rate limiting is the exception and is checked differently -- see below.
        """
        # An invalid key. A real 401 from the vendor, not a simulated one.
        self._expect_failure(
            "Invalid API key rejected",
            api_key="sk-thiskeyisnotreal000000000000000000000000000000000",
            model=model, base_url=base_url, expect="401",
        )

        # A model that cannot exist. A real 404.
        self._expect_failure(
            "Unavailable model rejected",
            api_key=key, model="gpt-0-does-not-exist", base_url=base_url, expect="404",
        )

        # A timeout, forced by giving a real request no time to finish. This
        # goes through requests' own timeout, which is the same path a slow
        # OpenAI would take.
        self._expect_failure(
            "Timeout surfaces as a provider error",
            api_key=key, model=model, base_url=base_url, expect="", timeout=0.001,
        )

        # An unreachable endpoint -- the shape of a DNS failure or an outage.
        self._expect_failure(
            "Unreachable endpoint surfaces as a provider error",
            api_key=key, model=model, base_url="https://api.openai.invalid/v1", expect="",
        )

        # Rate limiting cannot be provoked on demand without deliberately
        # abusing the account, which is a worse thing to do than to check this
        # one differently. What is verified instead is the handling: that a 429
        # becomes an AIProviderError carrying the billing hint, using a stubbed
        # response with a real 429 body.
        self._check_rate_limit(key, model, base_url)

    def _expect_failure(self, name, *, api_key, model, base_url, expect, timeout=None):
        try:
            openai_client.complete(
                api_key=api_key, model=model, base_url=base_url,
                system="", messages=[{"role": "user", "content": "hi"}],
                max_tokens=PROBE_TOKENS, timeout=timeout,
            )
        except AIProviderError as exc:
            message = str(exc)
            ok = expect in message if expect else True
            leaked = api_key and api_key in message
            if leaked:
                self._record(name, False, "the error message contained the API key")
                return
            self._record(name, ok, f"raised AIProviderError: {message[:220]}")
            return
        except Exception as exc:  # noqa: BLE001 - anything else is the bug
            self._record(name, False, f"raised {type(exc).__name__} instead of AIProviderError: {exc}")
            return
        self._record(name, False, "the call unexpectedly succeeded")

    def _check_rate_limit(self, key, model, base_url):
        """A genuine 429 body, put through the genuine error path."""
        class _429:
            status_code = 429
            text = ""

            @staticmethod
            def json():
                return {"error": {
                    "message": "Rate limit reached for gpt-4o-mini in organization org-x on requests per min (RPM).",
                    "type": "requests", "code": "rate_limit_exceeded",
                }}

        with patch("apps.ai.providers.openai.requests.post", return_value=_429()):
            try:
                openai_client.complete(
                    api_key=key, model=model, base_url=base_url,
                    system="", messages=[{"role": "user", "content": "hi"}], max_tokens=PROBE_TOKENS,
                )
            except AIProviderError as exc:
                message = str(exc)
                ok = "429" in message and "rate limit" in message.lower()
                self._record("Rate limit handled", ok, f"raised AIProviderError: {message[:220]}")
                return
        self._record("Rate limit handled", False, "a 429 did not raise")

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _rollback(self, keep):
        """Everything written inside is undone, unless --keep was passed."""
        if keep:
            return transaction.atomic()
        return _Rollback()

    def _error_of(self, response) -> str:
        try:
            return (response.json().get("error") or {}).get("message", "")[:220]
        except ValueError:
            return response.text[:220]

    def _heading(self, title):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(title))

    def _record(self, name, ok, detail="", skipped=False):
        self.results.append(Result(name, ok, detail, skipped))
        mark = "SKIP" if skipped else ("PASS" if ok else "FAIL")
        style = self.style.WARNING if skipped else (self.style.SUCCESS if ok else self.style.ERROR)
        self.stdout.write(f"  {style(mark)}  {name}")
        if detail:
            self.stdout.write(f"        {detail}")

    def _summary(self):
        self._heading("Summary")
        failed = [result for result in self.results if not result.ok and not result.skipped]
        passed = [result for result in self.results if result.ok and not result.skipped]
        self.stdout.write(f"  {len(passed)} passed, {len(failed)} failed, {len(self.results)} checks")
        for result in failed:
            self.stdout.write(self.style.ERROR(f"  FAILED: {result.name} -- {result.detail}"))
        if not failed:
            self.stdout.write(self.style.SUCCESS("  The OpenAI integration is working against the real API."))


class _Rollback:
    """An atomic block that always rolls back, however it exits."""

    def __enter__(self):
        self._atomic = transaction.atomic()
        self._atomic.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        transaction.set_rollback(True)
        self._atomic.__exit__(exc_type, exc, tb)
        return False


def _ok_send():
    from apps.whatsapp.providers.base import SendResult

    return SendResult(success=True, provider_message_id="wamid.verify.intercepted")
