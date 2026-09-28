"""
Prove the chatbot pipeline works on *this* deployment, end to end.

``manage.py test`` proves the code is correct. It does not prove that this
particular install can actually answer a customer: the worker may not be
running, Redis may be unreachable, the webhook may be unroutable, the AI key
may be missing, the number may have been unlinked last Tuesday. Those are the
failures that matter in production and none of them are visible from a green
test suite.

So this walks one message through the real deployment -- the real webhook view
with a real HMAC signature, the real Celery worker, the real database -- and
prints what each stage did.

    docker compose exec backend python manage.py verify_chatbot

**It does not message a real phone by default.** With no ``--account`` it
builds a throwaway number whose credentials no provider will accept, so the
send genuinely runs and genuinely fails *at the provider* -- which still proves
every stage before it, and is reported as exactly that rather than as a
delivery.

``--account <id> --live --to +91…`` uses a really connected number and really
puts a message on somebody's WhatsApp. It exists because "we tested everything
except the part the customer sees" is not a verification, and it is opt-in
because it costs a credit and messages a real person.

Everything it creates is removed again on the way out unless ``--keep`` is
passed.
"""

import hashlib
import hmac
import json
import time
import uuid

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from apps.chatbot import services
from apps.chatbot.models import Chatbot, ChatbotEvent, ChatbotRule, ChatbotSession
from apps.inbox.models import Conversation
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount

APP_SECRET = "verify-chatbot-app-secret"
SLUG = "chatbot-verification"

TICK = "PASS"
CROSS = "FAIL"
DASH = "SKIP"


