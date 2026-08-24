"""
What the calling agent is told, and how one turn of a call is produced.

The prompt in this file is doing more work than it looks like. Everything a
text agent can get away with -- a bulleted list, a markdown link, a paragraph
of caveats, an em-dash -- is actively wrong when a speech synthesiser reads it
out to somebody holding a phone. So the rules here are about *speech*: short
turns, spoken numbers, no formatting characters, and one question at a time.

The other constraint is the clock. The carrier drops a call whose webhook has
not answered in fifteen seconds, so a turn is not allowed to think for as long
as it likes: the model runs at low effort by default, the tool loop is capped,
and the whole turn works to a deadline it checks between rounds.
"""

import json
import logging
import time
from dataclasses import dataclass, field

from django.conf import settings

from apps.ai import knowledge
from apps.ai.providers.anthropic import AIProviderError, converse, tool_result_block

from . import tools

logger = logging.getLogger("multisender")

# How many model round-trips one turn of conversation may take. One is the
# normal case (answer). Two covers "look it up, then answer". Beyond that the
# person on the phone is listening to silence.
MAX_TOOL_ROUNDS = 3

_INSTRUCTIONS = """You are {name}, speaking with someone on a live phone call on behalf of a business.

Everything you say is read out loud by a speech synthesiser. That governs how you write:
- One or two sentences per turn. This is a conversation, not a statement.
- Plain spoken words only. No markdown, no bullet points, no headings, no emoji, no asterisks,
  no brackets, no URLs. If you need to give a link or a long reference, send it on WhatsApp instead.
- Write numbers, prices, dates and times the way a person says them out loud: "two thousand rupees",
  "half past four", "the fourteenth of March".
- Ask one question at a time, then stop and let them answer.
- Speak the language they speak. If they switch, switch with them.

How to behave:
- You are talking to a real person whose time you are taking. Get to the point.
- Never invent a price, a date, an availability, an order status, a refund or a policy.
  If you do not know, say you will find out, or look it up with the search_knowledge tool.
- If they ask you to stop calling, apologise, confirm you will not call again, and end the call.
- If they sound like they have misheard you, say it again more simply rather than louder.
- When something needs doing, use the tool. Saying "I have booked that for you" without calling the
  tool is a lie the business has to apologise for later."""

_SPEECH_NOTE = """
What they say reaches you through a speech recogniser over a phone line, so it will contain
mistakes: wrong homophones, missing words, mangled names and numbers. Read through the errors
where the meaning is obvious. Where it is not -- especially for a number, a name or an email
address -- read it back to them and ask if you have it right, rather than guessing."""

_KNOWLEDGE_ONLY = """
Answer only from the reference material you are given. If it does not cover something, say you
will check and have someone follow up, rather than guessing."""


@dataclass
class TurnResult:
    """One turn's worth of decisions."""

    speech: str = ""
    #: "", tools.END_CALL or tools.TRANSFER.
    terminal: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    #: Tool calls made this turn, as (name, result) for the transcript.
    tool_log: list = field(default_factory=list)


def api_key(config) -> str | None:
    return config.get_api_key() or getattr(settings, "AI_API_KEY", "") or None


def build_system_prompt(config, session, chunks) -> str:
    parts = [_INSTRUCTIONS.format(name=config.display_name or "the assistant"), _SPEECH_NOTE]

    if config.business_context.strip():
        parts.append(f"\nAbout this business:\n{config.business_context.strip()}")

    goal = (session.goal or config.default_goal or "").strip()
    if goal:
        parts.append(f"\nWhy you are on this call:\n{goal}")

    if session.contact is not None and session.contact.name:
        parts.append(f"\nYou are speaking to {session.contact.name}.")

    if session.context:
        # Facts the business handed over for this specific call -- an order
        # number, an amount due. Rendered plainly so the model quotes them
        # rather than reconstructing them.
        facts = "\n".join(f"- {key}: {value}" for key, value in session.context.items())
        parts.append(f"\nWhat you have been told about this call:\n{facts}")

    if config.answer_from_knowledge_only:
        parts.append(_KNOWLEDGE_ONLY)

    if chunks:
        references = "\n\n".join(f"[{chunk.document.title}]\n{chunk.content}" for chunk in chunks)
        parts.append(f"\nReference material:\n{references}")

    return "\n".join(parts)


def build_history(session) -> list:
    """
    The call so far, in the shape the API expects.

    Rebuilt from the transcript on every turn rather than carried between
    webhooks: the turns are already in the database because they have to be
    (a call that drops must still leave a transcript), and a carrier retrying
    a webhook must not be able to produce two different conversations.

    The transcript is the *only* source. What the caller just said is already
    in it -- the engine records it before asking the model anything -- so
    taking it as a second argument here is how it ends up in the prompt twice.
    """
    from .models import CallTurn

    history = []
    spoken = (
        CallTurn.objects.filter(session=session, role__in=(CallTurn.Role.AGENT, CallTurn.Role.CALLER))
        .order_by("ordinal", "id")
    )
    for turn in spoken:
        text = (turn.text or "").strip()
        if not text:
            continue
        role = "assistant" if turn.role == CallTurn.Role.AGENT else "user"
        # The API rejects two turns in a row from the same role, which happens
        # here whenever somebody answers a question in two breaths.
        if history and history[-1]["role"] == role:
            history[-1]["content"] += f" {text}"
        else:
            history.append({"role": role, "content": text})

    # A conversation must start with the other party, or the model is being
    # asked to continue something it never said. The agent's greeting is the
    # first thing on any outbound call, so this is the normal case, not an
    # edge one.
    while history and history[0]["role"] == "assistant":
        history.pop(0)
    return history


