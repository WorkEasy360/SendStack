"""
Which model provider a tenant's agent talks to.

Client modules are stateless -- the key comes from the tenant's config and is
passed into every call -- so there is nothing to instantiate, and adding a
provider is one entry here plus one module next to it. Same arrangement as
``apps.voice.providers.registry`` and ``apps.whatsapp.providers.registry``.

Callers import `complete`/`converse` from here rather than from a vendor
module, and pass `provider=config.provider`. The provider defaults to
Anthropic so that a caller with nothing configured behaves the way this
package did when Anthropic was the only option.
"""

from . import anthropic, custom, openai, openrouter
from .base import AIProviderError, AIResponse, ToolCall, tool_result_block

__all__ = [
    "AIProviderError", "AIResponse", "ToolCall", "tool_result_block",
    "ANTHROPIC", "OPENAI", "OPENROUTER", "CUSTOM", "complete", "converse", "get_client",
    "base_url_problem", "model_name_problem", "supports_sampling", "default_model_for",
    "Credentials", "credentials",
]

ANTHROPIC = "ANTHROPIC"
OPENAI = "OPENAI"
OPENROUTER = "OPENROUTER"
CUSTOM = "CUSTOM"

# Kept as plain strings rather than importing the model's TextChoices, because
# `apps.voice.models` needs the same list and neither app should have to
# import the other's models to reach a provider client.
CHOICES = (
    (ANTHROPIC, "Anthropic (Claude)"),
    (OPENAI, "OpenAI"),
    (OPENROUTER, "OpenRouter"),
    (CUSTOM, "Custom (OpenAI-compatible)"),
)

_CLIENTS = {
    ANTHROPIC: anthropic,
    OPENAI: openai,
    OPENROUTER: openrouter,
    CUSTOM: custom,
}


def get_client(provider: str = ANTHROPIC):
    """
    The client module for a provider name.

    An unknown name raises rather than falling back to a default: a tenant
    whose provider was renamed out from under them should be told, not quietly
    billed at another vendor with a key that cannot work there anyway.
    """
    client = _CLIENTS.get(provider or ANTHROPIC)
    if client is None:
        raise AIProviderError(f"No AI provider is registered for '{provider}'.")
    return client


def converse(*, provider: str = ANTHROPIC, **kwargs) -> AIResponse:
    """One turn of conversation, with tools. See `anthropic.converse`."""
    return get_client(provider).converse(**kwargs)


def complete(*, provider: str = ANTHROPIC, **kwargs) -> AIResponse:
    """One turn of plain conversation. See `anthropic.complete`."""
    return get_client(provider).complete(**kwargs)


def supports_sampling(provider: str, model: str) -> bool:
    """Whether `temperature` may be sent to this provider's model at all."""
    return get_client(provider).supports_sampling(model)


def model_name_problem(provider: str, model: str) -> str:
    """
    Why this model name cannot work with this provider, or "" if it can.

    Only the vendor prefix is checked, because that is the mistake a settings
    page actually produces: somebody switches provider and leaves the model
    box alone. The two vocabularies are not compatible in either direction --
    Anthropic has never heard of `anthropic/claude-sonnet-4.5`, and OpenRouter
    needs the prefix to know who to route to -- and both failures arrive as a
    404 from the vendor hours later, in a log, when a customer is waiting.

    A custom endpoint has no vocabulary this package can know in advance --
    it could be anything from "gpt-4o" to a fine-tune's internal name -- so
    nothing is checked there beyond what `base_url_problem` covers.

    Whether the model *exists* is left to the vendor: keeping a list of every
    model on OpenRouter current is not a thing this codebase can win at, and
    guessing wrong would block a model that works.
    """
    model = (model or "").strip()
    if not model:
        return ""
    if provider == OPENROUTER and "/" not in model:
        return (
            "OpenRouter model names are written vendor/model -- for example "
            "anthropic/claude-sonnet-4.5, openai/gpt-4o-mini or google/gemini-2.0-flash-001."
        )
    if provider == ANTHROPIC and "/" in model:
        return (
            "Anthropic model names have no vendor prefix -- for example claude-sonnet-5. "
            f"Choose OpenRouter as the provider if you meant to route {model}."
        )
    if provider == OPENAI and "/" in model:
        return (
            "OpenAI model names have no vendor prefix -- for example gpt-4o-mini, not openai/gpt-4o-mini. "
            f"Choose OpenRouter as the provider if you meant to route {model}."
        )
    return ""


