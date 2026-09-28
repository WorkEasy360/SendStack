"""
Anthropic Messages API client.

Written against `requests`, which the project already depends on, rather than
pulling in an SDK: the surface used here is one POST, and a vendored SDK would
be a new dependency to keep current for no behaviour this needs.

Two entry points sit on that one POST. `complete` is a plain question-and-answer
turn (the WhatsApp agent). `converse` adds tool use and the current
generation's thinking/effort controls, and is what the voice agent runs on --
a call needs the model to *do* things (hang up, transfer, save a detail) and
not merely say them.

The request and response shapes this module speaks are the package's internal
ones (see `base`), so there is nothing to translate here.
"""

import logging
import time

import requests
from django.conf import settings

# Re-exported: this module was the whole of the package before there was more
# than one provider, and callers still import the shared vocabulary from here.
from .base import AIProviderError, AIResponse, ToolCall, tool_result_block

__all__ = [
    "AIProviderError", "AIResponse", "ToolCall", "tool_result_block",
    "complete", "converse", "supports_adaptive_thinking", "supports_sampling",
]

logger = logging.getLogger("sendstack")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# Models that reject `temperature`/`top_p`/`top_k` with a 400 rather than
# ignoring them. Sampling controls were removed from the current generation
# when adaptive thinking replaced them, so sending the field every older model
# accepted is now the difference between an answer and an error. Matched by
# prefix because a tenant stores whatever model name they typed.
_NO_SAMPLING_PREFIXES = (
    "claude-fable-",
    "claude-mythos-",
    "claude-opus-5",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-5",
)

# Models that understand adaptive thinking and `output_config.effort`.
# Anything older errors on both, so neither is sent where it is not understood.
_ADAPTIVE_PREFIXES = _NO_SAMPLING_PREFIXES + ("claude-opus-4-6", "claude-sonnet-4-6")


def supports_sampling(model: str) -> bool:
    """Whether `temperature` may be sent to this model at all."""
    return not (model or "").startswith(_NO_SAMPLING_PREFIXES)


def supports_adaptive_thinking(model: str) -> bool:
    """Whether this model accepts `thinking: adaptive` and `output_config.effort`."""
    return (model or "").startswith(_ADAPTIVE_PREFIXES)


def _post(payload, api_key, timeout):
    if not api_key:
        raise AIProviderError("No API key is configured for the AI agent.")

    started = time.monotonic()
    try:
        response = requests.post(
            API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            json=payload,
            timeout=timeout or getattr(settings, "AI_REQUEST_TIMEOUT_SECONDS", 30),
        )
    except requests.RequestException as exc:
        raise AIProviderError(f"Could not reach the AI provider: {exc}") from exc

    latency_ms = int((time.monotonic() - started) * 1000)

    if response.status_code != 200:
        # The provider's own message is the useful half here (an expired key,
        # a rate limit, a model name that no longer exists), so it is passed
        # through rather than flattened into "AI failed".
        detail = ""
        try:
            detail = (response.json().get("error") or {}).get("message", "")
        except ValueError:
            detail = response.text[:200]
        if response.status_code == 401:
            # The single most common cause is a key from another vendor pasted
            # into this field, and "invalid x-api-key" does not say so.
            detail = f"{detail} (this provider is set to Anthropic, so it expects a key beginning sk-ant-)"
        raise AIProviderError(f"AI provider returned {response.status_code}: {detail}")

    try:
        return response.json(), latency_ms
    except ValueError as exc:
        raise AIProviderError("AI provider returned a response that was not JSON.") from exc


def _parse(data, latency_ms) -> AIResponse:
    content = data.get("content") or []
    text = "".join(block.get("text", "") for block in content if block.get("type") == "text").strip()
    tool_calls = [
        ToolCall(id=block.get("id", ""), name=block.get("name", ""), input=block.get("input") or {})
        for block in content
        if block.get("type") == "tool_use"
    ]
    usage = data.get("usage") or {}
    return AIResponse(
        text=text,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        latency_ms=latency_ms,
        stop_reason=data.get("stop_reason", ""),
        tool_calls=tool_calls,
        content=content,
    )


def converse(*, api_key, model, system, messages, tools=None, max_tokens=400, temperature=None,
             effort=None, timeout=None, base_url=None) -> AIResponse:
    """
    One turn of conversation, optionally with tools the model may call.

    `messages` is the Anthropic shape -- a list of {"role", "content"} with
    roles alternating and starting at "user". Callers build it rather than
    passing raw text so that a real conversation reaches the model as a
    conversation, which is what makes follow-up questions work at all.

    `temperature` and `effort` are each dropped when the configured model does
    not accept them, so changing the model on the settings page can never turn
    a working agent into a 400.

    `base_url` is accepted and ignored: Anthropic's endpoint is fixed, and the
    registry passes this uniformly across providers so callers never have to
    know which ones use it.
    """
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = tools

    if temperature is not None and supports_sampling(model):
        payload["temperature"] = temperature

    if supports_adaptive_thinking(model):
        # Thinking stays on. Turning it off on the current generation makes the
        # model occasionally narrate a tool call as plain text instead of
        # emitting a tool_use block -- on a phone call that means the agent
        # says "let me transfer you" and then never transfers. Lowering effort
        # is the supported way to buy the latency back instead.
        payload["thinking"] = {"type": "adaptive"}
        if effort:
            payload["output_config"] = {"effort": effort}

    data, latency_ms = _post(payload, api_key, timeout)
    return _parse(data, latency_ms)


def complete(*, api_key, model, system, messages, max_tokens=400, temperature=0.2, timeout=None,
             base_url=None) -> AIResponse:
    """One turn of plain conversation, with no tools. See `converse`."""
    return converse(
        api_key=api_key, model=model, system=system, messages=messages,
        max_tokens=max_tokens, temperature=temperature, timeout=timeout,
    )
