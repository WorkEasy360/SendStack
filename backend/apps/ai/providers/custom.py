"""
Custom OpenAI-compatible endpoint client -- for anything not on OpenRouter:
a self-hosted model behind vLLM or Ollama, Azure OpenAI, Groq, an internal
gateway. Same OpenAI chat-completions shape OpenRouter speaks (translated in
`openai_compat`), just posted to whatever URL the tenant configured instead
of a fixed one.

The difference from every other client in this package: there is no single
vendor to have opinions about here, so nothing assumes a key prefix, a model
naming scheme, or which parameters the endpoint understands beyond what it is
sent. The tenant's own base URL is the only thing that says where this goes.
"""

import logging
import time

import requests
from django.conf import settings

from core.outbound_urls import OutboundURLNotAllowed, check_outbound_url

from .base import AIProviderError, AIResponse
from .openai_compat import EFFORT, parse_response, to_openai_messages, to_openai_tools

__all__ = ["complete", "converse", "supports_sampling"]

logger = logging.getLogger("sendstack")


def supports_sampling(model: str) -> bool:
    """
    Whether `temperature` may be sent.

    Always -- whether a self-hosted or third-party endpoint accepts it is
    between the tenant and whatever they pointed this at, not something this
    package can know in advance for an address it has never seen.
    """
    return True


def _endpoint(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise AIProviderError(
            "No base URL is configured for this AI provider. Set one on the agent's settings page "
            "-- for example https://api.groq.com/openai/v1 or http://localhost:11434/v1."
        )
    # Re-checked here and not only in the serializer. This is the last point
    # before the socket opens, so it is the check that covers a row saved
    # before the rule existed and a hostname whose DNS answer changed after it
    # was saved. See core.outbound_urls.
    try:
        check_outbound_url(base)
    except OutboundURLNotAllowed as exc:
        raise AIProviderError(str(exc)) from exc
    return f"{base}/chat/completions"


def _post(payload, api_key, base_url, timeout):
    if not api_key:
        raise AIProviderError("No API key is configured for the AI agent.")
    url = _endpoint(base_url)

    started = time.monotonic()
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
            json=payload,
            timeout=timeout or getattr(settings, "AI_REQUEST_TIMEOUT_SECONDS", 30),
        )
    except requests.RequestException as exc:
        raise AIProviderError(f"Could not reach {url}: {exc}") from exc

    latency_ms = int((time.monotonic() - started) * 1000)

    if response.status_code != 200:
        detail = ""
        try:
            detail = (response.json().get("error") or {}).get("message", "")
        except ValueError:
            detail = response.text[:200]
        raise AIProviderError(f"AI provider returned {response.status_code}: {detail}")

    try:
        data = response.json()
    except ValueError as exc:
        raise AIProviderError("AI provider returned a response that was not JSON.") from exc

    # Some OpenAI-compatible gateways report an upstream failure inside a 200,
    # the way OpenRouter does. Unhandled, that is an empty reply and an agent
    # that says nothing at all.
    error = data.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else error
        raise AIProviderError(f"AI provider returned an error: {message}")

    return data, latency_ms


def converse(*, api_key, model, system, messages, tools=None, max_tokens=400, temperature=None,
             effort=None, timeout=None, base_url=None) -> AIResponse:
    """One turn of conversation, optionally with tools. See `anthropic.converse`."""
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

    data, latency_ms = _post(payload, api_key, base_url, timeout)
    return parse_response(data, latency_ms)


def complete(*, api_key, model, system, messages, max_tokens=400, temperature=0.2, timeout=None,
             base_url=None) -> AIResponse:
    """One turn of plain conversation, with no tools. See `converse`."""
    return converse(
        api_key=api_key, model=model, system=system, messages=messages,
        max_tokens=max_tokens, temperature=temperature, timeout=timeout, base_url=base_url,
    )
