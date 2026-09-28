from django.conf import settings
from django.db.models import F
from django.utils import timezone

from apps.contacts.models import Contact
from apps.contacts.utils import normalize_phone
from apps.credits.services import InsufficientCreditsError, reserve_credits
from apps.messaging.models import Message
from apps.whatsapp.models import WhatsAppAccount

from .models import Conversation

_MEDIA_TYPES = {"image", "video", "document"}


class InboxError(Exception):
    """Raised for inbox actions that don't apply given current state."""


def _extract_button_reply(message_obj: dict) -> tuple[str, str] | None:
    """
    ``(button_id, label)`` when this inbound message is a tapped button.

    Three provider shapes mean the same thing: an ``interactive`` reply to one
    of our reply buttons, an ``interactive`` reply to a list row, and the
    ``button`` payload an approved message template's quick reply sends back.
    All three are flattened here so the rest of the platform only ever sees
    "the contact chose the option with this id".
    """
    msg_type = message_obj.get("type")

    if msg_type == "interactive":
        interactive = message_obj.get("interactive", {})
        reply = interactive.get(interactive.get("type") or "", {})
        if isinstance(reply, dict) and reply.get("id"):
            return str(reply["id"]), str(reply.get("title") or reply["id"])
        return None

    if msg_type == "button":
        button = message_obj.get("button", {})
        payload = button.get("payload") or button.get("text")
        if payload:
            return str(payload), str(button.get("text") or payload)

    return None


def _describe_location(message_obj: dict) -> str:
    """A location as one readable line, keeping whatever detail arrived."""
    location = message_obj.get("location") or {}
    name = (location.get("name") or "").strip()
    address = (location.get("address") or "").strip()
    latitude, longitude = location.get("latitude"), location.get("longitude")

    parts = [part for part in (name, address) if part]
    if latitude is not None and longitude is not None:
        parts.append(f"{latitude}, {longitude}")
    return f"[location: {' — '.join(parts)}]" if parts else "[location shared]"


def _describe_contacts(message_obj: dict) -> str:
    """Shared contact cards, named where the payload names them."""
    cards = message_obj.get("contacts") or []
    names = []
    for card in cards[:3]:
        if not isinstance(card, dict):
            continue
        name = (card.get("name") or {}).get("formatted_name") or ""
        if name:
            names.append(str(name).strip())
    if names:
        return f"[contact shared: {', '.join(names)}]"
    return f"[{len(cards) or 1} contact card shared]"


#: Inbound types with no attachment of ours to store, rendered as a short
#: bracketed line instead. They are listed rather than lumped into the
#: unknown-type branch so the thread reads as what happened ("voice message")
#: rather than as an internal complaint ("unsupported message type: audio"),
#: and so a chatbot can answer them sensibly -- see
#: ``apps.chatbot.engine._is_readable``.
_DESCRIBED_TYPES = {
    "audio": "[voice message]",
    "voice": "[voice message]",
    "sticker": "[sticker]",
    "video_note": "[video note]",
    "order": "[order placed]",
    "system": "[system message]",
    "unknown": "[unsupported message]",
}


def _extract_body_and_type(message_obj: dict) -> tuple[str, str]:
    """
    One inbound message reduced to (body, our message type).

    Every WhatsApp message type reaches this, including the ones the platform
    cannot store as an attachment. Those become a short bracketed description
    rather than an empty body, for two reasons: an empty row in the inbox is
    indistinguishable from a bug, and an automated reply looking at an empty
    string has nothing to decide from. The type stays TEXT for them because
    ``Message.MessageType`` describes what this platform can *send*, and
    widening it would let an API caller ask for an outbound sticker the
    provider layer has no way to deliver.
    """
    msg_type = (message_obj.get("type") or "text").lower()

    if msg_type == "text":
        return message_obj.get("text", {}).get("body", ""), Message.MessageType.TEXT

    if msg_type in _MEDIA_TYPES:
        media = message_obj.get(msg_type, {})
        body = media.get("caption") or f"[{msg_type} received]"
        return body, getattr(Message.MessageType, msg_type.upper())

    button_reply = _extract_button_reply(message_obj)
    if button_reply:
        # The label is what the contact saw themselves tap, so it reads as
        # their own words in the thread -- and keyword rules match it just as
        # they would have matched the same words typed out.
        return button_reply[1], Message.MessageType.TEXT

    if msg_type == "location":
        return _describe_location(message_obj), Message.MessageType.TEXT
    if msg_type == "contacts":
        return _describe_contacts(message_obj), Message.MessageType.TEXT
    if msg_type == "reaction":
        emoji = (message_obj.get("reaction") or {}).get("emoji") or ""
        return f"[reacted {emoji}]".replace(" ]", "]"), Message.MessageType.TEXT
    if msg_type in _DESCRIBED_TYPES:
        return _DESCRIBED_TYPES[msg_type], Message.MessageType.TEXT

    return f"[unsupported message type: {msg_type}]", Message.MessageType.TEXT


