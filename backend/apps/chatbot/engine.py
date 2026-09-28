"""
One inbound message, from arrival to answer.

This is the orchestrator and it does exactly one thing per section, in an order
chosen so that the cheapest and most consequential checks come first. Nothing
here talks to a provider, renders a template, spends a credit or calls a model:
it decides, and delegates.

The order, and why:

===  ==================  ====================================================
1    claim               exactly-once, under a row lock, before anything else
2    loop protection     never answer ourselves, never answer forever
3    state               a conversation a person owns is not the bot's
4    timeout             a message a day later starts a new conversation
5    language            free, deterministic, needed by everything below
6    readability         a sticker gets a sentence, not a model call
7    welcome             the first message on a new conversation
8    resume              a flow already waiting on an answer continues
9    rules               what the tenant wrote down, in priority order
10   flow                the graph, if its Start trigger claims the message
11   AI                  a guess, so it goes last
12   fallback            nothing else spoke, so say something honest
===  ==================  ====================================================

Every step writes to the trail (``trace.py``), which is what makes "why did the
bot not reply?" a screen rather than an afternoon.
"""

import logging
import re

from django.db import transaction
from django.utils import timezone

from apps.messaging.models import Message

from . import actions as actions_module
from . import ai_reply, flow, language, matching, outbound, services, variables
from .models import Chatbot, ChatbotEvent, ChatbotHandoff, ChatbotSession
from .trace import Trace

logger = logging.getLogger("sendstack")

#: Most bot replies in one rolling minute, per conversation. This is not the
#: provider's rate limit (that is ``whatsapp.limits``, applied per number at
#: send time) -- it is the anti-runaway limit for one thread, which is where a
#: loop actually shows up.
MAX_REPLIES_PER_MINUTE = 8

#: Phrases that mean "stop talking to the robot", in the three languages this
#: platform's customers actually use. Matched before anything is retrieved, so
#: the request costs nothing and is never answered with a document quote.
HUMAN_REQUEST_PHRASES = (
    # English
    "talk to human", "talk to a human", "speak to a human", "speak to human",
    "talk to agent", "talk to an agent", "speak to an agent", "speak to agent",
    "talk to someone", "speak to someone", "real person", "human agent",
    "customer care", "customer service", "connect me", "put me through",
    "agent please", "human please", "live agent", "talk to support",
    # Hinglish / transliterated Hindi
    "agent se baat", "insaan se baat", "human se baat", "kisi se baat",
    "baat karni hai", "baat karao", "agent chahiye", "customer care se baat",
    # Hindi
    "एजेंट से बात", "इंसान से बात", "किसी से बात",
)

#: Words suggesting the customer is unhappy enough that a person should see it.
#: Deliberately blunt and deliberately short: this is a *routing* signal, not a
#: sentiment score, and every extra word is another way to route a satisfied
#: customer to a busy agent.
NEGATIVE_PHRASES = (
    "worst", "terrible", "useless", "fraud", "cheat", "scam", "refund my money",
    "very bad", "pathetic", "disgusting", "furious", "complaint", "legal action",
    "consumer court", "bakwas", "ghatiya", "dhokha", "paise wapas",
)

#: What ``apps.inbox`` writes as the body of a message that carried nothing
#: readable: "[sticker]", "[voice message]", "[location: ...]", "[image
#: received]". Every one is the whole body, wrapped in square brackets, which
#: is what makes this recognisable without the engine learning the list.
#:
#: The false positive -- a customer whose entire message is "[hello]" -- gets
#: the bot's "I understand text best" sentence, which is a graceful answer to
#: an odd message rather than a failure.
PLACEHOLDER_BODY = re.compile(r"^\[[^\]]*\]$")


