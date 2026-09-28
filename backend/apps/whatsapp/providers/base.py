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
    #: The provider's own error code, carried through from ProviderError.code
    #: ("network_error", "timeout", "invalid_recipient", ...). It exists so
    #: that messaging can classify a failure without reading the sentence in
    #: `error` -- see Message.FailureReason. Blank when the provider had no
    #: code to give, which is honest: it means "failed", not "failed for no
    #: reason".
    code: str = ""


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
    # The correlation id we handed the provider at send time (the send
    # attempt's idempotency key), echoed back on the status event. It is how a
    # delivery receipt is tied to the right message when the provider's own
    # message id never reached us -- e.g. the worker died before persisting it.
    # See apps/messaging/receivers.py.
    biz_opaque_callback_data: str | None = None


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


@dataclass
class NumberLookup:
    """
    One phone number's WhatsApp registration -- the number filter's answer.

    ``phone`` is echoed back exactly as the caller passed it, so a task can
    match answers to its own rows without re-deriving whatever the provider
    did to the number on the wire.
    """

    phone: str
    exists: bool
    jid: str | None = None


@dataclass
class GroupSummary:
    """One group the connected number belongs to, without its member list."""

    id: str
    subject: str
    member_count: int = 0
    #: Only admins may post.
    announce: bool = False
    is_community: bool = False
    created_at: datetime | None = None


@dataclass
class GroupMember:
    #: E.164, with the "+", matching how contacts are stored.
    phone: str
    jid: str = ""
    name: str = ""
    is_admin: bool = False


@dataclass
class GroupMembers:
    id: str
    subject: str
    members: list[GroupMember] = field(default_factory=list)
    #: Members WhatsApp exposed only by an anonymous id, with no phone number
    #: to import. Reported rather than dropped so the screen can say so.
    hidden_count: int = 0