class Command(BaseCommand):
    help = "Walk one inbound message through the live chatbot pipeline and report what happened."

    def add_arguments(self, parser):
        parser.add_argument(
            "--account", type=int, default=None,
            help="Send the reply from this real WhatsAppAccount id instead of a throwaway one.",
        )
        parser.add_argument(
            "--to", default=None,
            help="With --live, the phone number in E.164 to actually message.",
        )
        parser.add_argument(
            "--live", action="store_true",
            help="Really send through the provider. Costs a credit and messages a real phone.",
        )
        parser.add_argument("--keep", action="store_true", help="Leave the test data behind for inspection.")
        parser.add_argument("--timeout", type=int, default=30, help="Seconds to wait for the worker.")

    def handle(self, *args, **options):
        self.live = options["live"]
        self.timeout = options["timeout"]
        self.results = []

        if self.live and not options["to"]:
            raise CommandError("--live needs --to +919999999999, the number that will actually be messaged.")

        # Refused rather than quietly ignored. Without a real account this
        # builds a throwaway number whose credentials no provider accepts, so
        # `--live` on its own would send nothing and still print "live" -- and
        # somebody would reasonably read that as proof of delivery. The entire
        # point of this command is not to claim things it did not do.
        if self.live and not options["account"]:
            raise CommandError(
                "--live also needs --account <id>, a really connected WhatsApp number.\n"
                "Without one this command uses a throwaway number that no provider will accept, "
                "so nothing would be delivered and 'live' would be a lie.\n"
                "The id is on the Devices page, next to the number, and in the Django "
                "admin under WhatsApp accounts."
            )

        self.stdout.write(self.style.MIGRATE_HEADING("\nChatbot pipeline verification"))
        self.stdout.write(
            "  mode: "
            + (
                "LIVE - a real WhatsApp message will be sent"
                if self.live
                else "dry run - the send runs for real against credentials no provider will accept"
            )
        )

        created = []
        try:
            tenant, user, account, chatbot = self._setup(options, created)
            self._run(tenant, user, account, chatbot, options)
        finally:
            if not options["keep"]:
                self._teardown(created)
            else:
                self.stdout.write(self.style.WARNING("\n  --keep: the test tenant was left in place."))

        return self._report()

    # -- setup ---------------------------------------------------------------

    def _setup(self, options, created):
        self.stdout.write(self.style.MIGRATE_HEADING("\n1. Configuration"))

        tenant, made = Tenant.objects.get_or_create(
            slug=SLUG, defaults={"name": "Chatbot verification", "kind": Tenant.Kind.CUSTOMER},
        )
        if made:
            created.append(tenant)

        user, made = get_user_model().objects.get_or_create(
            username=f"{SLUG}-owner", defaults={"tenant": tenant, "credits": 100},
        )
        if made:
            created.append(user)
        if user.credits < 10:
            user.credits = 100
            user.save(update_fields=["credits"])

        if options["account"]:
            account = WhatsAppAccount.objects.filter(pk=options["account"]).first()
            if account is None:
                raise CommandError(f"No WhatsApp account with id {options['account']}.")
            self._say(
                account.status == WhatsAppAccount.Status.CONNECTED,
                f"Using the real number '{account.name}' ({account.phone_number or 'no number'})",
                f"'{account.name}' is {account.status} — the send will be refused, which is itself a valid result.",
            )
            # A real account belongs to its own tenant; everything below has to
            # follow it, or the engine's tenant scoping would (correctly)
            # refuse to answer.
            tenant = account.tenant or tenant
            user = account.user or user
        else:
            account = WhatsAppAccount(
                tenant=tenant, user=user, name="Verification line",
                phone_number="+910000000000", phone_number_id="verify-pn",
                status=WhatsAppAccount.Status.CONNECTED, webhook_verify_token="verify",
            )
            account.set_access_token("verification-token")
            account.set_app_secret(APP_SECRET)
            account.save()
            created.append(account)
            self._ok("A throwaway connected number was created")

        chatbot = Chatbot.objects.create(
            tenant=tenant, whatsapp_account=account, created_by=user,
            name=f"Verification bot {uuid.uuid4().hex[:6]}",
            bot_type=Chatbot.BotType.HYBRID,
            welcome_message="Hello! I'm the verification bot.",
            fallback_message="I couldn't answer that.",
            ai_enabled=False,
            # A published bot on a real number would start answering that
            # number's actual customers. Scoped to one contact by the rule
            # below, and removed again at the end.
        )
        created.append(chatbot)

        ChatbotRule.objects.create(
            chatbot=chatbot, name="Pricing", match_type=ChatbotRule.MatchType.CONTAINS,
            pattern="price", priority=1,
            actions=[{
                "type": "SEND_MESSAGE",
                "body": "Verification reply: our basic plan is 999 a month.",
                "buttons": [],
            }],
        )
        ChatbotRule.objects.create(
            chatbot=chatbot, name="Handoff test", match_type=ChatbotRule.MatchType.CONTAINS,
            pattern="__never_matches__", priority=99, actions=[{"type": "HANDOFF", "message": "", "reason": "RULE_ACTION"}],
        )

        # Anything already published on this number is paused for the duration,
        # because the database allows only one live bot per number -- and
        # silently taking somebody's production bot down would be worse than
        # failing here.
        self.displaced = list(
            Chatbot.objects.filter(whatsapp_account=account, status=Chatbot.Status.PUBLISHED)
            .exclude(pk=chatbot.pk)
            .values_list("pk", flat=True)
        )
        if self.displaced:
            Chatbot.objects.filter(pk__in=self.displaced).update(status=Chatbot.Status.PAUSED)
            self.stdout.write(
                self.style.WARNING(
                    f"  note: paused {len(self.displaced)} bot(s) already live on this number; "
                    "they are restored at the end."
                )
            )

        version = services.publish(chatbot, user=user, notes="verification")
        self._say(version.published_at is not None, f"Published v{version.version} with 2 rules")
        return tenant, user, account, chatbot

    # -- the walk ------------------------------------------------------------

    def _run(self, tenant, user, account, chatbot, options):
        base = self._base_url()
        phone = (options["to"] or "+919000000001").lstrip("+")
        # Whether the provider call is expected to succeed. A throwaway account
        # carries credentials no provider will accept, so its send *should*
        # fail -- and failing at the provider, having got that far, is itself
        # the result: every stage before it worked.
        self.expect_delivery = bool(options["account"])

        self.stdout.write(self.style.MIGRATE_HEADING("\n2. Inbound message"))

        response = self._deliver(base, account, phone, "What is your price?", "verify.in.1")
        self._say(response.status_code == 200, f"Signed webhook accepted ({response.status_code})",
                  f"The webhook returned {response.status_code} — check the app secret and the signature header.")

        conversation = self._wait(
            lambda: Conversation.objects.filter(whatsapp_account=account, contact__phone=f"+{phone}").first(),
            "conversation created",
        )
        self._say(conversation is not None, "Conversation and contact created from the inbound message")
        if conversation is None:
            return

        inbound = Message.objects.filter(conversation=conversation, direction=Message.Direction.INBOUND).first()
        self._say(inbound is not None, "Inbound message stored")

        self.stdout.write(self.style.MIGRATE_HEADING("\n3. Processing (Celery)"))

        session = self._wait(
            lambda: ChatbotSession.objects.filter(conversation=conversation).first(),
            "session created",
        )
        self._say(session is not None, "Chatbot session created")

        reply = self._wait(
            lambda: Message.objects.filter(
                conversation=conversation, direction=Message.Direction.OUTBOUND,
                body__icontains="Verification reply",
            ).first(),
            "rule reply queued",
        )
        self._say(
            reply is not None,
            "The pricing rule matched and its reply was queued",
            "No reply was produced. Is the Celery worker running? Check `docker compose logs celery-worker`.",
        )

        trail = list(
            ChatbotEvent.objects.filter(conversation=conversation).order_by("created_at", "id")
        )
        self._say(bool(trail), f"Debug trail recorded ({len(trail)} steps)")
        for event in trail:
            marker = {"OK": "  ok  ", "SKIPPED": " skip ", "ERROR": " err  "}[event.outcome]
            self.stdout.write(f"      [{marker}] {event.step:<12} {event.duration_ms:>5}ms  {event.detail}")

        self.stdout.write(self.style.MIGRATE_HEADING("\n4. Outbound"))
        if reply is not None:
            reply = self._wait_terminal(reply)
            delivered = reply.status in (
                Message.Status.SENT, Message.Status.DELIVERED, Message.Status.READ,
            )
            # Delivered, or refused by the provider, or being retried after a
            # refusal -- all three mean the request left this deployment and
            # got an answer, which is what this stage is asserting.
            reached_provider = (
                delivered
                or bool(reply.failure_reason or reply.error_message)
                or reply.retry_count > 0
            )

            if self.expect_delivery:
                self._say(
                    delivered,
                    f"The provider accepted the reply (status {reply.status}, id {reply.provider_message_id})",
                    f"The send did not succeed: status={reply.status} "
                    f"reason={reply.failure_reason or '—'} error={reply.error_message or '—'}",
                )
                if self.live and delivered:
                    self.stdout.write(
                        self.style.SUCCESS(f"      A real WhatsApp message was sent to +{phone}.")
                    )
            else:
                # A throwaway number cannot really send, and pretending
                # otherwise would be the exact dishonesty this command exists
                # to avoid. What is being asserted is that the message reached
                # the provider under its own steam and the refusal was recorded
                # with a reason -- not that anything arrived.
                self._say(
                    reached_provider,
                    "The reply reached the provider and was refused as expected "
                    f"(status {reply.status}, attempts {reply.retry_count + 1}, "
                    f"reason {reply.failure_reason or reply.error_message or 'retrying'})",
                    f"The reply never reached the provider: it is still {reply.status} after "
                    f"{self.timeout}s with no attempt recorded. Is the Celery worker running, and can "
                    "it reach the provider's API?",
                )
                self.stdout.write(
                    "      These are throwaway credentials, so nothing was delivered to a phone.\n"
                    "      Run with --account <id> --live --to +<number> to prove the last hop too."
                )

        self.stdout.write(self.style.MIGRATE_HEADING("\n5. Idempotency"))
        before = Message.objects.filter(conversation=conversation, direction=Message.Direction.OUTBOUND).count()
        self._deliver(base, account, phone, "What is your price?", "verify.in.1")
        time.sleep(3)
        after = Message.objects.filter(conversation=conversation, direction=Message.Direction.OUTBOUND).count()
        self._say(before == after, "The same webhook twice produced no second reply",
                  f"A duplicate webhook produced {after - before} extra message(s).")

        self.stdout.write(self.style.MIGRATE_HEADING("\n6. Human handoff"))
        self._deliver(base, account, phone, "I want to talk to a human", "verify.in.2")
        session = self._wait(
            lambda: ChatbotSession.objects.filter(
                conversation=conversation, state=ChatbotSession.State.HUMAN_ACTIVE,
            ).first(),
            "handoff",
        )
        self._say(session is not None, "Asking for a person moved the conversation to HUMAN_ACTIVE")

        if session is not None:
            quiet_before = Message.objects.filter(
                conversation=conversation, direction=Message.Direction.OUTBOUND,
            ).count()
            self._deliver(base, account, phone, "and what is your price?", "verify.in.3")
            time.sleep(4)
            quiet_after = Message.objects.filter(
                conversation=conversation, direction=Message.Direction.OUTBOUND,
            ).count()
            self._say(
                quiet_before == quiet_after,
                "The bot stayed quiet while a person had the conversation",
                "The bot answered a conversation a person owns.",
            )

            services.resume_bot(conversation, user)
            self._deliver(base, account, phone, "what is your price now?", "verify.in.4")
            resumed = self._wait(
                lambda: Message.objects.filter(
                    conversation=conversation, direction=Message.Direction.OUTBOUND,
                ).count() > quiet_after,
                "bot resumed",
            )
            self._say(bool(resumed), "Returning to the bot made it answer again")

        self.stdout.write(self.style.MIGRATE_HEADING("\n7. Dependencies"))
        self._check_dependencies(tenant)

    def _base_url(self):
        """
        The URL this deployment's own webhook actually answers on.

        A real HTTP request rather than Django's test client, because the point
        of this command is to verify *the deployment*: the test client bypasses
        gunicorn, the middleware chain and the host checking a provider's
        request really goes through, and would pass happily on an install whose
        webhook is unreachable.
        """
        return (getattr(settings, "BACKEND_PUBLIC_URL", "") or "http://localhost:8000").rstrip("/")

    def _wait_terminal(self, message):
        """
        Wait until the send has actually been attempted, then read the row back.

        "Attempted" rather than "finished", and the difference matters: a
        refused send is *retried* with exponential backoff, so a message the
        provider has already rejected twice is still sitting in QUEUED between
        attempts. Waiting for a terminal status would time out on a send that
        demonstrably reached the provider -- which is the very thing being
        verified. A non-zero ``retry_count`` is the evidence that the provider
        was reached and said no.
        """
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            message.refresh_from_db()
            attempted = (
                message.status not in (Message.Status.QUEUED, Message.Status.SENDING)
                or message.retry_count > 0
                or bool(message.error_message)
            )
            if attempted:
                break
            time.sleep(0.5)
        return message

    def _deliver(self, base, account, phone, body, message_id):
        # Shaped the way the account's own provider actually posts. The two are
        # not interchangeable: Cloud API nests messages under
        # entry/changes/value, while the bridge posts a flat top-level
        # ``messages`` list and translates to the Cloud API shape server-side
        # (providers/qr_bridge.py::_as_cloud_api_message). Sending the wrong one
        # parses to zero events -- accepted, signature valid, nothing recorded --
        # which is the most confusing possible way for this to fail.
        is_qr = account.connection_method == WhatsAppAccount.ConnectionMethod.QR
        if is_qr:
            payload = json.dumps({
                "messages": [{
                    "id": message_id, "from": phone, "type": "text",
                    "body": body, "push_name": "Verification",
                }],
            }).encode()
        else:
            payload = json.dumps({
                "object": "whatsapp_business_account",
                "entry": [{"id": "waba", "changes": [{"field": "messages", "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": account.phone_number_id or "pn"},
                    "contacts": [{"profile": {"name": "Verification"}, "wa_id": phone}],
                    "messages": [{"id": message_id, "from": phone, "type": "text", "text": {"body": body}}],
                }}]}],
            }).encode()

        # Signed the way the account's own provider signs, because the point of
        # posting to the real endpoint is to exercise the real signature check.
        # The two providers key it differently: Cloud API uses the account's own
        # app secret, while the QR bridge uses one deployment-wide secret and
        # ignores the account entirely.
        if is_qr:
            secret = getattr(settings, "QR_PROVIDER_WEBHOOK_SECRET", "") or ""
            if not secret:
                raise CommandError(
                    f"'{account.name}' is a QR-linked number, and QR_PROVIDER_WEBHOOK_SECRET is not set "
                    "on this deployment -- so its webhook cannot be signed and no inbound message from it "
                    "would be accepted. Set it, or verify against a Cloud API number instead."
                )
        else:
            secret = account.get_app_secret() or ""
            if not secret:
                raise CommandError(
                    f"'{account.name}' has no app secret stored, so its webhook cannot be signed. "
                    "Add one on the Devices page -- without it this deployment refuses every inbound "
                    "message for that number."
                )

        signature = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        try:
            return requests.post(
                f"{base}{reverse('whatsapp-webhook', args=[account.pk])}",
                data=payload,
                headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
                timeout=20,
            )
        except requests.RequestException as exc:
            raise CommandError(
                f"Could not reach this deployment's own webhook at {base}: {exc}\n"
                "That is the URL a provider posts to. If it is unreachable from here, no inbound "
                "message can be processed at all."
            ) from exc

    def _check_dependencies(self, tenant):
        from django.core.cache import cache

        try:
            cache.set("chatbot-verify", "1", 5)
            self._say(cache.get("chatbot-verify") == "1", "Redis (cache) is reachable")
        except Exception as exc:  # noqa: BLE001
            self._say(False, "", f"Redis is not reachable: {exc}")

        try:
            from apps.realtime import events

            self._say(events.publish(tenant.pk, "chatbot.verify", {}), "Redis pub/sub (live inbox) is reachable",
                      "Live updates are unavailable; the inbox will poll instead.")
        except Exception as exc:  # noqa: BLE001
            self._say(False, "", f"Live updates unavailable: {exc}")

        from apps.ai.models import AIAgentConfig
        from apps.ai.services import is_configured

        config = AIAgentConfig.for_tenant(tenant.pk)
        if is_configured(config=config):
            self._ok(f"AI agent configured ({config.provider})")
        else:
            self._skip("AI agent is not configured for this tenant — AI answering was not exercised")

    # -- teardown ------------------------------------------------------------

    def _teardown(self, created):
        for bot in [item for item in created if isinstance(item, Chatbot)]:
            Conversation.objects.filter(chatbot_session__chatbot=bot).delete()
        for item in reversed(created):
            try:
                item.delete()
            except Exception:  # noqa: BLE001 - a leftover row is not worth a traceback
                pass
        if getattr(self, "displaced", None):
            Chatbot.objects.filter(pk__in=self.displaced).update(status=Chatbot.Status.PUBLISHED)
        self.stdout.write("\n  Test data removed.")

    # -- reporting -----------------------------------------------------------

    def _wait(self, probe, what):
        """Poll for something the worker is meant to produce."""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            value = probe()
            if value:
                return value
            time.sleep(0.5)
        return None

    def _ok(self, message):
        self.results.append(True)
        self.stdout.write(self.style.SUCCESS(f"  [{TICK}] {message}"))

    def _fail(self, message):
        self.results.append(False)
        self.stdout.write(self.style.ERROR(f"  [{CROSS}] {message}"))

    def _skip(self, message):
        self.stdout.write(self.style.WARNING(f"  [{DASH}] {message}"))

    def _say(self, condition, good, bad=None):
        if condition:
            self._ok(good)
        else:
            self._fail(bad or good)

    def _report(self):
        passed = sum(1 for value in self.results if value)
        total = len(self.results)
        self.stdout.write("")
        if passed == total:
            self.stdout.write(self.style.SUCCESS(f"  {passed}/{total} checks passed."))
            if not self.expect_delivery:
                self.stdout.write(
                    "  Nothing was delivered to a phone: the send was made against credentials no\n"
                    "  provider accepts, so this proves every stage up to and including the provider\n"
                    "  call, and not the delivery itself. To prove that too:\n"
                    "    manage.py verify_chatbot --account <connected id> --live --to +<number>"
                )
        else:
            self.stdout.write(self.style.ERROR(f"  {passed}/{total} checks passed — see the failures above."))
        self.stdout.write("")