class EngineContext:
    """
    Everything one turn needs, assembled once.

    A small object rather than eleven arguments threaded through fifteen
    functions. ``spoke``, ``replies_sent`` and ``extra_delay`` are mutable
    because the actions layer writes to them as it goes -- they are the turn's
    running state, not configuration.
    """

    __slots__ = (
        "message", "conversation", "contact", "account", "session", "chatbot",
        "chatbot_settings", "version", "trace", "trace_steps", "text", "button_id",
        "reply_language", "spoke", "replies_sent", "extra_delay", "_render_context",
    )

    def __init__(self, *, message, session, chatbot, version, trace):
        self.message = message
        self.conversation = message.conversation
        self.contact = message.contact
        self.account = message.whatsapp_account
        self.session = session
        self.chatbot = chatbot
        self.version = version
        self.chatbot_settings = services.settings_of(version, chatbot)
        self.trace = trace
        self.trace_steps = ChatbotEvent.Step
        self.text = (message.body or "").strip()
        self.button_id = message.replied_button_id or ""
        self.reply_language = Chatbot.Language.AUTO
        self.spoke = False
        self.replies_sent = 0
        self.extra_delay = 0
        self._render_context = None

    def render_context(self) -> dict:
        """
        The variable map, built once per turn and reused.

        Cached because a rule with four messages would otherwise rebuild the
        contact's whole render context four times, and one of those builds
        reads the contact's custom fields and the tenant's timezone.
        """
        if self._render_context is None:
            self._render_context = variables.build_context(
                self.contact, self.session, last_message=self.text,
            )
        return self._render_context

    def invalidate_context(self) -> None:
        """Called after a variable is set, so the next render sees it."""
        self._render_context = None


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def should_handle(message) -> bool:
    """
    Whether a chatbot owns this message at all -- one indexed query.

    Called from the inbound signal, inside the webhook request, so it must be
    cheap: everything expensive happens on the worker afterwards.
    """
    if message.direction != Message.Direction.INBOUND or message.conversation_id is None:
        return False
    return services.live_chatbot_for(message.whatsapp_account) is not None


class Busy(Exception):
    """Another worker is mid-turn on this conversation. The caller should come back."""


#: How long one turn may hold its conversation before the lock is assumed
#: abandoned. Longer than the slowest realistic turn (a model call plus three
#: tool rounds), short enough that a worker killed mid-turn does not wedge a
#: customer's conversation for the rest of the day.
TURN_LOCK_SECONDS = 90


def _turn_lock_key(conversation_id) -> str:
    return f"sendstack:chatbot:turn:{conversation_id}"


def handle(message_id: int, correlation_id: str = "", *, preview=None) -> str:
    """
    Run one inbound message through the bot. Returns a short outcome word.

    ``preview`` is ``(chatbot, version)`` and is how the builder's test panel
    runs a **draft** through this exact function. It replaces only the two
    lookups that would otherwise insist on a published bot -- every guard,
    every rule, every node and every action below them runs unchanged, which
    is the difference between a simulator and a second implementation of the
    conversation. Nothing reaches a phone: the preview's session is flagged
    and ``outbound.send_now`` captures instead of sending.

    Safe to call twice with the same message: the claim below is what makes
    that a no-op rather than a second reply. That property is not optional --
    Celery delivers at least once, and a retried task must not double-answer.

    Raises ``Busy`` when another worker is already mid-turn on this
    conversation. Two turns running at once is not a data-integrity problem --
    every write is a targeted UPDATE -- but it is an *ordering* one: a customer
    who sends "I want shoes" and "size 9" a second apart would have the second
    answered against a flow position the first had not finished writing. The
    caller retries rather than waits, because blocking a worker on a lock held
    across a model call is how a queue backs up.

    The lock is a cache key rather than a held database transaction, precisely
    because a turn can take seconds: holding `SELECT ... FOR UPDATE` across an
    AI call would tie up a connection from a small pool for the duration.
    """
    message = (
        Message.objects
        .select_related("conversation", "contact", "whatsapp_account", "conversation__tenant")
        .filter(pk=message_id)
        .first()
    )
    if message is None or message.conversation_id is None:
        return "GONE"

    trace = Trace(
        tenant_id=message.tenant_id, conversation_id=message.conversation_id,
        message_id=message.pk, correlation_id=correlation_id or None,
    )

    from django.core.cache import cache

    key = _turn_lock_key(message.conversation_id)
    try:
        acquired = cache.add(key, str(message_id), TURN_LOCK_SECONDS)
    except Exception:  # noqa: BLE001
        # No cache means no ordering guarantee, and going quiet would be far
        # worse than answering slightly out of order. The *duplicate*
        # protection does not depend on this -- it is the session's high-water
        # mark, which is in PostgreSQL.
        logger.warning("Could not take the chatbot turn lock; proceeding unserialised.", exc_info=True)
        acquired = True

    if not acquired:
        trace.skipped(
            ChatbotEvent.Step.SESSION, "Another message on this conversation is still being handled.",
        )
        trace.flush()
        raise Busy(f"conversation {message.conversation_id} is mid-turn")

    try:
        return _handle(message, trace, preview=preview)
    except Exception:  # noqa: BLE001 - a crash here must not lose the trail
        logger.exception("The chatbot engine crashed on message %s.", message_id)
        trace.failed(ChatbotEvent.Step.ERROR, "The chatbot failed unexpectedly on this message.")
        return "ERROR"
    finally:
        try:
            cache.delete(key)
        except Exception:  # noqa: BLE001 - the TTL releases it anyway
            logger.debug("Could not release the chatbot turn lock.", exc_info=True)
        trace.flush()


