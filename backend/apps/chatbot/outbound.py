"""
Everything the bot says goes out through here, and through nothing else.

This module owns exactly two things the existing send pipeline does not
already do:

1. **The human-like delay**, and its cancellation.
2. **Counting** what the bot has said, so the loop protection and the reply
   limits have something to read.

It owns nothing else. The message row, the credit reservation, the variable
rendering, the compliance gate, the per-number send limits, the provider call
and the retry are all ``automation.send_automated_message`` ->
``messaging.send_message_task``, unchanged. A chatbot that talked to a provider
directly would have to reimplement all of that and would get the interesting
half of it wrong -- which is why ``ProviderAdapter`` appears nowhere in this
app.
"""

import logging
import random

from django.utils import timezone

from apps.messaging.models import Message
from apps.whatsapp.models import WhatsAppAccount
from core.buttons import ensure_ids

from .models import ChatbotEvent, ChatbotSession

logger = logging.getLogger("sendstack")

#: Most messages one inbound message may produce. A flow that chains
#: MESSAGE -> MESSAGE -> MESSAGE is legitimate ("here's the price list",
#: "here's the brochure", "anything else?"); a flow that produces twelve is a
#: loop somebody built by accident, and the recipient experiences it as spam
#: from a number they will report.
MAX_REPLIES_PER_TURN = 5

#: Longest delay the engine will actually wait. A response delay is meant to
#: stop a reply landing in the same instant as the question; a business that
#: types 600 into the box has misunderstood the field, and a ten-minute pause
#: before an automated answer is worse than none.
MAX_DELAY_SECONDS = 120


def delay_for(config, extra: float = 0.0) -> float:
    """
    How long to wait before this reply, in seconds.

    Drawn per message rather than fixed, because a bot that always answers in
    exactly 3.0 seconds reads more mechanical than one that answers instantly.

    ``extra`` is a flow ``DELAY`` node's own pause, added on top. It is a pause
    the author asked for explicitly, so it is *not* randomised -- but it is
    still subject to the same ceiling, because a worker is not going to hold a
    conversation open for ten minutes whatever the graph says.
    """
    low = max(int(getattr(config, "response_delay_min_seconds", 0) or 0), 0)
    high = max(int(getattr(config, "response_delay_max_seconds", 0) or 0), 0)
    if high < low:
        low, high = high, low
    high = min(high, MAX_DELAY_SECONDS)
    low = min(low, high)

    drawn = round(random.uniform(low, high), 2) if high > 0 else 0.0
    return min(drawn + max(float(extra), 0.0), MAX_DELAY_SECONDS)


def invalidate_pending(session) -> int:
    """
    Cancel every reply this session has scheduled but not yet sent.

    Bumping a counter rather than revoking a Celery task, because revocation in
    a broker is advisory at best: the task may already have been handed to a
    worker, and a worker that has it will run it. A token the task checks
    *before it sends* is the only version of this that actually holds.

    Returns the new token, so a caller scheduling a fresh reply in the same
    breath can carry it.
    """
    ChatbotSession.objects.filter(pk=session.pk).update(delivery_token=session.delivery_token + 1)
    session.delivery_token += 1
    return session.delivery_token


def account_problem(account) -> str:
    """
    Why this number cannot send right now, or "" when it can.

    Named rather than boolean because the whole point is that a bot which
    cannot speak must say why in the trail. "The bot did not reply" and "the
    bot could not reply because the phone was unlinked on Tuesday" are the same
    screen to a customer unless somebody writes the second one down.
    """
    if account is None:
        return "This chatbot has no WhatsApp number attached."
    if account.status != WhatsAppAccount.Status.CONNECTED:
        return f"The WhatsApp number '{account.name}' is {account.get_status_display().lower()}."
    if not account.user_id:
        return f"The WhatsApp number '{account.name}' has no owner to bill sends to."

    from apps.whatsapp.health import pause_state

    try:
        paused, reason = pause_state(account)
    except Exception:  # noqa: BLE001 - a health-check failure must not silence the bot
        logger.exception("Could not read the pause state for account %s.", account.pk)
        return ""
    if paused:
        return f"Sending from '{account.name}' is paused: {reason or 'paused by the platform'}."
    return ""


