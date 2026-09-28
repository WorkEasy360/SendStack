"""
The payment provider boundary.

Everything provider-specific lives behind this interface, and nothing above it
knows the word "Razorpay". That is not architectural decoration: a payment
provider is the single most likely third party in this system to be replaced
-- for pricing, for a new market, for a compliance requirement -- and a
codebase with a provider's SDK sprinkled through its views cannot be moved
without rewriting the views.

The contract is deliberately small. Five operations, and each returns a plain
dataclass rather than the provider's own object, so a caller never ends up
reading `response["razorpay_order_id"]` in a service.

**Two rules bind every implementation:**

1. **Never report a success the provider has not confirmed server-side.** A
   value that came from the browser is a claim, not a fact. `verify_payment`
   must check a signature or ask the provider directly, and must fail closed.
2. **Never let a secret out.** Not into a return value, not into a log line,
   not into an exception message. `public_config()` exists precisely so that
   the one credential the browser legitimately needs has an explicit, narrow
   way out and nothing else does.
"""

from dataclasses import dataclass, field


class PaymentError(Exception):
    """The provider could not do what was asked. Safe to show the customer."""


class PaymentNotConfigured(PaymentError):
    """
    This deployment has no working payment provider.

    Its own type because it is the one payment failure that is a *deployment*
    problem rather than a customer problem, and the two need different
    answers: "try again" versus "this deployment does not take payments yet".
    """


class SignatureInvalid(PaymentError):
    """A callback did not carry a signature this provider's secret produces."""


@dataclass
class CheckoutSession:
    """What the browser needs to open a hosted payment page, and nothing else."""

    provider_order_id: str
    amount_minor: int
    currency: str
    #: Everything the client-side checkout widget needs. Public values only --
    #: a publishable key id belongs here, a secret never does.
    public_params: dict = field(default_factory=dict)


@dataclass
class PaymentResult:
    """One payment, as this system understands it, whatever the provider called it."""

    provider_payment_id: str
    provider_order_id: str
    amount_minor: int
    currency: str
    #: True only when the provider has confirmed the money moved.
    captured: bool
    failure_reason: str = ""
    raw_status: str = ""


@dataclass
class WebhookResult:
    """One parsed, signature-checked webhook."""

    event_id: str
    event_type: str
    provider_order_id: str = ""
    payment: PaymentResult | None = None
    payload: dict = field(default_factory=dict)


class PaymentProvider:
    """
    What every adapter implements.

    A base class rather than a Protocol so that `is_configured` and the
    "unsupported operation" defaults live in one place: an adapter for a
    provider that has no concept of, say, a cancellable subscription should
    inherit a clear refusal rather than have to write one.
    """

    #: Short stable key, stored on every row this provider produces. Changing
    #: it would orphan existing payments, so it is part of the data model.
    name = "base"

    #: Whether this provider can take money at all. False for the manual
    #: provider, which exists to make "no gateway" an explicit, safe state
    #: rather than a crash.
    supports_checkout = False

    #: The webhook event types this adapter acts on. An **allow-list**, so a
    #: new event type the provider starts sending cannot begin driving
    #: behaviour nobody designed; anything else is recorded and ignored.
    #:
    #: Declared per adapter rather than known by the service, so that
    #: apps.billing.services never has to import a specific provider to find
    #: out what it handles.
    handled_events: tuple = ()

    def is_configured(self) -> bool:
        """Whether every credential this provider needs is present."""
        return False

    def public_config(self) -> dict:
        """
        The values the browser may see. **The only sanctioned way out for a
        credential**, and it must return publishable identifiers only.
        """
        return {}

    # -- Operations ---------------------------------------------------------

    def create_checkout(self, *, checkout) -> CheckoutSession:
        raise PaymentNotConfigured("This deployment cannot take payments.")

    def verify_payment(self, *, checkout, payload: dict) -> PaymentResult:
        """
        Confirm, server-side, that the payment named in `payload` really happened.

        `payload` comes from the browser. Nothing in it may be trusted: the
        implementation checks a provider signature over it, or asks the
        provider's API directly, and raises rather than guessing.
        """
        raise PaymentNotConfigured("This deployment cannot take payments.")

    def handle_webhook(self, *, body: bytes, headers) -> WebhookResult:
        """
        Verify a webhook's signature and parse it. Raises `SignatureInvalid`
        if the signature does not check out -- **before** any parsing that
        could act on the contents.
        """
        raise PaymentNotConfigured("This deployment cannot take payments.")

    def cancel_subscription(self, *, subscription) -> None:
        """
        Stop future charges at the provider.

        Default is a no-op rather than an error: a provider integration that
        charges one payment at a time has nothing recurring to cancel, and the
        local subscription record is cancelled by the caller either way.
        """
        return None
