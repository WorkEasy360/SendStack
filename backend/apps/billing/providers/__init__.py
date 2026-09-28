"""
Resolving the payment provider this deployment actually has.

`get_provider()` is the only way anything gets one, and it is deliberately
strict about the difference between three states that are easy to blur:

* **Payments switched off** -- `PAYMENTS_ENABLED` is false. The product shows
  no checkout and says plans are assigned by the operator.
* **Payments on, provider unconfigured** -- somebody set the flag and not the
  credentials. This raises `PaymentNotConfigured`, which surfaces as
  "payments are not available right now", and **never** as a successful
  checkout. Section 107 of the phase brief has a name for the alternative:
  fake success.
* **Payments on and working** -- a live adapter.

The manual provider is not a stub or a mock. It is the honest representation
of a deployment that takes money outside this system -- a reseller invoicing
their customers directly -- where an operator assigns the plan and no checkout
should ever be offered.
"""

import logging

from django.conf import settings

from .base import (  # noqa: F401 - re-exported: this is the package's public surface
    CheckoutSession,
    PaymentError,
    PaymentNotConfigured,
    PaymentProvider,
    PaymentResult,
    SignatureInvalid,
    WebhookResult,
)

logger = logging.getLogger("sendstack")


class ManualProvider(PaymentProvider):
    """
    No gateway. Plans are assigned by an operator; nothing here takes money.

    Every operation inherits the base class's refusal, which is the point: a
    deployment in this mode cannot accidentally produce a checkout, and a
    caller that tries gets an exception with a sentence in it rather than an
    empty session that the frontend renders as a broken pay button.
    """

    name = "manual"
    supports_checkout = False

    def is_configured(self) -> bool:
        # True: "no gateway" is a complete, valid configuration. It is
        # `supports_checkout` that stops a payment being attempted, not a
        # pretence that something is missing.
        return True

    def public_config(self) -> dict:
        return {"provider": self.name}


def _adapters() -> dict:
    from .razorpay import RazorpayProvider

    return {ManualProvider.name: ManualProvider, RazorpayProvider.name: RazorpayProvider}


def payments_enabled() -> bool:
    """
    Whether this deployment can take a payment right now.

    Both halves: the switch *and* a provider that supports checkout *and* has
    its credentials. Any one of the three missing means no, because a "yes"
    that turns into an exception at the checkout button is worse than a
    straight no on the billing page.
    """
    if not getattr(settings, "PAYMENTS_ENABLED", False):
        return False
    try:
        provider = get_provider()
    except PaymentNotConfigured:
        return False
    return provider.supports_checkout and provider.is_configured()


def get_provider(name: str = None) -> PaymentProvider:
    """
    The configured adapter.

    Raises `PaymentNotConfigured` for an unknown name rather than silently
    falling back to the manual provider: a typo in ``PAYMENT_PROVIDER`` that
    quietly disabled payments would be discovered by a customer, not by the
    operator who made it.
    """
    name = (name or getattr(settings, "PAYMENT_PROVIDER", "manual") or "manual").strip().lower()
    adapter = _adapters().get(name)
    if adapter is None:
        logger.error("PAYMENT_PROVIDER names an unknown provider: %r.", name)
        raise PaymentNotConfigured("This deployment's payment provider is not recognised.")
    return adapter()


def require_live_provider() -> PaymentProvider:
    """
    A provider that can actually take money, or a refusal explaining why not.

    The single gate every payment-taking path goes through, so there is one
    place that decides -- and one message a customer can be shown.
    """
    if not getattr(settings, "PAYMENTS_ENABLED", False):
        raise PaymentNotConfigured(
            "Online payment is not available on this deployment. Contact us to arrange a plan."
        )
    provider = get_provider()
    if not provider.supports_checkout:
        raise PaymentNotConfigured(
            "Online payment is not available on this deployment. Contact us to arrange a plan."
        )
    if not provider.is_configured():
        # Deliberately does not say *which* credential is missing. This
        # message reaches a customer's browser.
        logger.error("PAYMENTS_ENABLED is on but the %s provider has no credentials.", provider.name)
        raise PaymentNotConfigured("Payments are not available right now. Please try again later.")
    return provider