def send_now(ctx, spec: dict, *, reference: str) -> Message | None:
    """
    Hand one message to the existing send pipeline, immediately.

    Returns the queued ``Message``, or None when it could not be queued at all
    -- a disconnected number, no credits, or an empty message. Every one of
    those is recorded in the trail by the caller rather than swallowed.
    """
    from apps.automation.services import send_automated_message

    from . import variables

    if variables.is_simulation(ctx.session):
        # The one place a preview differs from a real turn, and deliberately
        # the *last* place: everything above this line -- the trigger, the
        # rules, the graph, the conditions, the AI call, the variables -- has
        # already run for real. A simulator that branched earlier than this
        # would be testing itself rather than the bot.
        return _capture_instead_of_sending(ctx, spec, reference=reference)

    problem = account_problem(ctx.account)
    if problem:
        ctx.trace.failed(ChatbotEvent.Step.SEND, problem, reference=reference)
        return None

    message = send_automated_message(
        ctx.account,
        ctx.contact,
        spec,
        conversation=ctx.conversation,
        reference=reference,
        # Always SERVICE. Everything a chatbot says is a reply to something the
        # customer just did, which is precisely the distinction
        # ``Message.Kind`` draws: it is still blocked for a contact who has
        # opted out, and it is never held until morning, because holding a
        # reply somebody is sitting there waiting for helps nobody.
        kind=Message.Kind.SERVICE,
    )
    if message is None:
        ctx.trace.failed(
            ChatbotEvent.Step.SEND,
            "The message could not be queued -- the number may be disconnected or out of credits.",
            reference=reference,
        )
        return None

    ctx.trace.step(
        ChatbotEvent.Step.SEND, "Handed to the send pipeline.",
        reference=reference, message_id=message.pk, status=message.status,
        message_type=message.message_type, has_buttons=bool(spec.get("buttons")),
    )
    _count_reply(ctx, message)
    return message


def deliver(ctx, spec: dict, *, reference: str) -> Message | None:
    """
    Say one thing, after the configured pause.

    With no delay configured this is ``send_now`` and nothing else, which is
    what every automated reply in this codebase did before chatbots existed.
    With a delay it schedules and returns None -- the caller has not failed,
    the message is simply not queued yet.
    """
    if ctx.replies_sent >= MAX_REPLIES_PER_TURN:
        ctx.trace.skipped(
            ChatbotEvent.Step.SEND,
            f"Stopped after {MAX_REPLIES_PER_TURN} replies to one message -- check this bot for a loop.",
            reference=reference,
        )
        return None

    # Read from the *published settings* rather than the live row, so a delay
    # somebody changed but has not republished does not take effect early --
    # every other behaviour on this path is frozen at publish and this must not
    # be the exception.
    config = getattr(ctx, "chatbot_settings", None) or ctx.chatbot
    # A flow DELAY node's pause is consumed here: it applies to the next thing
    # said and not to everything after it, which is what "pause, then say this"
    # means when you draw it.
    extra, ctx.extra_delay = getattr(ctx, "extra_delay", 0), 0
    seconds = delay_for(config, extra) if config is not None else 0.0

    from . import variables

    if variables.is_simulation(ctx.session):
        # A preview reports the pause rather than taking it. Scheduling the
        # real delayed task would queue work against a conversation that is
        # about to be rolled back, and making somebody watch three seconds of
        # nothing to check their own flow helps no one -- but the number is
        # still worth showing, because "why is my bot slow" starts here.
        if seconds > 0:
            ctx.trace.step(
                ChatbotEvent.Step.SEND, f"Would pause {seconds:.1f}s first.",
                reference=reference, simulated=True, delay_seconds=seconds,
            )
        return send_now(ctx, spec, reference=reference)

    if seconds <= 0:
        return send_now(ctx, spec, reference=reference)

    from .tasks import deliver_delayed_reply_task

    ctx.replies_sent += 1
    # The token is read, not bumped: this reply is *wanted*. Bumping here would
    # cancel the message queued one line earlier in the same multi-message
    # answer.
    deliver_delayed_reply_task.apply_async(
        kwargs={
            "session_id": ctx.session.pk,
            "token": ctx.session.delivery_token,
            "spec": spec,
            "reference": reference,
            "correlation_id": ctx.trace.correlation_id,
        },
        countdown=seconds,
    )
    ctx.trace.step(
        ChatbotEvent.Step.SEND, f"Scheduled in {seconds:.1f}s.",
        reference=reference, delay_seconds=seconds, delivery_token=ctx.session.delivery_token,
    )
    return None


