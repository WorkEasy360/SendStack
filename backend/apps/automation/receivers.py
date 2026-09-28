import logging

from django.dispatch import receiver

from apps.inbox.signals import inbound_message_recorded

from . import services
from .services import TriggerEvent

logger = logging.getLogger("sendstack")


@receiver(inbound_message_recorded)
def evaluate_automation_rules(sender, message, is_new_conversation, **kwargs):
    """
    Decide what, if anything, answers this inbound message.

    Everything that can speak is tried in one ordered ladder and the first
    thing that claims the message ends it. That single rule is what keeps a
    contact from being answered twice for one message -- a keyword rule and a
    keyword-triggered workflow both firing would send two replies seconds
    apart, which reads as a broken bot to the recipient and as spam to
    WhatsApp.

    The order is deliberate, most specific first:

      0. consent      -- an opt-out request answers itself and stops the rest
      0.5 chatbot     -- a published chatbot on this number owns the message
      1. welcome      -- first message on this conversation
      2. button rule  -> button workflow    (an unambiguous choice)
      3. keyword rule -> keyword workflow   (something the tenant wrote down)
      4. inbound workflow                   (a catch-all sequence)
      5. the AI agent                       (a guess, so it goes last)

    Within each pair the single-shot rule wins over the workflow: it is the
    cheaper, more predictable thing, and a tenant who wants the sequence
    instead simply does not write the rule.

    **The chatbot rung.** A published chatbot subsumes rungs 1-5: it has its
    own welcome message, its own priority-ordered rules, its own flow and its
    own AI, and letting the old ladder also run would answer one message twice.
    So when a bot owns the number it takes the message and the rest is skipped.
    A tenant migrating from the old rules can set `defer_to_automation` on the
    bot, which restores the original order and gives the bot only what nothing
    below it claimed -- so an existing workspace can adopt a chatbot without its
    working automations going quiet on the day it publishes one.

    A workspace with no chatbot reaches none of this: `chatbot_claims` is one
    indexed query returning None, and the ladder below runs exactly as it
    always has.
    """
    from apps.compliance.services import handle_inbound as handle_consent

    # Consent comes first and, when it applies, instead of everything else.
    # Someone who has just typed STOP must not get a welcome message, a
    # keyword auto-reply, or the next step of a workflow -- answering an
    # unsubscribe request with marketing is the single worst thing this
    # platform could do, and it is exactly what firing the rules below on
    # the word "stop" would produce. The only thing they get back is the
    # confirmation, which compliance sends itself.
    if handle_consent(message):
        return

    # Before the ladder rather than inside it: this does not answer anything,
    # it decides *what the message is*. On a QR-linked number a question goes
    # out as a numbered list, so the contact's choice arrives as the text "1"
    # rather than as a tap -- and every rung below matches on
    # `replied_button_id`. Resolving here means one ladder serves both kinds of
    # connection, and neither the rules nor the workflows learn that buttons
    # can arrive two ways.
    outcome = services.resolve_option_reply(message)
    if outcome == "UNMATCHED":
        # They were clearly choosing, and it was not on the list. Read it back
        # rather than letting a stray number fall through to a keyword rule
        # that happens to match it.
        if services.reprompt_options(message):
            return

    # A published chatbot on this number answers instead of everything below,
    # unless it has been told to let the old automations go first. Queued
    # rather than run inline -- see `chatbot_claims`.
    claim = _chatbot_claim(message)
    if claim == "TAKES":
        return

    handled = False

    if is_new_conversation:
        handled = services.trigger_welcome_message(message)
        if not handled:
            handled = services.start_workflows_for(message, TriggerEvent.NEW_CONVERSATION)

    if not handled:
        handled = services.trigger_button_rules(message)
    if not handled:
        handled = services.start_workflows_for(message, TriggerEvent.BUTTON)

    if not handled:
        handled = services.trigger_keyword_rules(message)
    if not handled:
        handled = services.start_workflows_for(message, TriggerEvent.KEYWORD)

    if not handled:
        handled = services.start_workflows_for(message, TriggerEvent.INBOUND_MESSAGE)

    # A deferring chatbot gets what the old automations did not want, in the
    # position the tenant-wide AI agent would otherwise occupy. It is one or
    # the other: a bot with AI enabled and the tenant agent both answering
    # would be two replies to one question.
    if not handled and claim == "DEFERS":
        _queue_chatbot(message)
        services.resume_runs_awaiting_button(message)
        return

    # The AI agent answers last and only what nothing else did. A keyword rule
    # is an answer the tenant wrote down on purpose; the agent guessing over
    # the top of it would be both slower and less correct.
    if not handled:
        from apps.ai.services import maybe_reply

        maybe_reply(message)

    # A tap can also be the thing a workflow is parked waiting for. This is
    # not part of the ladder: it resumes a sequence already under way rather
    # than answering the message, so it runs whoever else spoke.
    services.resume_runs_awaiting_button(message)


def _chatbot_claim(message) -> str:
    """
    Whether a chatbot owns this message: ``"TAKES"``, ``"DEFERS"`` or ``""``.

    ``"TAKES"`` means the work has already been queued and this ladder must
    stop. ``"DEFERS"`` means the bot wants whatever the old rules leave.

    Imported inside the function on purpose. ``apps.automation`` loads before
    ``apps.chatbot``, and a module-level import would make this app's readiness
    depend on one that reaches back into it. A deployment that has removed the
    chatbot app entirely keeps working: the ImportError is caught and the
    ladder is exactly what it was before this existed.
    """
    try:
        from apps.chatbot import services as chatbot_services
    except ImportError:
        return ""

    try:
        chatbot = chatbot_services.live_chatbot_for(message.whatsapp_account)
    except Exception:  # noqa: BLE001 - a chatbot lookup must never cost the old ladder
        logger.exception("Could not look up a chatbot for account %s.", message.whatsapp_account_id)
        return ""

    if chatbot is None:
        return ""
    if getattr(chatbot, "defer_to_automation", False):
        return "DEFERS"

    _queue_chatbot(message)
    return "TAKES"


def _queue_chatbot(message) -> None:
    """
    Hand the message to the chatbot worker.

    Queued, never run inline: a model call takes seconds and the provider's
    webhook is holding a connection open waiting for a 200. Everything the
    engine needs is on the message row, so nothing has to be carried across.
    """
    try:
        from apps.chatbot.tasks import process_inbound_message_task

        process_inbound_message_task.delay(message.pk)
    except Exception:  # noqa: BLE001 - a broker blip must not 500 the webhook
        logger.exception("Could not queue chatbot processing for message %s.", message.pk)
