"""
One turn of the WhatsApp agent, with tools.

`services.answer` decides *whether* to speak and what the agent is told; this
decides what it says once it has been asked, and runs whatever it asks for on
the way. The split matters: everything in `services` about staying out of a
human's conversation, refusing when retrieval found nothing, and counting
replies is untouched by tool calling, and a tool loop that quietly bypassed
any of it would be a safety regression wearing a feature's clothes.

The loop is the voice agent's (`apps.voice.agent.run_turn`) with the phone
taken out. Same provider layer, same pairing rules, same repeat detection --
because the thing that breaks tool calling is never the happy path, it is a
tool_use left unanswered or an assistant turn not echoed back verbatim, and
having solved that once it should be solved the same way twice.

What is different is what a turn costs. On a call the budget is a carrier
that hangs up after fifteen seconds. Here nobody is holding a phone, so the
bound is money and sanity rather than milliseconds: a fixed number of rounds,
no repeats, and a hard stop that still produces an answer.
"""

import json
import logging
from dataclasses import dataclass, field

from django.conf import settings

from . import tools
from .providers.registry import converse, tool_result_block

logger = logging.getLogger("sendstack")

#: How many model round-trips one reply may take.
#:
#: One is a plain answer. Two is "look it up, then answer", which is the
#: common tool case. Three is "look it up, look up the customer, then answer".
#: Beyond that a model is usually going in circles, and each extra round is a
#: full re-send of the conversation at the tenant's expense.
DEFAULT_MAX_TOOL_ROUNDS = 4

#: Never honour a tenant-configured cap above this. The field exists so a
#: tenant can lower it; raising it to fifty would let one message bill fifty
#: requests.
MAX_TOOL_ROUNDS_CEILING = 8


@dataclass
class TurnResult:
    """What one turn produced."""

    text: str = ""
    #: "" or tools.HANDOFF -- the agent asked to be taken off this conversation.
    terminal: str = ""
    #: Why, when terminal is set. Recorded for the team, not shown to anyone.
    terminal_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    #: Model round-trips actually made.
    rounds: int = 0
    #: (name, result) per tool run, in order.
    tool_log: list = field(default_factory=list)
    #: Whether the loop stopped because it ran out of rounds.
    hit_cap: bool = False


def max_rounds(config) -> int:
    """The round cap for this tenant, clamped to something survivable."""
    try:
        configured = int(getattr(config, "max_tool_rounds", 0) or 0)
    except (TypeError, ValueError):
        configured = 0
    if configured <= 0:
        configured = DEFAULT_MAX_TOOL_ROUNDS
    return max(1, min(configured, MAX_TOOL_ROUNDS_CEILING))


def run_turn(config, context, system, history, *, max_tokens=None) -> TurnResult:
    """
    Ask the model, run what it asks for, and ask again until it answers.

    Raises `AIProviderError` if the model could not be reached; the caller
    decides what happens to the customer, exactly as it did before tools
    existed.

    `context` is a `tools.ToolContext` built from the conversation. It is the
    only thing any tool learns about ownership, and nothing the model returns
    can change it.
    """
    definitions = tools.definitions(config)
    settings_for_call = _resolved(config)
    cap = max_rounds(config)

    result = TurnResult()
    parts = []
    # Tool calls already run this turn, keyed on name plus arguments. A model
    # that re-asks for a tool because the result was not what it hoped for is
    # a known failure mode, and here it would mean a second CRM write and a
    # second message on somebody's phone.
    executed = {}

    for round_number in range(cap):
        response = converse(
            provider=config.provider,
            api_key=settings_for_call.api_key,
            model=settings_for_call.model,
            base_url=settings_for_call.base_url,
            system=system,
            messages=history,
            # An empty list is not the same as None: some endpoints reject
            # `tools: []`, and a tenant with tools switched off should be
            # making exactly the request they made before this file existed.
            tools=definitions or None,
            max_tokens=max_tokens or config.max_output_tokens,
            temperature=config.temperature,
        )
        result.rounds += 1
        result.input_tokens += response.input_tokens
        result.output_tokens += response.output_tokens
        result.latency_ms += response.latency_ms

        # Across rounds a model often restates its answer. On a phone that is
        # the same sentence twice out loud; here it is the same paragraph
        # twice in a chat bubble. Either way, once.
        if response.text and response.text not in parts:
            parts.append(response.text)

        if not response.tool_calls:
            break

        # The assistant turn goes back exactly as it came, tool_use blocks
        # included, or the tool_results below have nothing to pair with and
        # the next request is a 400.
        history.append({"role": "assistant", "content": response.content})

        results = []
        terminal = ""
        terminal_reason = ""
        repeats = 0

        for call in response.tool_calls:
            key = (call.name, json.dumps(call.input, sort_keys=True, default=str))
            if key in executed:
                # Answered from the first run. The block is still emitted --
                # an unanswered tool_use is a 400 on the very next request.
                repeats += 1
                results.append(tool_result_block(call, executed[key]))
                continue

            outcome = tools.execute(call.name, call.input, config, context)
            executed[key] = outcome.result
            result.tool_log.append((call.name, outcome.result))
            results.append(tool_result_block(call, outcome.result, is_error=outcome.is_error))

            if outcome.terminal and not terminal:
                terminal = outcome.terminal
                terminal_reason = outcome.detail

        if terminal:
            # The agent has asked to be taken off this conversation. Another
            # round would only produce something to say on the way out, and
            # the handoff message is the tenant's to write, not the model's.
            result.terminal = terminal
            result.terminal_reason = terminal_reason
            break

        if repeats == len(response.tool_calls):
            # Every tool this round had already run. The model is going round
            # in circles; another request adds cost and no information.
            logger.warning(
                "The WhatsApp agent repeated its tool calls on conversation %s; stopping the loop.",
                context.conversation_id,
            )
            break

        history.append({"role": "user", "content": results})
    else:
        # Ran out of rounds with the model still asking for tools. Whatever it
        # has said so far is what the customer gets -- which is why `parts` is
        # accumulated rather than replaced.
        result.hit_cap = True
        logger.warning(
            "The WhatsApp agent hit the %s-round tool cap on conversation %s.", cap, context.conversation_id,
        )

    result.text = "\n\n".join(part.strip() for part in parts if part.strip()).strip()
    return result


def _resolved(config):
    from .services import resolved

    return resolved(config)
