from django.conf import settings

from .base import ProviderError, WhatsAppProvider
from .cloud_api import WhatsAppCloudAPIProvider
from .qr_bridge import NotConfiguredProvider, WhatsAppQRBridgeProvider


def _build_qr_provider() -> WhatsAppProvider:
    if not settings.QR_PROVIDER_BASE_URL:
        return NotConfiguredProvider()
    if not settings.QR_PROVIDER_API_KEY or not settings.QR_PROVIDER_WEBHOOK_SECRET:
        # A base URL alone isn't enough to talk to the bridge -- and the
        # bridge itself refuses to start without its own copies of these
        # (qr-bridge/src/config.ts), so calls would otherwise fail as an
        # opaque "network_error" instead of naming the real problem.
        return NotConfiguredProvider(
            "QR provider is missing its API key/webhook secret configuration.",
            code="configuration_error",
        )
    return WhatsAppQRBridgeProvider(
        base_url=settings.QR_PROVIDER_BASE_URL,
        api_key=settings.QR_PROVIDER_API_KEY,
        webhook_secret=settings.QR_PROVIDER_WEBHOOK_SECRET,
    )


_PROVIDERS: dict[str, WhatsAppProvider] = {
    "CLOUD_API": WhatsAppCloudAPIProvider(),
    "QR_PROVIDER": _build_qr_provider(),
}


def get_provider(account) -> WhatsAppProvider:
    provider = _PROVIDERS.get(account.provider)
    if provider is None:
        raise ProviderError(f"No provider registered for '{account.provider}'.", code="unknown_provider")
    return provider


def get_provider_by_name(name: str) -> WhatsAppProvider:
    provider = _PROVIDERS.get(name)
    if provider is None:
        raise ProviderError(f"No provider registered for '{name}'.", code="unknown_provider")
    return provider


def provider_capabilities() -> dict:
    """
    Every registered provider's capabilities, keyed by provider name.

    The composer needs this as well as the per-account answer: somebody with
    no Cloud API number has to be told that a list *would* work on one, rather
    than that lists do not work -- the first is a purchase decision and the
    second is wrong.
    """
    return {name: provider.capabilities() for name, provider in _PROVIDERS.items()}