def _handle(message, trace, preview=None) -> str:
    chatbot = preview[0] if preview else services.live_chatbot_for(message.whatsapp_account)
    if chatbot is None:
        trace.skipped(ChatbotEvent.Step.SESSION, "No published chatbot answers this number.")
        return "NO_BOT"

    trace.bind(chatbot_id=chatbot.pk)
    trace.step(
        ChatbotEvent.Step.RECEIVED, "Inbound message received.",
        provider=message.whatsapp_account.provider,
        account_id=message.whatsapp_account_id,
        message_type=message.message_type,
        from_phone=message.to_phone,
        external_message_id=message.provider_message_id or "",
    )

    # --- 1. claim, under the conversation's own lock -----------------------
    #
    # The lock is the session row, which is one per conversation. Two messages
    # from the same customer therefore serialise, which is what makes "sends
    # three messages in a row" produce three ordered answers rather than three
    # workers racing on one flow position.
    with transaction.atomic():
        session = _claim(message, chatbot, trace)
        if session is None:
            return "DUPLICATE"

    version = preview[1] if preview else _version_for(session, chatbot, trace)
    ctx = EngineContext(message=message, session=session, chatbot=chatbot, version=version, trace=trace)

    # --- 2. loop protection ------------------------------------------------
    loop = _loop_problem(ctx)
    if loop:
        trace.skipped(ChatbotEvent.Step.DEDUPE, loop)
        return "LOOP_BLOCKED"

    # --- 3. is this conversation the bot's? --------------------------------
    verdict = _state_verdict(ctx)
    if verdict:
        trace.skipped(ChatbotEvent.Step.STATE, verdict, state=session.state)
        return "NOT_BOTS"

    # --- 4. has the conversation gone cold? --------------------------------
    if services.has_timed_out(session, ctx.chatbot_settings):
        services.restart_session(session, chatbot)
        ctx.version = session.version or chatbot.published_version
        ctx.chatbot_settings = services.settings_of(ctx.version, chatbot)
        trace.step(ChatbotEvent.Step.SESSION, "The previous conversation had gone quiet; starting a new one.")

    _mark_active(ctx)

    # --- 5. language -------------------------------------------------------
    detected = language.detect(ctx.text)
    ctx.reply_language = language.resolve(
        getattr(ctx.chatbot_settings, "language", Chatbot.Language.AUTO),
        detected,
        session.detected_language,
    )
    if detected != Chatbot.Language.AUTO and detected != session.detected_language:
        ChatbotSession.objects.filter(pk=session.pk).update(detected_language=detected)
        session.detected_language = detected
    trace.step(
        ChatbotEvent.Step.LANGUAGE, f"Replying in {ctx.reply_language.lower()}.",
        detected=detected, configured=getattr(ctx.chatbot_settings, "language", ""),
    )

    # --- 6. can the bot read it? -------------------------------------------
    if not _is_readable(message):
        return _unreadable(ctx)

    # --- the customer asked for a person -----------------------------------
    if _wants_a_human(ctx.text) and getattr(ctx.chatbot_settings, "human_handoff_enabled", True):
        if services.handoff_rules(ctx.chatbot_settings).get("customer_request", True):
            services.hand_off(
                ctx, trigger=ChatbotHandoff.Trigger.CUSTOMER_REQUEST,
                detail="The customer asked to speak to a person.",
            )
            return "HANDOFF"

    # --- 7-11. who answers -------------------------------------------------
    outcome = _route(ctx)

    # --- 12. nothing spoke -------------------------------------------------
    if not ctx.spoke and outcome not in ("HANDOFF", "END", "TRANSFER", "AWAIT"):
        _fallback(ctx)
        outcome = "FALLBACK"
    elif ctx.spoke and outcome not in ("FALLBACK",):
        _clear_fallbacks(ctx)

    _publish_state(ctx)
    return outcome