def record_inbound_message(event) -> Message | None:
    """Turn a stored WhatsAppWebhookEvent (event_type=INBOUND_MESSAGE) into a Conversation + Message."""
    account = event.account
    if not account.tenant_id:
        return None

    message_obj = event.payload.get("message", {})
    profile_name = event.payload.get("profile_name", "")
    raw_from = message_obj.get("from", "")
    if not raw_from:
        return None

    try:
        phone = normalize_phone(raw_from)
    except ValueError:
        return None

    contact, _ = Contact.objects.get_or_create(
        tenant=account.tenant, phone=phone,
        defaults={"name": profile_name, "created_by_id": account.user_id},
    )

    conversation, conversation_created = Conversation.objects.get_or_create(
        whatsapp_account=account, contact=contact, defaults={"tenant": account.tenant},
    )

    body, message_type = _extract_body_and_type(message_obj)
    button_reply = _extract_button_reply(message_obj)

    message = Message.objects.create(
        tenant=account.tenant, conversation=conversation, whatsapp_account=account, contact=contact,
        to_phone=contact.phone, direction=Message.Direction.INBOUND, message_type=message_type, body=body,
        status=Message.Status.DELIVERED, provider_message_id=message_obj.get("id") or None,
        replied_button_id=button_reply[0] if button_reply else "",
        # A real tap, as opposed to the number somebody typed off a fallback
        # list -- `automation.services.resolve_option_reply` stamps that one.
        # Kept apart so "the buttons are not reaching anybody's phone" stays
        # distinguishable from "they are reaching people who cannot tap them".
        interaction_type=Message.Interaction.NATIVE_BUTTON if button_reply else "",
        delivered_at=timezone.now(),
    )

    Conversation.objects.filter(pk=conversation.pk).update(
        unread_count=F("unread_count") + 1, last_message_at=timezone.now(),
    )

    # Transient hints for callers (e.g. the webhook receiver) -- not model
    # fields, never persisted. `referral_payload` is Meta's block describing
    # the click-to-WhatsApp ad this message came from; it exists only on the
    # first message of such a conversation, which is exactly when lead
    # attribution can be established at all.
    message.is_new_conversation = conversation_created
    message.referral_payload = message_obj.get("referral") or {}
    return message


def mark_read(conversation: Conversation) -> None:
    conversation.messages.filter(direction=Message.Direction.INBOUND, status=Message.Status.DELIVERED).update(
        status=Message.Status.READ, read_at=timezone.now(),
    )
    Conversation.objects.filter(pk=conversation.pk).update(unread_count=0)


def assign_agent(conversation: Conversation, agent) -> Conversation:
    if agent is not None and agent.tenant_id != conversation.tenant_id:
        raise InboxError("This agent does not belong to your tenant.")
    conversation.assigned_agent = agent
    conversation.save(update_fields=["assigned_agent", "updated_at"])
    return conversation


def close(conversation: Conversation) -> Conversation:
    conversation.status = Conversation.Status.CLOSED
    conversation.closed_at = timezone.now()
    conversation.save(update_fields=["status", "closed_at", "updated_at"])
    return conversation


def reopen(conversation: Conversation) -> Conversation:
    conversation.status = Conversation.Status.OPEN
    # Cleared rather than kept: a reopened conversation is open work again,
    # and leaving the old timestamp would report it as resolved in the same
    # breath as showing it in the open queue.
    conversation.closed_at = None
    conversation.save(update_fields=["status", "closed_at", "updated_at"])
    return conversation


def send_reply(conversation: Conversation, actor, *, message_type=Message.MessageType.TEXT, body="",
               media_url="", media_items=None, caption="", filename="", buttons=None) -> Message:
    if conversation.whatsapp_account.status != WhatsAppAccount.Status.CONNECTED:
        raise InboxError("This WhatsApp account is not connected.")
    if not body and not media_url and not media_items:
        raise InboxError("Provide a message body or at least one attachment.")

    try:
        reserve_credits(user_id=actor.pk, amount=1, reference=f"conversation:{conversation.pk}")
    except InsufficientCreditsError as exc:
        raise InboxError(str(exc)) from exc

    # Off by default: an agent typing a link into a live chat is usually
    # sending someone to a specific place, and quietly replacing what they
    # typed with a short code they cannot read back is a surprise. Deployments
    # that want every outbound link attributed turn it on.
    links = []
    if getattr(settings, "LINK_TRACKING_TRACK_REPLIES", False):
        from apps.tracking.services import default_utm, rewrite

        utm = default_utm("chat")
        body, body_links = rewrite(body, tenant_id=conversation.tenant_id, contact=conversation.contact, utm=utm)
        caption, caption_links = rewrite(
            caption, tenant_id=conversation.tenant_id, contact=conversation.contact, utm=utm,
        )
        links = body_links + caption_links

    message = Message.objects.create(
        tenant=conversation.tenant, conversation=conversation, whatsapp_account=conversation.whatsapp_account,
        contact=conversation.contact, to_phone=conversation.contact.phone, direction=Message.Direction.OUTBOUND,
        message_type=message_type, body=body, media_url=media_url, media_items=media_items or [],
        caption=caption, filename=filename, buttons=buttons or [],
        # An agent typing in the inbox is answering a live conversation, so
        # this is never held for quiet hours -- making someone wait until
        # morning for a reply they are sitting there waiting for helps nobody.
        # An unsubscribed contact is still off limits; see messaging.gating.
        kind=Message.Kind.SERVICE,
        billed_to=actor, status=Message.Status.QUEUED,
    )

    if links:
        from apps.tracking.services import save_links

        save_links(links, message=message)

    Conversation.objects.filter(pk=conversation.pk).update(last_message_at=timezone.now())

    # A person just answered, which is the only thing that stops the response
    # clock -- see apps.teams.services.on_human_reply.
    from apps.teams.services import on_human_reply

    on_human_reply(conversation)

    from apps.messaging.tasks import send_message_task

    send_message_task.delay(message.pk)
    message.refresh_from_db()  # a no-op unless CELERY_TASK_ALWAYS_EAGER already resolved it above
    return message