def run_turn(config, session, caller_text: str) -> TurnResult:
    """
    Produce what the agent says next, running any tools it asks for.

    Raises AIProviderError if the model could not be reached at all; the
    caller decides what to say to the person still holding the phone.
    """
    # `caller_text` is the retrieval query only -- it is already in the
    # transcript, which is what `build_history` reads.
    chunks = knowledge.search(session.tenant_id, caller_text, limit=config.knowledge_chunks) if caller_text else []
    system = build_system_prompt(config, session, chunks)
    messages = build_history(session)
    if not messages:
        # Nothing has been said yet by either side -- the opening turn of a
        # call the agent did not greet. Give the model something to answer.
        messages = [{"role": "user", "content": "(the call has just connected)"}]

    definitions = tools.definitions(config)
    budget = getattr(settings, "VOICE_TURN_BUDGET_SECONDS", 11)
    deadline = time.monotonic() + budget

    result = TurnResult()
    spoken_parts = []
    # Tool calls already run this turn, keyed by name and arguments. A model
    # that loops -- asking for the same tool again because the result did not
    # look like what it expected -- is a known failure mode, and here it would
    # mean a second WhatsApp message, a second booked callback, and the same
    # sentence read out twice down the phone.
    executed = {}

    for round_number in range(MAX_TOOL_ROUNDS):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            logger.warning("Voice turn on call %s ran out of budget after %s rounds.", session.pk, round_number)
            break

        response = converse(
            api_key=api_key(config),
            model=config.model,
            system=system,
            messages=messages,
            tools=definitions,
            max_tokens=config.max_output_tokens,
            temperature=config.temperature,
            effort=config.effort or None,
            timeout=remaining,
        )
        result.input_tokens += response.input_tokens
        result.output_tokens += response.output_tokens
        result.latency_ms += response.latency_ms

        # Repeated text is dropped rather than queued twice: across tool
        # rounds the model often restates its answer, and the caller would
        # hear the same sentence back to back.
        if response.text and response.text not in spoken_parts:
            spoken_parts.append(response.text)

        if not response.tool_calls:
            break

        # The assistant turn has to go back exactly as it came, tool_use
        # blocks included, or the tool_results have nothing to pair with.
        messages.append({"role": "assistant", "content": response.content})

        results = []
        terminal = ""
        farewell = ""
        repeats = 0
        for call in response.tool_calls:
            key = (call.name, json.dumps(call.input, sort_keys=True, default=str))
            if key in executed:
                # Answered from the first run. The block is still emitted --
                # an unanswered tool_use is a 400 on the next request, which
                # mid-call is a dropped call.
                repeats += 1
                results.append(tool_result_block(call, executed[key]))
                continue

            outcome = tools.execute(call.name, call.input, config, session)
            executed[key] = outcome.result
            result.tool_log.append((call.name, outcome.result))
            results.append(tool_result_block(call, outcome.result, is_error=outcome.is_error))
            if outcome.terminal and not terminal:
                terminal = outcome.terminal
                farewell = outcome.farewell

        if terminal:
            # Nothing more to ask the model: the call is ending or moving. The
            # farewell is spoken instead of another round-trip, which is both
            # faster and the only way to be sure something is said before the
            # line goes.
            if farewell:
                spoken_parts.append(farewell)
            result.terminal = terminal
            break

        if repeats == len(response.tool_calls):
            # Every tool this round was one already run: the model is going
            # round in circles and another request would only add silence.
            logger.warning("Voice turn on call %s repeated its tool calls; stopping the loop.", session.pk)
            break

        messages.append({"role": "user", "content": results})
    else:
        logger.warning("Voice turn on call %s hit the tool-round cap.", session.pk)

    result.speech = " ".join(part.strip() for part in spoken_parts if part.strip()).strip()
    return result


def summarise(config, session) -> str:
    """
    A few lines somebody can read instead of the whole transcript.

    Runs after the call, on the worker, so it is the one place in this app
    that may take as long as it needs and use the tenant's full model.
    """
    from .models import CallTurn

    turns = CallTurn.objects.filter(
        session=session, role__in=(CallTurn.Role.AGENT, CallTurn.Role.CALLER),
    ).order_by("ordinal", "id")

    lines = [
        f"{'Agent' if turn.role == CallTurn.Role.AGENT else 'Caller'}: {turn.text.strip()}"
        for turn in turns
        if (turn.text or "").strip()
    ]
    if not lines:
        return ""

    system = (
        "Summarise this phone call for somebody who was not on it. Cover what the call was about, what "
        "the person said they wanted, what was agreed, and anything still outstanding. Three or four "
        "short lines of plain text, no markdown. Do not invent anything that is not in the transcript."
    )
    try:
        response = converse(
            api_key=api_key(config),
            model=config.model,
            system=system,
            messages=[{"role": "user", "content": "\n".join(lines)}],
            max_tokens=300,
            temperature=config.temperature,
            effort=config.effort or None,
        )
    except AIProviderError as exc:
        logger.warning("Could not summarise call %s: %s", session.pk, exc)
        return ""
    return (response.text or "").strip()