def _route(ctx) -> str:
    """
    Decide who answers, in the ladder's order. Returns the outcome word.

    The first thing that speaks ends it. That single rule is what stops a
    keyword rule and a flow both firing and sending two replies seconds apart,
    which reads as a broken bot to the recipient and as spam to WhatsApp.
    """
    session = ctx.session
    settings_obj = ctx.chatbot_settings

    # Outside hours the bot says so and stops. Placed after the human-request
    # check on purpose -- somebody asking for a person at 2am should still be
    # queued for one rather than told the office is shut and left there.
    if not services.is_within_hours(ctx.chatbot, ctx.version):
        return _outside_hours(ctx)

    # The reply ceiling, before anything can speak rather than only before the
    # AI. The setting says "the bot has sent this many replies", and a limit
    # that counted model answers but not rule answers would be a limit that
    # did not mean what it said -- a bot looping on a catch-all rule is exactly
    # the case somebody sets this for.
    if _reply_limit_reached(ctx):
        return "HANDOFF"

    # 7. welcome -- the first thing this conversation has ever seen
    if session.messages_received <= 1 and session.bot_replies == 0:
        welcome = (getattr(settings_obj, "welcome_message", "") or "").strip()
        if welcome:
            outbound.deliver(
                ctx, outbound.text_spec(variables.render(welcome, ctx.render_context())),
                reference=f"chatbot:welcome:{ctx.chatbot.pk}",
            )
            ctx.trace.step(ChatbotEvent.Step.RESPONSE, "Welcome message sent.")
            # Deliberately *not* a return: a welcome greets, it does not
            # answer. Somebody whose first message is "what are your prices?"
            # should get the greeting and the price, not the greeting alone.

    # 8. the answer to a question the bot asked last time
    #
    # Before the flow and before the rules, because it is not an answer to
    # *this* turn -- it is the missing half of the previous one. A question can
    # be asked by a flow node or by a rule, and both park the same marker, so
    # storing the answer here is what makes "what size?" -> "9" work in either.
    _consume_pending_answer(ctx)

    if session.awaiting_node_id:
        outcome = flow.resume(ctx)
        if outcome:
            return outcome

    bot_type = getattr(settings_obj, "bot_type", Chatbot.BotType.HYBRID)

    # 9. rules
    if bot_type in (Chatbot.BotType.RULE_BASED, Chatbot.BotType.HYBRID):
        outcome = _run_rules(ctx)
        if outcome:
            return outcome

    # 10. the flow's own start
    #
    # Only when its trigger claims this message. A flow used to start on
    # anything at all, which made "run this graph when they say 'refund'"
    # impossible to express without a rule that duplicated its first node --
    # see ``flow.should_start``. A flow already part-way through is not asked:
    # it is resumed above, or carries on from ``current_node_id``.
    if bot_type in (Chatbot.BotType.RULE_BASED, Chatbot.BotType.HYBRID):
        if flow.nodes_of(ctx.version) and not session.current_node_id and flow.should_start(ctx):
            outcome = flow.run(ctx)
            if outcome and outcome != "DONE":
                return outcome
            if ctx.spoke:
                return "FLOW"

    # 11. the AI, last, and only what nothing else took
    if bot_type in (Chatbot.BotType.AI_BASED, Chatbot.BotType.HYBRID):
        if getattr(settings_obj, "ai_enabled", True):
            answered = ai_reply.answer(ctx, reference=f"chatbot:ai:{ctx.conversation.pk}")
            if answered:
                return "AI"
            if ctx.session.handed_off_at is not None:
                return "HANDOFF"
        else:
            ctx.trace.skipped(ChatbotEvent.Step.AI_REQUEST, "AI answering is switched off for this bot.")

    return ""


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------

