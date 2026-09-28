"""
The event vocabulary, the payload shapes, and the signature developers verify.

Events are how an integration finds out what happened without polling. They
are emitted from `receivers.py`, which listens to the signals the existing apps
already send -- this module never reaches into a provider, and never parses a
WhatsApp payload. That matters most for button replies: `message.received` and
`button.clicked` are both built from a `messaging.Message` row that the
existing QR-bridge inbound path has *already* parsed and resolved, so there is
exactly one button parser in this codebase and it is not this one.
"""

import hashlib
import hmac
import logging
import uuid

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("sendstack")


# --- Event types ---------------------------------------------------------

MESSAGE_SENT = "message.sent"
MESSAGE_DELIVERED = "message.delivered"
MESSAGE_READ = "message.read"
MESSAGE_FAILED = "message.failed"
MESSAGE_RECEIVED = "message.received"
BUTTON_CLICKED = "button.clicked"
CAMPAIGN_STARTED = "campaign.started"
CAMPAIGN_COMPLETED = "campaign.completed"
CAMPAIGN_FAILED = "campaign.failed"
ACCOUNT_CONNECTED = "account.connected"
ACCOUNT_DISCONNECTED = "account.disconnected"

EVENT_TYPES = {
    MESSAGE_SENT: "An outbound message was accepted by the provider.",
    MESSAGE_DELIVERED: "An outbound message reached the recipient's device.",
    MESSAGE_READ: "The recipient opened an outbound message.",
    MESSAGE_FAILED: "An outbound message could not be delivered.",
    MESSAGE_RECEIVED: "A contact sent an inbound message.",
    BUTTON_CLICKED: "A contact chose one of the options offered on a message.",
    CAMPAIGN_STARTED: "A campaign began dispatching.",
    CAMPAIGN_COMPLETED: "A campaign finished; every recipient reached a terminal state.",
    CAMPAIGN_FAILED: "A campaign stopped without completing.",
    ACCOUNT_CONNECTED: "A WhatsApp account became available for sending.",
    ACCOUNT_DISCONNECTED: "A WhatsApp account stopped being available for sending.",
}

ALL_EVENT_TYPES = tuple(EVENT_TYPES)


# --- Signing -------------------------------------------------------------
#
# Header names are branded so a receiver handling several vendors' webhooks can
# tell whose is whose without inspecting the body.

SIGNATURE_HEADER = "X-SendStack-Signature"
TIMESTAMP_HEADER = "X-SendStack-Timestamp"
EVENT_HEADER = "X-SendStack-Event"
DELIVERY_HEADER = "X-SendStack-Delivery"


def signature(payload_bytes: bytes, secret: str, timestamp: int) -> str:
    """
    ``sha256=<hex>`` over ``"<timestamp>.<body>"``.

    The timestamp is **inside** the signed string, not merely sent alongside
    it. If it were only a header, an attacker replaying a captured delivery
    could rewrite it to `now` and the signature would still verify -- which
    would make the replay window below decorative. Signing it binds the body
    to the moment it was signed.

    The ``sha256=`` prefix is there so the algorithm can change one day without
    the verification code having to guess which one it is looking at.
    """
    signed = f"{timestamp}.".encode("utf-8") + payload_bytes
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(payload_bytes: bytes, secret: str, timestamp, presented: str, *, tolerance_seconds: int = 300) -> bool:
    """
    The reference implementation of what a receiver should do.

    Not used in the send path -- it exists so the documentation can show
    verification that is known to match the signer exactly, and so a test can
    prove the two agree rather than proving the signer agrees with itself.
    """
    try:
        timestamp = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(timezone.now().timestamp()) - timestamp) > tolerance_seconds:
        return False
    return hmac.compare_digest(signature(payload_bytes, secret, timestamp), presented or "")


def build(event_type: str, data: dict) -> dict:
    """
    The envelope every webhook body uses.

    `id` is stable across retries of the same event, which is what lets a
    receiver deduplicate: this platform guarantees at-least-once delivery, and
    a receiver that treats a repeat as a new event will double-charge somebody.
    """
    return {
        "id": f"evt_{uuid.uuid4().hex}",
        "event": event_type,
        "timestamp": timezone.now().isoformat(),
        "data": data,
    }


