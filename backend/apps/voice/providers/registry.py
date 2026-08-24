"""
Which carrier a tenant's calls go through.

Provider instances are stateless -- credentials live on the tenant's
``VoiceAgentConfig`` and are passed into every call -- so one instance per
carrier is shared by every tenant, and adding a carrier is one entry here plus
one module next to it.
"""

from .base import NotConfiguredProvider, VoiceProvider, VoiceProviderError
from .twilio import TwilioVoiceProvider

_PROVIDERS: dict[str, VoiceProvider] = {
    "TWILIO": TwilioVoiceProvider(),
}


def get_provider(config) -> VoiceProvider:
    """
    The provider for a tenant's voice agent.

    Missing credentials come back as ``NotConfiguredProvider`` rather than an
    exception, so a settings page can render the agent's state without a
    try/except and a webhook can still reject an unsigned request.
    """
    provider = _PROVIDERS.get(config.provider)
    if provider is None:
        raise VoiceProviderError(f"No voice provider registered for '{config.provider}'.", code="unknown_provider")
    if not config.has_carrier_credentials:
        return NotConfiguredProvider(
            "The voice agent has no carrier credentials. Add them on the Voice Agent page.",
            code="not_configured",
        )
    return provider


def get_provider_by_name(name: str) -> VoiceProvider:
    provider = _PROVIDERS.get(name)
    if provider is None:
        raise VoiceProviderError(f"No voice provider registered for '{name}'.", code="unknown_provider")
    return provider