def _claim(message, chatbot, trace):
    """
    Take exclusive ownership of this conversation and record the message once.

    Returns the locked session, or None when this message has already been
    handled. ``last_processed_message_id`` is a high-water mark rather than a
    set: message ids increase, so "less than or equal to what we have already
    done" catches both a redelivered task and a message overtaken by a newer
    one from the same customer. The second case is deliberate -- answering
    "what's the price?" after the customer has already followed it with "never
    mind" is worse than not answering it.
    """
    session = services.ensure_session(message.conversation, chatbot)
    locked = ChatbotSession.objects.select_for_update().filter(pk=session.pk).first()
    if locked is None:
        return None

    if locked.last_processed_message_id and message.pk <= locked.last_processed_message_id:
        trace.skipped(
            ChatbotEvent.Step.DEDUPE, "This message has already been handled.",
            last_processed=locked.last_processed_message_id,
        )
        return None

    ChatbotSession.objects.filter(pk=locked.pk).update(
        last_processed_message_id=message.pk,
        messages_received=locked.messages_received + 1,
        last_customer_message_at=timezone.now(),
        # A new message means any reply composed for the previous one is
        # stale. This is one of the three things that cancels a delayed send.
        delivery_token=locked.delivery_token + 1,
        chatbot=chatbot if locked.chatbot_id is None else locked.chatbot_id,
    )
    locked.refresh_from_db()
    trace.step(
        ChatbotEvent.Step.SESSION, "Session claimed.",
        session_id=locked.pk, state=locked.state, version=getattr(locked.version, "version", None),
    )
    return locked


def _version_for(session, chatbot, trace):
    """
    Which published snapshot this turn runs against.

    The rule is: **pin only what would break if it moved.**

    A conversation standing on a flow node keeps the version it started on. The
    node it is parked at, and the ones after it, may not exist in the new graph
    -- moving a customer mid-question onto a rewritten flow drops them somewhere
    arbitrary or nowhere at all.

    Everything else takes the current published version. A conversation that is
    merely open is not "inside" anything, and pinning it would mean a rule fixed
    at lunchtime does not reach a customer who wrote in that morning until their
    session times out -- which reads, correctly, as the fix not working.

    The pinned version is also released once the flow lets go, so a long-running
    conversation converges on current behaviour rather than staying on an old
    snapshot forever.
    """
    live = chatbot.published_version
    mid_flow = bool(session.awaiting_node_id or session.current_node_id)

    if mid_flow and session.version_id:
        if session.version_id != getattr(live, "pk", None):
            trace.step(
                ChatbotEvent.Step.SESSION,
                "Staying on the version this conversation started on -- it is part-way through a flow.",
                pinned_version=getattr(session.version, "version", None),
                live_version=getattr(live, "version", None),
            )
        return session.version

    if live is not None and session.version_id != live.pk:
        ChatbotSession.objects.filter(pk=session.pk).update(version=live)
        session.version = live
        session.version_id = live.pk
    return live or session.version


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------

def _loop_problem(ctx) -> str:
    """
    Why this message must not be answered, or "" when it may be.

    Four independent protections, because they catch four different loops:

    * **Our own number.** Two bots on two of this deployment's numbers messaging
      each other is the classic infinite loop, and it is invisible to every
      other check -- each side sees a perfectly ordinary inbound message.
    * **An automated message.** Belt and braces: the inbound path only ever
      writes INBOUND rows, so this cannot fire today. It is here so that a
      future path which loops an outbound message back through the engine
      fails closed rather than silently.
    * **A reply burst.** More than ``MAX_REPLIES_PER_MINUTE`` from this bot on
      this thread is a loop whatever caused it.
    * **A disconnected number**, which is not a loop but is the other reason to
      stop before doing expensive work -- there is nowhere to send the answer.
    """
    from apps.whatsapp.models import WhatsAppAccount

    if ctx.message.is_automated or ctx.message.direction != Message.Direction.INBOUND:
        return "This is not a customer message."

    phone = (ctx.contact.phone or "").lstrip("+")
    if phone:
        ours = WhatsAppAccount.objects.filter(
            tenant_id=ctx.session.tenant_id, phone_number__endswith=phone[-10:],
        ).exclude(phone_number="").exists()
        if ours:
            return "That number belongs to this workspace -- refusing to let two bots talk to each other."

    recent = Message.objects.filter(
        conversation_id=ctx.conversation.pk,
        direction=Message.Direction.OUTBOUND,
        is_automated=True,
        queued_at__gte=timezone.now() - timezone.timedelta(minutes=1),
    ).count()
    if recent >= MAX_REPLIES_PER_MINUTE:
        return f"This bot has already sent {recent} messages here in the last minute."

    problem = outbound.account_problem(ctx.account)
    if problem:
        return problem
    return ""