def _capture_instead_of_sending(ctx, spec: dict, *, reference: str):
    """
    Record what the bot would have said, and say nothing.

    Written into the trail as an ordinary SEND step so the preview panel reads
    the same rows the debug panel does -- one renderer, and a preview that
    cannot show a message the real trail would not have recorded.

    Returns None, which every caller already handles: a delayed reply returns
    None too, so no call site needed changing to make previewing possible.
    """
    ctx.trace.step(
        ChatbotEvent.Step.SEND,
        "Would send this message.",
        reference=reference,
        simulated=True,
        body=spec.get("body") or "",
        buttons=[
            {"id": button.get("id", ""), "title": button.get("title", "")}
            for button in ensure_ids(spec.get("buttons") or [])
        ],
        media=[item.get("url", "") for item in spec.get("media_items") or []],
    )
    # Counted exactly as a real reply is, so the reply ceiling and the loop
    # protection behave in a preview the way they will in production. A
    # simulator with no limits would happily demonstrate a flow that gets cut
    # off in front of the first real customer.
    ctx.replies_sent += 1
    ctx.spoke = True
    ChatbotSession.objects.filter(pk=ctx.session.pk).update(
        bot_replies=ctx.session.bot_replies + 1, last_bot_message_at=timezone.now(),
    )
    ctx.session.bot_replies += 1
    return None


def _count_reply(ctx, message) -> None:
    """Record that the bot spoke, on the session and on the turn."""
    ctx.replies_sent += 1
    ctx.spoke = True
    ChatbotSession.objects.filter(pk=ctx.session.pk).update(
        bot_replies=ctx.session.bot_replies + 1, last_bot_message_at=timezone.now(),
    )
    ctx.session.bot_replies += 1
    ctx.session.last_bot_message_at = timezone.now()


def text_spec(body: str, *, buttons=None) -> dict:
    """
    The message spec for a plain (or button-bearing) text reply.

    Ids are filled in here rather than trusted from the action, because this is
    the last point at which the bot still knows what it is offering. A reply
    button goes out carrying an id and comes back carrying the same one, so a
    button sent without one is a button whose tap no BUTTON rule can match --
    a dead end the author would only discover from a customer.
    """
    return {
        "message_type": Message.MessageType.TEXT,
        "body": body,
        "media_url": "",
        "media_items": [],
        "caption": "",
        "filename": "",
        "variables": {},
        "buttons": ensure_ids(buttons),
        "template_id": None,
    }


def trim(text: str, limit: int) -> str:
    """
    Cut a reply to length without cutting a word -- or, better, a sentence.

    A model told to be brief occasionally is not, and a WhatsApp message that
    stops mid-word reads as a bug rather than as brevity. Preference order is
    sentence boundary, then word boundary, then a hard cut with an ellipsis.
    """
    if limit <= 0 or len(text) <= limit:
        return text

    window = text[:limit]
    for terminator in (". ", "! ", "? ", "\n"):
        cut = window.rfind(terminator)
        # Only accept a sentence end in the last third, or a two-sentence reply
        # gets trimmed back to one.
        if cut > limit * 0.6:
            return window[: cut + 1].strip()

    cut = window.rfind(" ")
    return (window[:cut] if cut > 0 else window).rstrip() + "..."