def emit(event_type: str, tenant_id, data: dict) -> int:
    """
    Fan one event out to every endpoint in this tenant subscribed to it.

    Returns the number of deliveries queued -- zero is the overwhelmingly
    common answer, because most workspaces have no webhooks at all, and it is
    reached in one indexed query with no further work.

    Never raises. This is called from signal receivers sitting on the send and
    inbound paths, and an exception here would fail the send that triggered it.
    A webhook that could break a message send is worse than no webhook.
    """
    if not getattr(settings, "PUBLIC_API_WEBHOOKS_ENABLED", True):
        return 0

    try:
        from .models import WebhookDelivery, WebhookEndpoint
        from .tasks import deliver_webhook

        endpoints = list(
            WebhookEndpoint.objects.filter(tenant_id=tenant_id, is_active=True).only(
                "id", "tenant_id", "events",
            )
        )
        subscribed = [endpoint for endpoint in endpoints if endpoint.wants(event_type)]
        if not subscribed:
            return 0

        envelope = build(event_type, data)
        deliveries = WebhookDelivery.objects.bulk_create([
            WebhookDelivery(
                tenant_id=tenant_id,
                endpoint=endpoint,
                event_type=event_type,
                event_id=envelope["id"],
                payload=envelope,
                status=WebhookDelivery.Status.PENDING,
                next_attempt_at=timezone.now(),
            )
            for endpoint in subscribed
        ])

        for delivery in deliveries:
            # Queued outside any transaction the caller may hold open would be
            # ideal, but this is called from signal receivers with no reliable
            # way to know. `deliver_webhook` re-reads the row and returns
            # quietly if it is not visible yet, and the sweeper picks up
            # anything that lands that way -- see tasks.py.
            deliver_webhook.delay(delivery.pk)

        return len(deliveries)
    except Exception:  # noqa: BLE001 -- see the docstring
        logger.exception("Could not emit webhook event %s for tenant %s.", event_type, tenant_id)
        return 0


# --- Payload builders ----------------------------------------------------
#
# One function per shape, so the receivers stay about *when* to emit and these
# stay about *what* an event looks like. Every one of them returns only fields
# the API itself already exposes -- there is no path from here to a session
# file, a Baileys credential, or an access token.


def message_payload(message) -> dict:
    from .serializers import public_message_id

    return {
        "message_id": public_message_id(message.pk),
        "campaign_id": message.campaign_id,
        "account_id": message.whatsapp_account_id,
        "channel": "whatsapp",
        "recipient": message.to_phone,
        "status": _public_status(message.status),
        "type": (message.message_type or "TEXT").lower(),
        "error": message.error_message or None,
        "failure_reason": message.failure_reason or None,
        "created_at": message.queued_at.isoformat() if message.queued_at else None,
        "updated_at": message.updated_at.isoformat() if message.updated_at else None,
    }


def inbound_payload(message) -> dict:
    from .serializers import public_message_id

    return {
        "message_id": public_message_id(message.pk),
        "account_id": message.whatsapp_account_id,
        "channel": "whatsapp",
        "from": message.to_phone,
        "contact_id": message.contact_id,
        "conversation_id": message.conversation_id,
        "type": (message.message_type or "TEXT").lower(),
        "text": message.body or "",
        "media_url": message.media_url or None,
        "timestamp": message.queued_at.isoformat() if message.queued_at else None,
    }


def button_payload(message) -> dict:
    """
    A contact choosing an option.

    `button_id` and `button_text` are read straight off the Message row that
    the inbound path already resolved -- including the numbered-reply fallback
    a QR-linked number needs, which `apps.automation.services` resolves onto
    exactly these two fields. `interaction_type` is passed through so an
    integration can tell a real tap from "the customer typed 2", because on a
    QR connection those are the same choice arriving two different ways.
    """
    payload = inbound_payload(message)
    payload.update({
        "button_id": message.replied_button_id,
        "button_text": message.body or "",
        "interaction_type": (message.interaction_type or "").lower() or None,
    })
    return payload


def campaign_payload(campaign) -> dict:
    return {
        "campaign_id": campaign.pk,
        "name": campaign.name,
        "status": (campaign.status or "").lower(),
        "channel": "whatsapp",
        "account_id": campaign.whatsapp_account_id,
        "total_recipients": campaign.total_recipients,
        "sent_count": campaign.sent_count,
        "delivered_count": campaign.delivered_count,
        "read_count": campaign.read_count,
        "failed_count": campaign.failed_count,
        "skipped_count": campaign.skipped_count,
        "started_at": campaign.started_at.isoformat() if campaign.started_at else None,
        "completed_at": campaign.completed_at.isoformat() if campaign.completed_at else None,
    }


def account_payload(account) -> dict:
    return {
        "account_id": account.pk,
        "name": account.name,
        "channel": "whatsapp",
        "provider": _public_provider(account),
        "status": (account.status or "").lower(),
        "phone": account.phone_number or None,
    }


def _public_status(status: str) -> str:
    """Message.Status -> the lowercase vocabulary the public API publishes."""
    from .serializers import PUBLIC_STATUS

    return PUBLIC_STATUS.get(status, (status or "").lower())


def _public_provider(account) -> str:
    from .serializers import PUBLIC_PROVIDER

    return PUBLIC_PROVIDER.get(account.provider, (account.provider or "").lower())