def _state_verdict(ctx) -> str:
    """Why the bot must stay out of this conversation, or "" when it may speak."""
    session = ctx.session

    if session.state == ChatbotSession.State.HUMAN_ACTIVE or session.handed_off_at is not None:
        return "A person is handling this conversation."

    if session.state in (ChatbotSession.State.RESOLVED, ChatbotSession.State.CLOSED):
        # A finished conversation that gets a new message is not finished. It
        # is reopened rather than ignored, which is what a customer coming back
        # two days later expects -- and the timeout check immediately after
        # this decides whether they get a fresh session or a continuation.
        ChatbotSession.objects.filter(pk=session.pk).update(
            state=ChatbotSession.State.BOT_ACTIVE, ended_at=None,
            resolution=ChatbotSession.Resolution.UNRESOLVED,
        )
        session.state = ChatbotSession.State.BOT_ACTIVE
        session.ended_at = None
        ctx.trace.step(ChatbotEvent.Step.STATE, "Reopened a finished conversation.")
        return ""

    # An agent who has taken the conversation in the inbox owns it, even
    # without a formal handoff -- assignment is how a person says "this is
    # mine", and a bot answering over the top of them is the complaint this
    # prevents.
    if ctx.conversation.assigned_agent_id and session.returned_to_bot_at is None:
        return "An agent is assigned to this conversation."
    return ""


def _reply_limit_reached(ctx) -> bool:
    """Whether this bot has said all it is allowed to say here."""
    limit = int(services.handoff_rules(ctx.chatbot_settings).get("max_bot_replies") or 0)
    if limit <= 0 or ctx.session.bot_replies < limit:
        return False
    ctx.trace.step(
        ChatbotEvent.Step.HANDOFF, f"The bot has sent its {limit}-message limit here.",
        outcome=ChatbotEvent.Outcome.SKIPPED,
    )
    if getattr(ctx.chatbot_settings, "human_handoff_enabled", True):
        services.hand_off(
            ctx, trigger=ChatbotHandoff.Trigger.REPLY_LIMIT,
            detail=f"The bot reached its limit of {limit} replies.",
        )
    return True


def _is_readable(message) -> bool:
    """
    Whether there is text here for the bot to work with.

    Media with a caption counts: somebody sending a photo of a product with
    "how much is this?" underneath has asked a perfectly answerable question,
    and refusing it because the message also carried an image would be worse
    than useless. A photo with no caption does not, and neither does a sticker
    or a shared location -- ``apps.inbox`` gives all of those a bracketed
    placeholder for a body, and answering "[sticker]" with the knowledge base
    is how a bot ends up quoting the refund policy at an emoji.

    Deliberately reads the *body* rather than the message type: the type says
    what the platform could store, and several unreadable kinds are stored as
    TEXT precisely because there is no attachment to keep.
    """
    body = (message.body or "").strip()
    if not body:
        return False
    return not PLACEHOLDER_BODY.match(body)


def _unreadable(ctx) -> str:
    """Say something honest about a sticker, a location, or an empty photo."""
    text = (getattr(ctx.chatbot_settings, "unsupported_message", "") or "").strip()
    ctx.trace.step(
        ChatbotEvent.Step.RESPONSE, "The message carried nothing the bot can read.",
        outcome=ChatbotEvent.Outcome.SKIPPED, message_type=ctx.message.message_type,
    )
    if not text:
        return "UNREADABLE"

    # An unreadable message still counts as the customer trying, so it counts
    # towards the fallback threshold: three stickers in a row is somebody who
    # needs a person.
    outbound.deliver(
        ctx, outbound.text_spec(variables.render(text, ctx.render_context())),
        reference=f"chatbot:unsupported:{ctx.conversation.pk}",
    )
    _count_fallback(ctx)
    return "UNREADABLE"