class WhatsAppProvider(ABC):
    """Contract every WhatsApp integration must implement."""

    #: Whether this provider's connection method exchanges a QR/pairing code.
    #: Cloud API is False (credential-based); the QR bridge provider is True.
    supports_qr: bool = False

    # -- Capabilities -------------------------------------------------------
    #
    # What a message composed on this connection will actually *arrive* as.
    #
    # Every flag below states what reaches a phone, not what the API accepts.
    # That distinction is the whole reason this block exists: a linked device
    # will accept an interactive message, return a message id and report it
    # delivered, and render it to the recipient as body text with the buttons
    # missing (see WhatsAppQRBridgeProvider.supports_interactive_buttons for
    # the measurement). A capability that describes the API rather than the
    # phone is worse than none, because every layer above reports success.
    #
    # Callers check these *before* sending rather than catching a
    # ``not_supported`` error, so unsupported content can degrade to readable
    # text instead of failing outright -- see apps/messaging/services.py --
    # and so the composer can say what will happen before anything is saved.

    #: Text, and attachments. No provider here lacks either; they are declared
    #: anyway so that the capability dict is a complete answer rather than one
    #: that silently omits the things that happen to be universal today.
    supports_text: bool = True
    supports_media: bool = True

    #: Up to three tappable reply buttons whose id comes back on a tap.
    supports_interactive_buttons: bool = False
    #: One tappable button that opens a link.
    supports_url_buttons: bool = False
    #: One tappable button that dials a number. False everywhere at present,
    #: and not an oversight -- Meta offers a call button only inside an
    #: approved template, never on a free-form send. See core.buttons.
    supports_call_buttons: bool = False
    #: A tappable menu (``interactive.type = "list"``).
    supports_list_messages: bool = False
    #: A native poll the recipient votes in.
    supports_poll_messages: bool = False

    #: Whether the connection can ask WhatsApp which numbers are registered
    #: (the number filter). Only a linked device can: the Cloud API has no
    #: contact-lookup endpoint at any version, by design.
    supports_number_check: bool = False
    #: Whether the connection can list the groups its number belongs to and
    #: read their members (the group grabber). Groups are a phone-app feature
    #: with no Cloud API counterpart.
    supports_group_listing: bool = False

    @classmethod
    def capabilities(cls) -> dict:
        """
        Everything this provider can and cannot deliver, as one dict.

        The API hands this to the composer so a message type that cannot
        arrive is labelled before it is built, rather than being discovered
        when a campaign reports a thousand sends that nobody could answer.
        """
        return {
            "text": cls.supports_text,
            "media": cls.supports_media,
            "buttons": cls.supports_interactive_buttons,
            "url_buttons": cls.supports_url_buttons,
            "call_buttons": cls.supports_call_buttons,
            "lists": cls.supports_list_messages,
            "polls": cls.supports_poll_messages,
            "number_check": cls.supports_number_check,
            "groups": cls.supports_group_listing,
        }

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

    # `operation_id` is the send attempt's idempotency key (see
    # apps/messaging/leasing.py). Providers that support idempotent submission
    # or an operation lookup use it -- the QR bridge as ``send_operation_id``,
    # the Cloud API as ``biz_opaque_callback_data``. The default "" means "no
    # idempotency", which is the historical behaviour and is what inbox/
    # automation sends still pass.

    @abstractmethod
    def send_text_message(self, account, to: str, body: str, operation_id: str = "") -> SendResult:
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
        operation_id: str = "",
    ) -> SendResult:
        ...

    def get_send_operation_status(self, account, operation_id: str) -> dict | None:
        """
        Ask the provider what became of one send, by its operation id.

        Used by the recovery sweep when a worker died after calling the
        provider but before recording the result. Returns:

        * ``None`` -- this provider cannot answer (no lookup, no idempotent
          submission). The caller must treat the send as UNKNOWN.
        * ``{"unreachable": True}`` -- the provider exists but could not be
          reached right now.
        * ``{"known": bool, "sent": bool, "provider_message_id": str | None,
          "ambiguous": bool}`` -- what the provider knows.

        Default: not supported.
        """
        return None

    def send_interactive_message(self, account, to: str, body: str, buttons: list,
                                 operation_id: str = "") -> SendResult:
        """
        Send body text with tappable buttons under it (``core.buttons`` shape).

        Not abstract: a provider that cannot do this simply leaves
        ``supports_interactive_buttons`` False and inherits this refusal, so
        adding buttons never breaks an existing integration.

        Attachments are deliberately not part of this call. WhatsApp allows a
        media *header* on an interactive message, but a SendStack message can
        carry several attachments and only one could ever be the header; the
        sender therefore delivers the attachments first and this message last,
        which also leaves the buttons at the bottom of the thread where they
        stay tappable.
        """
        raise ProviderError(
            "This connection cannot send messages with buttons.", code="not_supported"
        )

    def send_list_message(self, account, to: str, body: str, list_content: dict,
                          operation_id: str = "") -> SendResult:
        """
        Send body text with a tappable menu under it (``core.interactive`` shape).

        Not abstract, for the same reason ``send_interactive_message`` is not:
        a provider that cannot do this leaves ``supports_list_messages`` False
        and inherits this refusal, so adding lists never breaks an existing
        integration.

        Attachments are handled the same way as for buttons -- sent first, as
        their own messages, with the menu last so it stays at the bottom of the
        thread where it can still be opened.
        """
        raise ProviderError(
            "This connection cannot send list messages.", code="not_supported"
        )

    def send_poll_message(self, account, to: str, poll_content: dict, operation_id: str = "") -> SendResult:
        """
        Send a poll the recipient votes in (``core.interactive`` shape).

        A poll is its own bubble and carries no body text: the question is the
        message. Callers with body text send it as a separate message first.
        """
        raise ProviderError(
            "This connection cannot send polls.", code="not_supported"
        )

    @abstractmethod
    def send_template_message(
        self,
        account,
        to: str,
        template_name: str,
        language_code: str,
        components: list[Any] | None = None,
        operation_id: str = "",
    ) -> SendResult:
        ...

    # -- Lookups (the number filter and the group grabber) ------------------
    #
    # Not abstract, for the same reason the interactive sends are not: a
    # provider that cannot do these leaves the two ``supports_*`` flags above
    # False and inherits the refusal. Callers check the flag first, so the
    # refusal is a safety net rather than the way "no" is normally answered.

    def check_numbers(self, account, phones: list[str]) -> list[NumberLookup]:
        """
        Which of ``phones`` are registered on WhatsApp, one answer per input.

        The answer list is the same length and order as ``phones``; a number
        the provider could not even ask about comes back ``exists=False``.
        """
        raise ProviderError("This connection cannot check numbers.", code="not_supported")

    def list_groups(self, account) -> list[GroupSummary]:
        """Every group the connected number is a member of."""
        raise ProviderError("This connection cannot list groups.", code="not_supported")

    def get_group_members(self, account, group_ids: list[str]) -> list[GroupMembers]:
        """The members of the named groups, with a phone number wherever WhatsApp exposes one."""
        raise ProviderError("This connection cannot read group members.", code="not_supported")

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
