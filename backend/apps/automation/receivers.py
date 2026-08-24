import logging

from django.dispatch import receiver

from apps.inbox.signals import inbound_message_recorded

from . import services
from .services import TriggerEvent

logger = logging.getLogger("multisender")


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
      1. welcome      -- first message on this conversation
      2. button rule  -> button workflow    (an unambiguous choice)
      3. keyword rule -> keyword workflow   (something the tenant wrote down)
      4. inbound workflow                   (a catch-all sequence)
      5. the AI agent                       (a guess, so it goes last)

    Within each pair the single-shot rule wins over the workflow: it is the
    cheaper, more predictable thing, and a tenant who wants the sequence
    instead simply does not write the rule.
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