def _outside_hours(ctx) -> str:
    """
    Tell the customer nobody is here, at most once per period of silence.

    Anchored to the bot's own last message rather than sent on every inbound
    message: somebody who writes three times at midnight has been told, and
    repeating it each time is the behaviour that makes people mute a business.
    """
    session = ctx.session
    already_told = (
        session.last_bot_message_at is not None
        and session.last_customer_message_at is not None
        and session.last_bot_message_at > (session.last_customer_message_at - timezone.timedelta(hours=8))
        and session.bot_replies > 0
    )
    ctx.trace.step(
        ChatbotEvent.Step.HOURS, "Outside this bot's working hours.",
        outcome=ChatbotEvent.Outcome.SKIPPED, already_told=already_told,
    )
    if already_told:
        return "OUTSIDE_HOURS"

    text = (getattr(ctx.chatbot_settings, "outside_hours_message", "") or "").strip()
    if text:
        outbound.deliver(
            ctx, outbound.text_spec(variables.render(text, ctx.render_context())),
            reference=f"chatbot:hours:{ctx.conversation.pk}",
        )
    return "OUTSIDE_HOURS"


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def _consume_pending_answer(ctx) -> bool:
    """
    Store this message as the answer to the question the bot last asked.

    ``ASK_QUESTION`` parks the variable's name under the reserved ``__awaiting``
    key. It is read and cleared here rather than inside the flow, because a rule
    can ask a question too and a rule has no node to resume -- putting this in
    the flow's own resume path is what made a rule-asked question silently
    forget the answer.

    The marker is cleared whether or not the value could be stored: a variable
    name that has since become invalid must not leave the conversation waiting
    on an answer forever.
    """
    stored = dict(ctx.session.variables or {})
    pending = stored.pop("__awaiting", None)
    if not pending:
        return False

    try:
        stored[pending] = variables.coerce_value(ctx.text)
    except Exception:  # noqa: BLE001
        logger.exception("Could not store the answer to a chatbot question.")

    ChatbotSession.objects.filter(pk=ctx.session.pk).update(variables=stored)
    ctx.session.variables = stored
    ctx.invalidate_context()
    ctx.trace.step(
        ChatbotEvent.Step.FLOW, f"Recorded the answer as {pending}.", variable=pending,
    )
    return True


def _run_rules(ctx) -> str:
    """
    Find the first rule that claims this message and run its actions.

    Rules come from the *published version*, not from the live table, so
    somebody editing rules at lunchtime does not change what a customer
    mid-conversation is answered with. The cooldown is checked after the match
    rather than before, so a rule on cooldown still *claims* the message --
    otherwise a customer typing the same keyword twice would fall through to
    the AI on the second attempt and get a completely different answer.
    """
    rules = _rules_for(ctx)
    if not rules:
        return ""

    rule = matching.first_match(rules, text=ctx.text, button_id=ctx.button_id)
    if rule is None:
        ctx.trace.step(
            ChatbotEvent.Step.RULE, f"No rule matched (checked {len(rules)}).",
            outcome=ChatbotEvent.Outcome.SKIPPED, checked=len(rules),
        )
        return ""

    ctx.trace.step(
        ChatbotEvent.Step.RULE, f"Matched '{rule.get('name')}'.",
        rule_id=rule.get("id"), priority=rule.get("priority"), match_type=rule.get("match_type"),
    )

    if not _claim_cooldown(ctx, rule):
        ctx.trace.skipped(
            ChatbotEvent.Step.RULE, "That rule answered this contact a moment ago.",
            rule_id=rule.get("id"), cooldown_seconds=rule.get("cooldown_seconds"),
        )
        return "RULE_COOLDOWN"

    _count_rule_match(rule.get("id"))
    outcome = actions_module.run(ctx, rule.get("actions") or [], reference=f"chatbot:rule:{rule.get('id')}")
    return outcome or ("RULE" if ctx.spoke else "RULE_SILENT")


def _rules_for(ctx) -> list:
    """The rules this session runs, frozen at publish where there is a version."""
    if ctx.version is not None and ctx.version.rules:
        return ctx.version.rules
    from .matching import as_dict

    return [as_dict(rule) for rule in ctx.chatbot.rules.filter(is_active=True).order_by("priority", "id")]


def _claim_cooldown(ctx, rule) -> bool:
    """
    Whether this rule may fire for this contact now.

    Uses the platform cache with ``add``, which is atomic: two workers handed
    two messages from the same customer cannot both be told they have the slot.
    A cache that is down means every claim succeeds, which is the right way for
    this to fail -- a duplicate reply is a much smaller problem than a bot that
    goes silent because Redis restarted.
    """
    seconds = int(rule.get("cooldown_seconds") or 0)
    if seconds <= 0:
        return True

    from django.core.cache import cache

    key = f"sendstack:chatbot:cooldown:{rule.get('id')}:{ctx.contact.pk}"
    try:
        return bool(cache.add(key, "1", seconds))
    except Exception:  # noqa: BLE001 - see the docstring: fail open
        logger.warning("Could not read the rule cooldown cache; allowing the reply.", exc_info=True)
        return True


