"""
OpenRouter client -- one key, many vendors' models.

OpenRouter speaks the OpenAI chat-completions shape; the translation to and
from this package's Anthropic-shaped turns lives in `openai_compat`, shared
with `custom` since it is the same shape both endpoints speak. What is
specific to OpenRouter and lives here instead: its fixed URL, its own
attribution header, and the 401 hint that names *this* vendor's key prefix.
"""

import logging
import time

import requests
from django.conf import settings

from .base import AIProviderError, AIResponse
from .openai_compat import EFFORT, parse_response, to_openai_messages, to_openai_tools

__all__ = ["complete", "converse", "supports_sampling", "to_openai_messages", "to_openai_tools"]

logger = logging.getLogger("sendstack")

API_URL = "https://openrouter.ai/api/v1/chat/completions"

# OpenRouter shows this next to the request in the account's activity log. It
# costs nothing, and it turns "something spent this" into an answer.
APP_TITLE = "SendStack"


def supports_sampling(model: str) -> bool:
    """
    Whether `temperature` may be sent.

    Always, here. OpenRouter drops parameters the routed model does not
    understand instead of rejecting the request, which is the behaviour the
    Anthropic client has to hand-maintain a prefix list to get.
    """
    return True


def _post(payload, api_key, timeout):
    if not api_key:
        raise AIProviderError("No API key is configured for the AI agent.")

    started = time.monotonic()
    try:
        response = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
                "X-Title": APP_TITLE,
            },
            json=payload,
            timeout=timeout or getattr(settings, "AI_REQUEST_TIMEOUT_SECONDS", 30),
        )
    except requests.RequestException as exc:
        raise AIProviderError(f"Could not reach the AI provider: {exc}") from exc

    latency_ms = int((time.monotonic() - started) * 1000)

    if response.status_code != 200:
        detail = ""
        try:
            detail = (response.json().get("error") or {}).get("message", "")
        except ValueError:
            detail = response.text[:200]
        if response.status_code == 401:
            # The same trap in the other direction: an sk-ant- key sent here
            # is rejected with nothing that explains why.
            detail = f"{detail} (this provider is set to OpenRouter, so it expects a key beginning sk-or-)"
        raise AIProviderError(f"AI provider returned {response.status_code}: {detail}")

    try:
        data = response.json()
    except ValueError as exc:
        raise AIProviderError("AI provider returned a response that was not JSON.") from exc

    # OpenRouter reports an upstream vendor's failure inside a 200, because the
    # request to *OpenRouter* did succeed. Unhandled, that arrives as an empty
    # answer and the agent says nothing at all -- which reads to the tenant as
    # the feature being broken rather than the model being out of credit.
    error = data.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else error
        raise AIProviderError(f"AI provider returned an error: {message}")

    return data, latency_ms


def converse(*, api_key, model, system, messages, tools=None, max_tokens=400, temperature=None,
             effort=None, timeout=None, base_url=None) -> AIResponse:
    """
    One turn of conversation, optionally with tools. See `anthropic.converse`.

    `base_url` is accepted and ignored: OpenRouter's endpoint is fixed, and
    the registry passes this uniformly across providers so callers never have
    to know which ones use it.
    """
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": to_openai_messages(system, messages),
    }
    if tools:
        payload["tools"] = to_openai_tools(tools)
    if temperature is not None:
        payload["temperature"] = temperature
    if effort:
        payload["reasoning"] = {"effort": EFFORT.get(effort, "medium")}

    data, latency_ms = _post(payload, api_key, timeout)
    return parse_response(data, latency_ms)


def complete(*, api_key, model, system, messages, max_tokens=400, temperature=0.2, timeout=None,
             base_url=None) -> AIResponse:
    """One turn of plain conversation, with no tools. See `converse`."""
    return converse(
        api_key=api_key, model=model, system=system, messages=messages,
        max_tokens=max_tokens, temperature=temperature, timeout=timeout,
    )