class Credentials:
    """What one call to a provider actually goes out with."""

    __slots__ = ("api_key", "model", "base_url")

    def __init__(self, api_key, model, base_url):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    def __repr__(self):  # pragma: no cover - a key must never reach a log
        return f"Credentials(model={self.model!r}, base_url={self.base_url!r}, api_key={'set' if self.api_key else 'unset'})"


def credentials(provider: str, *, api_key="", model="", base_url="") -> Credentials:
    """
    Resolve one agent's stored settings against the deployment's environment.

    Both agents -- the WhatsApp one and the calling one -- store a key, a model
    and a base URL per tenant, and both may leave any of them blank. This is
    the single place that decides what blank means, so the two cannot drift
    into answering it differently.

    The order is always **tenant first, environment second**. A tenant who
    pasted their own key is billed to their own account whatever the
    environment says; a tenant who did not falls through to the deployment's,
    which is how a single-tenant install configures this once in `.env` and
    never opens the settings page at all.

    OpenAI is the only provider with an environment fallback for the model and
    the endpoint as well as the key, because it is the only one the deployment
    configures by environment (OPENAI_MODEL, OPENAI_BASE_URL). For the others
    a blank model is a blank model and the caller's own field is authoritative.
    """
    from django.conf import settings

    key = (api_key or "").strip()
    resolved_model = (model or "").strip()
    resolved_base_url = (base_url or "").strip()

    if provider == OPENAI:
        key = key or (getattr(settings, "OPENAI_API_KEY", "") or "").strip()
        resolved_model = resolved_model or openai.default_model()
        resolved_base_url = resolved_base_url or openai.default_base_url()

    key = key or (getattr(settings, "AI_API_KEY", "") or "").strip()
    return Credentials(key or None, resolved_model, resolved_base_url)


def default_model_for(provider: str) -> str:
    """
    The model a tenant who named none should get, where the provider has one.

    Only OpenAI does, because only OpenAI is configured deployment-wide by
    environment (OPENAI_MODEL) -- the requirement being that the model in use
    is whatever that variable says, not a constant compiled into a settings
    page months ago. Every other provider returns "", meaning "whatever the
    config field already holds".
    """
    if provider == OPENAI:
        return openai.default_model()
    return ""


def base_url_problem(provider: str, base_url: str) -> str:
    """
    Why this base URL cannot work, or "" if it can.

    Only the Custom provider has one to check: Anthropic and OpenRouter each
    have a fixed endpoint, so a base URL there is unused rather than wrong.
    Custom has nothing to send requests to without one, and finding that out
    only when a customer is waiting for a reply is the failure this catches
    on the way in instead.

    The second check is where it points. A base URL is the one field in this
    product that turns a tenant's typing into an outbound request from this
    server, so it is held to core.outbound_urls' rules -- refused if it
    addresses this deployment's own network unless the operator has said that
    host is fine. Applied to any provider that carries one, not only Custom:
    a URL that is unused today is a URL somebody wires up tomorrow.
    """
    from core.outbound_urls import outbound_url_problem

    trimmed = (base_url or "").strip()
    if provider == CUSTOM and not trimmed:
        return (
            "A custom provider needs the OpenAI-compatible endpoint's base URL -- for example "
            "https://api.groq.com/openai/v1 or http://localhost:11434/v1."
        )
    if trimmed:
        return outbound_url_problem(trimmed)
    return ""
