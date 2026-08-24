"""
Provider abstraction for WhatsApp account connectivity and messaging.

No code outside this package (or ``connections/``) should know how a specific
WhatsApp integration works. Campaign, messaging, and inbox logic must talk to
a ``WhatsAppProvider`` instance obtained from ``providers.get_provider`` and
nothing else, so a new integration can be added later without touching those
apps.

Two connection shapes exist behind this one interface:
  - Cloud API: synchronous credential validation (``start_connection`` and
    ``get_connection_status`` both resolve immediately).
  - QR/device-linking: asynchronous pairing (``start_connection`` returns a
    pending session with a QR payload; the caller polls
    ``get_connection_status``/``get_qr`` and/or receives a
    ``parse_connection_webhook`` push once the phone confirms pairing).
``ConnectionManager`` (see ``connections/manager.py``) is the only caller of
the connection-lifecycle methods -- it owns translating provider results into
``WhatsAppAccount``/``WhatsAppConnectionSession`` state.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class ProviderError(Exception):
    """Raised when a provider call fails in a way callers must handle."""

    def __init__(self, message, *, code=None, ambiguous=False):
        super().__init__(message)
        self.message = message
        self.code = code
        #: True when the request may already have reached the provider, so we
        #: cannot tell whether the message went out. Callers must not retry an
        #: ambiguous send -- doing so risks delivering it twice.
        self.ambiguous = ambiguous


@dataclass
class ConnectionResult:
    connected: bool
    phone_number: str | None = None
    display_name: str | None = None
    error: str | None = None


@dataclass
class SendResult:
    success: bool
    provider_message_id: str | None = None
    error: str | None = None
    #: Set when the send failed in a way that leaves delivery unknown (e.g. the
    #: request timed out after it was already transmitted). Retrying such a
    #: send could deliver a duplicate, so messaging treats it as terminal --
    #: see apps/messaging/tasks.py.
    ambiguous: bool = False


@dataclass
class MessagePayload:
    to: str
    body: str | None = None
    media_type: str | None = None
    media_link: str | None = None
    caption: str | None = None
    filename: str | None = None
    template_name: str | None = None
    language_code: str | None = None
    template_components: list[Any] = field(default_factory=list)


class WebhookEventType:
    """
    Canonical ``WebhookEvent.event_type`` values.

    These strings are written straight into
    ``whatsapp.WhatsAppWebhookEvent.event_type`` and are then compared against
    ``WhatsAppWebhookEvent.EventType`` by the messaging and inbox receivers, so
    they MUST match that model's choices exactly. They previously did not --
    providers emitted lowercase names, every receiver's equality check failed,
    and inbound messages and delivery receipts were stored but never acted on.
    A test pins the two together; use these constants rather than literals.
    """

    MESSAGE_STATUS = "MESSAGE_STATUS"
    INBOUND_MESSAGE = "INBOUND_MESSAGE"


@dataclass
class WebhookEvent:
    event_type: str  # a WebhookEventType value
    provider_event_id: str
    raw: dict
    # Generic fields so downstream apps (messaging/inbox) never need to know
    # a specific provider's payload shape. Only set for MESSAGE_STATUS.
    message_id: str | None = None
    status: str | None = None


@dataclass
class ConnectionSessionResult:
    """Result of starting (or refreshing) a connection attempt."""

    status: str  # WhatsAppAccount.Status value: CONNECTING/QR_READY/CONNECTED/ERROR
    session_id: str | None = None
    qr_data: str | None = None  # ready-to-display data URL/string; only set when status == QR_READY
    qr_expires_at: datetime | None = None
    phone_number: str | None = None
    display_name: str | None = None
    provider_account_id: str | None = None
    error: str | None = None


@dataclass
class ConnectionStatusResult:
    """Result of polling an in-progress or completed connection attempt."""

    status: str
    qr_data: str | None = None
    qr_expires_at: datetime | None = None
    phone_number: str | None = None
    display_name: str | None = None
    provider_account_id: str | None = None
    error: str | None = None


@dataclass
class QRResult:
    qr_data: str
    expires_at: datetime | None = None


@dataclass
class ConnectionWebhookEvent:
    """A connection-lifecycle push from a provider (see qr_bridge.py)."""

    session_id: str
    status: str
    phone_number: str | None = None
    display_name: str | None = None
    provider_account_id: str | None = None
    error: str | None = None


class WhatsAppProvider(ABC):
    """Contract every WhatsApp integration must implement."""

    #: Whether this provider's connection method exchanges a QR/pairing code.
    #: Cloud API is False (credential-based); the QR bridge provider is True.
    supports_qr: bool = False

    #: Whether this provider can deliver a message with interactive buttons.
    #: Callers check this *before* sending rather than catching the
    #: ``not_supported`` error, so a message with buttons can degrade to
    #: readable text on a connection that has no interactive support instead
    #: of failing outright -- see apps/messaging/services.py.
    supports_interactive_buttons: bool = False

    @abstractmethod
    def start_connection(self, account) -> ConnectionSessionResult:
        """Begin (or restart) a connection attempt for this account."""

    @abstractmethod
    def get_connection_status(self, account, session_id: str | None) -> ConnectionStatusResult:
        """Poll the current state of an in-progress or completed connection attempt."""

    @abstractmethod
    def get_qr(self, account, session_id: str) -> QRResult:
        """Fetch the current QR payload for a pending session. Raise ProviderError(code="not_supported") if this provider has no QR step."""

    @abstractmethod
    def disconnect(self, account, session_id: str | None) -> None:
        ...

    @abstractmethod
    def get_account_info(self, account) -> ConnectionResult:
        """Confirm the account's stored credentials/session are usable right now."""

    @abstractmethod
    def send_text_message(self, account, to: str, body: str) -> SendResult:
        ...

    @abstractmethod
    def send_media_message(
        self,
        account,
        to: str,
        media_type: str,
        media_link: str,
        caption: str | None = None,
        filename: str | None = None,
    ) -> SendResult:
        ...

    def send_interactive_message(self, account, to: str, body: str, buttons: list) -> SendResult:
        """
        Send body text with tappable buttons under it (``core.buttons`` shape).

        Not abstract: a provider that cannot do this simply leaves
        ``supports_interactive_buttons`` False and inherits this refusal, so
        adding buttons never breaks an existing integration.

        Attachments are deliberately not part of this call. WhatsApp allows a
        media *header* on an interactive message, but a MultiSender message can
        carry several attachments and only one could ever be the header; the
        sender therefore delivers the attachments first and this message last,
        which also leaves the buttons at the bottom of the thread where they
        stay tappable.
        """
        raise ProviderError(
            "This connection cannot send messages with buttons.", code="not_supported"
        )

    @abstractmethod
    def send_template_message(
        self,
        account,
        to: str,
        template_name: str,
        language_code: str,
        components: list[Any] | None = None,
    ) -> SendResult:
        ...

    @abstractmethod
    def verify_webhook_challenge(self, account, query_params: dict) -> str | None:
        """Handle the provider's GET webhook-verification handshake."""

    @abstractmethod
    def validate_webhook_signature(self, account, raw_body: bytes, signature_header: str | None) -> bool:
        ...

    @abstractmethod
    def parse_webhook_payload(self, payload: dict) -> list[WebhookEvent]:
        ...

    def parse_connection_webhook(self, raw_body: bytes, signature_header: str | None) -> ConnectionWebhookEvent | None:
        """
        Verify and parse a provider-level connection-lifecycle webhook (see
        ``webhooks.py::WhatsAppConnectionWebhookView``). Only meaningful for
        providers with an asynchronous pairing step; the default returns
        None so providers that don't need it (Cloud API) need not override.
        """
        return None