def _count_rule_match(rule_id) -> None:
    """Bump the analytics counter without reading it first."""
    if not rule_id:
        return
    from django.db.models import F

    from .models import ChatbotRule

    try:
        ChatbotRule.objects.filter(pk=rule_id).update(
            match_count=F("match_count") + 1, last_matched_at=timezone.now(),
        )
    except Exception:  # noqa: BLE001
        logger.debug("Could not count a rule match.", exc_info=True)


# ---------------------------------------------------------------------------
# Fallback and handoff conditions
# ---------------------------------------------------------------------------

def _fallback(ctx) -> None:
    """
    Nothing answered. Say so honestly, or hand over if this keeps happening.

    The handoff conditions are checked *before* the fallback message is sent,
    so a customer being handed over does not first receive "sorry, I didn't get
    that" and then "let me get a colleague" -- which reads as a bot arguing
    with itself.
    """
    rules = services.handoff_rules(ctx.chatbot_settings)
    fallbacks = ctx.session.consecutive_fallbacks + 1
    handoff_allowed = getattr(ctx.chatbot_settings, "human_handoff_enabled", True)

    trigger = None
    if handoff_allowed:
        threshold = int(rules.get("repeated_fallback") or 0)
        if threshold and fallbacks >= threshold:
            trigger = ChatbotHandoff.Trigger.REPEATED_FALLBACK
        elif rules.get("negative_sentiment") and _sounds_unhappy(ctx.text):
            trigger = ChatbotHandoff.Trigger.NEGATIVE_SENTIMENT
        elif _matches_handoff_keyword(ctx.text, rules.get("keywords") or ""):
            trigger = ChatbotHandoff.Trigger.KEYWORD

    _count_fallback(ctx)

    if trigger is not None:
        services.hand_off(
            ctx, trigger=trigger,
            detail={
                ChatbotHandoff.Trigger.REPEATED_FALLBACK: f"The bot could not answer {fallbacks} times in a row.",
                ChatbotHandoff.Trigger.NEGATIVE_SENTIMENT: "The customer sounded unhappy.",
                ChatbotHandoff.Trigger.KEYWORD: "The message matched a handoff keyword.",
            }[trigger],
        )
        return

    text = (getattr(ctx.chatbot_settings, "fallback_message", "") or "").strip()
    ctx.trace.step(
        ChatbotEvent.Step.FALLBACK, "Nothing answered; sending the fallback.",
        outcome=ChatbotEvent.Outcome.SKIPPED, consecutive=fallbacks,
    )
    if text:
        outbound.deliver(
            ctx, outbound.text_spec(variables.render(text, ctx.render_context())),
            reference=f"chatbot:fallback:{ctx.conversation.pk}",
        )


def _count_fallback(ctx) -> None:
    ChatbotSession.objects.filter(pk=ctx.session.pk).update(
        consecutive_fallbacks=ctx.session.consecutive_fallbacks + 1,
    )
    ctx.session.consecutive_fallbacks += 1


def _clear_fallbacks(ctx) -> None:
    if ctx.session.consecutive_fallbacks:
        ChatbotSession.objects.filter(pk=ctx.session.pk).update(consecutive_fallbacks=0)
        ctx.session.consecutive_fallbacks = 0


def _wants_a_human(text: str) -> bool:
    lowered = f" {(text or '').lower()} "
    return any(phrase in lowered for phrase in HUMAN_REQUEST_PHRASES)


def _sounds_unhappy(text: str) -> bool:
    lowered = f" {(text or '').lower()} "
    return any(phrase in lowered for phrase in NEGATIVE_PHRASES)


def _matches_handoff_keyword(text: str, keywords: str) -> bool:
    words = [word.strip().lower() for word in (keywords or "").split(",") if word.strip()]
    lowered = (text or "").lower()
    return any(word in lowered for word in words)


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------

def _mark_active(ctx) -> None:
    if ctx.session.state in (ChatbotSession.State.NEW, ChatbotSession.State.WAITING_FOR_CUSTOMER):
        ChatbotSession.objects.filter(pk=ctx.session.pk).update(state=ChatbotSession.State.BOT_ACTIVE)
        ctx.session.state = ChatbotSession.State.BOT_ACTIVE


def _publish_state(ctx) -> None:
    services._publish(ctx.session.tenant_id, "chatbot.state", {
        "conversation": ctx.conversation.pk,
        "chatbot": getattr(ctx.chatbot, "pk", None),
        "state": ctx.session.state,
        "bot_replies": ctx.session.bot_replies,
    })
