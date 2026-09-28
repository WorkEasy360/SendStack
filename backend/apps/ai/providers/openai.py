"""
OpenAI client -- billed direct, against api.openai.com or an Azure/gateway
mirror of it.

This is `custom` with opinions. The wire shape is identical (translated in
`openai_compat`, shared with `openrouter` and `custom`), so what is here is
only the three things a tenant should not have to know: the endpoint they are
almost certainly pointing at, which of their models will refuse `temperature`,
and the fact that a model past a certain generation renamed `max_tokens`.

Those last two are the reason this exists as a provider at all rather than
being one more Custom endpoint. OpenAI's reasoning models (o1/o3/o4, gpt-5)
reject a request carrying `temperature`, and reject one carrying `max_tokens`,
with a 400 that arrives hours later in a log while a customer waits. A tenant
who typed a model name into a settings box has no way to know that, and the
platform does.
"""

import logging
import time

import requests
from django.conf import settings

from core.outbound_urls import OutboundURLNotAllowed, check_outbound_url

from .base import AIProviderError, AIResponse
from .openai_compat import EFFORT, parse_response, to_openai_messages, to_openai_tools

__all__ = ["complete", "converse", "supports_sampling", "default_base_url", "default_model", "DEFAULT_BASE_URL"]

logger = logging.getLogger("sendstack")

#: Where OpenAI itself lives. A deployment pointed at Azure OpenAI or an
#: internal gateway overrides this with OPENAI_BASE_URL, and a single tenant
#: overrides it again with their own `base_url`.
DEFAULT_BASE_URL = "https://api.openai.com/v1"

#: Used only when neither the tenant nor the deployment named a model. Chosen
#: because it is the cheapest current model that can hold a support
#: conversation, which is the wrong thing to *silently* upgrade and the right
#: thing to fall back to.
FALLBACK_MODEL = "gpt-4o-mini"

#: Model families that refuse `temperature` and want `max_completion_tokens`
#: rather than `max_tokens`. Matched on prefix because the dated suffixes
#: ("o3-mini-2025-01-31") are endless and all behave the same way.
_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")

#: What may follow a family name and still be the same family. The dot is why
#: this is a set rather than a hyphen: a point release ("gpt-5.1") is the same
#: family as "gpt-5" and refuses `temperature` for the same reason, but it does
#: not start with "gpt-5-", so matching on the hyphen alone silently treats
#: every point release as an ordinary chat model. That is a 400 on the first
#: real customer, which is precisely the failure this module exists to prevent.
_VERSION_SEPARATORS = ("-", ".")


def _is_reasoning_model(model: str) -> bool:
    name = (model or "").strip().lower()
    return any(
        name == prefix or name.startswith(tuple(f"{prefix}{sep}" for sep in _VERSION_SEPARATORS))
        for prefix in _REASONING_PREFIXES
    )


def supports_sampling(model: str) -> bool:
    """
    Whether `temperature` may be sent to this model.

    False for the reasoning families, which return a 400 rather than ignoring
    it. Callers that ask first (the voice agent's settings page) can then say
    so in the UI instead of letting the tenant discover it mid-call.
    """
    return not _is_reasoning_model(model)


def default_base_url() -> str:
    """The endpoint a tenant who set nothing should reach."""
    return (getattr(settings, "OPENAI_BASE_URL", "") or DEFAULT_BASE_URL).strip().rstrip("/")


def default_model() -> str:
    """The model a tenant who set nothing should get, from OPENAI_MODEL."""
    return (getattr(settings, "OPENAI_MODEL", "") or FALLBACK_MODEL).strip()


def _endpoint(base_url: str) -> str:
    supplied = (base_url or "").strip().rstrip("/")
    # Only what a *tenant* typed is checked. `default_base_url()` comes from
    # OPENAI_BASE_URL, which is the operator's own choice -- a deployment
    # entitled to point at an internal gateway must not be refused by a rule
    # that exists to stop tenants reaching one. See core.outbound_urls.
    if supplied:
        try:
            check_outbound_url(supplied)
        except OutboundURLNotAllowed as exc:
            raise AIProviderError(str(exc)) from exc
    base = supplied or default_base_url()
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
        if response.status_code == 401:
            # The commonest way this fails in practice: a key from another
            # vendor pasted into a field set to OpenAI. The vendor's own 401
            # says "incorrect API key" and nothing about which vendor.
            detail = f"{detail} (this provider is set to OpenAI, so it expects a key beginning sk-)"
        elif response.status_code == 429:
            detail = f"{detail} (rate limit or quota -- check the billing on this OpenAI account)"
        raise AIProviderError(f"AI provider returned {response.status_code}: {detail}")

    try:
        data = response.json()
    except ValueError as exc:
        raise AIProviderError("AI provider returned a response that was not JSON.") from exc

    # A gateway sitting in front of OpenAI can report an upstream failure
    # inside a 200, the way OpenRouter does. Unhandled that is an empty reply,
    # which reads as the agent being broken rather than the account being out
    # of credit.
    error = data.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else error
        raise AIProviderError(f"AI provider returned an error: {message}")

    return data, latency_ms


def converse(*, api_key, model, system, messages, tools=None, max_tokens=400, temperature=None,
             effort=None, timeout=None, base_url=None) -> AIResponse:
    """One turn of conversation, optionally with tools. See `anthropic.converse`."""
    model = (model or "").strip() or default_model()
    reasoning = _is_reasoning_model(model)

    payload = {
        "model": model,
        "messages": to_openai_messages(system, messages),
    }
    # Same number, two spellings. The older models reject the new name and the
    # newer ones reject the old, so this is not a thing that can be sent both
    # ways to be safe.
    payload["max_completion_tokens" if reasoning else "max_tokens"] = max_tokens

    if tools:
        payload["tools"] = to_openai_tools(tools)
    if temperature is not None and not reasoning:
        payload["temperature"] = temperature
    if effort and reasoning:
        # Only the reasoning models have an effort dial; sending one to gpt-4o
        # is a 400 for an unrecognised parameter.
        payload["reasoning_effort"] = EFFORT.get(effort, "medium")

    data, latency_ms = _post(payload, api_key, base_url, timeout)
    return parse_response(data, latency_ms)


def complete(*, api_key, model, system, messages, max_tokens=400, temperature=0.2, timeout=None,
             base_url=None) -> AIResponse:
    """One turn of plain conversation, with no tools. See `converse`."""
    return converse(
        api_key=api_key, model=model, system=system, messages=messages,
        max_tokens=max_tokens, temperature=temperature, timeout=timeout, base_url=base_url,
    )
