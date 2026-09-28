"""
The vocabulary every AI provider in this package speaks.

There is one internal request shape and one internal response shape, and both
are Anthropic's: a list of ``{"role", "content"}`` turns whose content is
either a string or a list of ``text``/``tool_use``/``tool_result`` blocks. That
choice is not a preference for Anthropic, it is a decision to have a single
shape at all -- the voice agent's tool loop echoes an assistant turn back
verbatim and pairs every ``tool_use`` with a ``tool_result``, and rewriting
that loop per provider is how the pairing quietly breaks on one of them.

A provider that speaks something else translates at its own edge (see
``openrouter``), so nothing above this package ever learns which vendor
answered.
"""

import json

__all__ = ["AIProviderError", "AIResponse", "ToolCall", "tool_result_block"]


class AIProviderError(Exception):
    """Raised when the model could not be reached or refused the request."""


class ToolCall:
    """One tool the model asked for, already decoded."""

    __slots__ = ("id", "name", "input")

    def __init__(self, id, name, input):  # noqa: A002 - mirrors the API's own field names
        self.id = id
        self.name = name
        self.input = input

    def __repr__(self):
        return f"ToolCall({self.name!r}, {self.input!r})"


class AIResponse:
    __slots__ = ("text", "input_tokens", "output_tokens", "latency_ms", "stop_reason", "tool_calls", "content")

    def __init__(self, text, input_tokens=0, output_tokens=0, latency_ms=0, stop_reason="",
                 tool_calls=None, content=None):
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.stop_reason = stop_reason
        #: Tool calls the model made this turn, in the order it made them.
        self.tool_calls = tool_calls or []
        #: The raw content blocks, to be echoed back as the assistant turn when
        #: continuing a tool-use exchange. Rebuilding them from `.text` would
        #: drop the tool_use blocks and break the pairing the API requires
        #: between a tool_use and its tool_result.
        self.content = content or []


def tool_result_block(tool_call, result, is_error=False) -> dict:
    """
    The user-turn block that answers one tool_use.

    Every tool_use in an assistant turn must be answered, including the ones
    that failed -- an unanswered call is a 400 on the next request, which on a
    live call is a dropped call.
    """
    block = {
        "type": "tool_result",
        "tool_use_id": tool_call.id,
        "content": result if isinstance(result, str) else json.dumps(result),
    }
    if is_error:
        block["is_error"] = True
    return block
