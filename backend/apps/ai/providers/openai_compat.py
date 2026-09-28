"""
Translation between this package's Anthropic-shaped turns and the OpenAI
chat-completions shape -- the one every OpenAI-compatible endpoint speaks,
which by now is most of them: OpenRouter, Azure OpenAI, Groq, self-hosted
vLLM/Ollama, and any gateway modelled on the same API.

Shared by `openrouter` and `custom` rather than duplicated, because this is
the part that breaks a provider quietly: the voice agent's tool loop echoes an
assistant turn back verbatim and pairs every tool_use with a tool_result, and
two copies of this translation drifting apart is how that pairing breaks on
one vendor and not the other. See `base` for why there is one internal shape
at all, and `openrouter` for the fuller account of what has to survive the
round trip.
"""

import json
import logging

from .base import AIResponse, ToolCall

__all__ = ["EFFORT", "STOP_REASONS", "to_openai_messages", "to_openai_tools", "parse_response"]

logger = logging.getLogger("sendstack")

# The voice agent's latency dial, in the three steps this shape accepts. The
# two highest settings collapse into "high" rather than being dropped: a
# tenant who asked for more thinking should get the most an endpoint offers,
# not silently the default.
EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}

# OpenAI calls these something else. Mapped so that callers reading
# `stop_reason` see one vocabulary whichever provider answered.
STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


# ---------------------------------------------------------------------------
# Anthropic shape -> OpenAI shape
# ---------------------------------------------------------------------------

def _tool_result_turns(blocks) -> list:
    """The `role: tool` turns that answer the assistant turn just above."""
    turns = []
    for block in blocks:
        content = block.get("content")
        turns.append({
            "role": "tool",
            "tool_call_id": block.get("tool_use_id", ""),
            "content": content if isinstance(content, str) else json.dumps(content, default=str),
        })
    return turns


def _assistant_turn(text: str, tool_use_blocks) -> dict:
    turn = {"role": "assistant", "content": text or None}
    if tool_use_blocks:
        turn["tool_calls"] = [
            {
                "id": block.get("id", ""),
                "type": "function",
                # Arguments cross the wire as a JSON *string*, not an object.
                # Sending the object is a 400 from most endpoints.
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input") or {}, default=str),
                },
            }
            for block in tool_use_blocks
        ]
    return turn


def to_openai_messages(system, messages) -> list:
    """One conversation, retold in the shape this family of endpoints wants."""
    out = []
    if system:
        out.append({"role": "system", "content": system})

    for message in messages or []:
        role = message.get("role", "user")
        content = message.get("content")

        if not isinstance(content, list):
            out.append({"role": role, "content": content or ""})
            continue

        text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
        tool_use = [block for block in content if block.get("type") == "tool_use"]
        tool_results = [block for block in content if block.get("type") == "tool_result"]

        if role == "assistant":
            out.append(_assistant_turn(text, tool_use))
            continue

        # Tool results first: they answer the tool_calls on the assistant turn
        # immediately before them, and a user turn wedged in between breaks
        # that pairing.
        out.extend(_tool_result_turns(tool_results))
        if text.strip():
            out.append({"role": "user", "content": text})

    return out


def to_openai_tools(tools) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for tool in tools or []
    ]


# ---------------------------------------------------------------------------
# OpenAI shape -> Anthropic shape
# ---------------------------------------------------------------------------

def _arguments(raw) -> dict:
    """
    A tool call's arguments, decoded defensively.

    A model that emits malformed JSON here would otherwise take down the whole
    turn; an empty dict lets the tool refuse it and the agent still say
    something to the person waiting.
    """
    try:
        decoded = json.loads(raw or "{}")
    except (TypeError, ValueError):
        logger.warning("Provider returned tool arguments that were not JSON: %.200s", raw)
        return {}
    return decoded if isinstance(decoded, dict) else {}


def parse_response(data, latency_ms) -> AIResponse:
    choice = (data.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    text = (message.get("content") or "").strip()

    content = [{"type": "text", "text": text}] if text else []
    tool_calls = []
    for raw in message.get("tool_calls") or []:
        function = raw.get("function") or {}
        call = ToolCall(
            id=raw.get("id", ""),
            name=function.get("name", ""),
            input=_arguments(function.get("arguments")),
        )
        tool_calls.append(call)
        content.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.input})

    usage = data.get("usage") or {}
    finish = choice.get("finish_reason") or ""
    return AIResponse(
        text=text,
        input_tokens=usage.get("prompt_tokens", 0) or 0,
        output_tokens=usage.get("completion_tokens", 0) or 0,
        latency_ms=latency_ms,
        stop_reason=STOP_REASONS.get(finish, finish),
        tool_calls=tool_calls,
        content=content,
    )
