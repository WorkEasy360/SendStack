"""
What the calling agent is allowed to do, as opposed to say.

Every tool here is a real side effect on a live phone call, which is why the
set is small, each one is gated on a config flag, and none of them can be
undone by the person on the other end. The two that end the conversation --
`end_call` and `transfer_to_human` -- are modelled as tools rather than as
phrases the engine greps for, because "I'll put you through to someone" and
"someone will call you back" are indistinguishable as text and opposite as
actions.

Tool results are written into the transcript as TOOL turns, so the recording
of what the agent *did* sits in the same ordered list as what it said.
"""

import logging
from dataclasses import dataclass

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.ai import knowledge

logger = logging.getLogger("sendstack")

# Terminal actions a tool can demand of the engine.
END_CALL = "END_CALL"
TRANSFER = "TRANSFER"


@dataclass
class ToolOutcome:
    """The result of one tool call, and whether the call survives it."""

    #: What the model is told happened. Fed back as the tool_result.
    result: str = ""
    #: "", END_CALL or TRANSFER.
    terminal: str = ""
    #: Said out loud before the call ends or transfers.
    farewell: str = ""
    is_error: bool = False


def definitions(config) -> list:
    """
    The tools this tenant's agent may use, in a fixed order.

    Fixed because the tool list is part of the cached prompt prefix: a set
    that reorders between turns re-bills the whole prefix on every turn of
    every call.
    """
    tools = [
        {
            "name": "end_call",
            "description": (
                "End the phone call. Use this when the conversation has reached a natural close, when the "
                "person asks you to stop calling, or when they say goodbye. Always say a closing line first "
                "via the farewell argument -- hanging up mid-conversation is rude and the person will notice."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "farewell": {
                        "type": "string",
                        "description": "The last thing said before the line drops. One short sentence.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why the call is ending. Recorded, not spoken.",
                    },
                },
                "required": ["farewell"],
            },
        },
        {
            "name": "save_details",
            "description": (
                "Record something the person told you that is worth keeping: their name, their email, what "
                "they want, or the outcome of the call. Call this as soon as you learn something rather than "
                "at the end -- if the line drops, only what you have already saved survives."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The person's name, if they gave it."},
                    "email": {"type": "string", "description": "Their email address, if they gave it."},
                    "interest": {"type": "string", "description": "What they are interested in, in a few words."},
                    "notes": {"type": "string", "description": "Anything else worth recording."},
                },
            },
        },
        {
            "name": "search_knowledge",
            "description": (
                "Look something up in the business's own reference material -- prices, hours, policies, "
                "product details. Use this rather than guessing whenever the person asks a specific factual "
                "question you have not already been given the answer to."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look up, in a few words."},
                },
                "required": ["query"],
            },
        },
    ]

    if config.allow_transfer and config.transfer_number:
        tools.append({
            "name": "transfer_to_human",
            "description": (
                "Hand this call to a person and leave the conversation. Use it when they ask for a human, "
                "when they are upset, or when what they need is beyond what you can do. Transferring is "
                "always better than guessing."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "farewell": {
                        "type": "string",
                        "description": "What to say while connecting them. One short sentence.",
                    },
                    "reason": {"type": "string", "description": "Why. Recorded, not spoken."},
                },
                "required": ["farewell"],
            },
        })

    if config.allow_whatsapp_followup:
        tools.append({
            "name": "send_whatsapp",
            "description": (
                "Send this person a WhatsApp message on the number you are calling, while still on the call. "
                "Use it for anything they would rather read than hear: a link, an address, a price list, a "
                "reference number. Tell them you are sending it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "The message text. Plain text, no markdown."},
                },
                "required": ["message"],
            },
        })

    if config.allow_callback_scheduling:
        tools.append({
            "name": "schedule_callback",
            "description": (
                "Arrange for this person to be called back later, when they say now is not a good time. "
                "Confirm the time with them before calling this."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "when": {
                        "type": "string",
                        "description": "When to call back, as an ISO-8601 timestamp with a timezone offset.",
                    },
                    "note": {"type": "string", "description": "What the callback is about."},
                },
                "required": ["when"],
            },
        })

    return tools


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def execute(name: str, payload: dict, config, session) -> ToolOutcome:
    """
    Run one tool. Never raises: a tool that blows up must still produce a
    tool_result, because an unanswered tool_use is a 400 on the next request
    and a 400 mid-call is a dropped call.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return ToolOutcome(result=f"There is no tool called {name}.", is_error=True)

    try:
        return handler(payload or {}, config, session)
    except Exception:
        logger.exception("Voice tool %s failed on call %s.", name, session.pk)
        return ToolOutcome(result="That did not work. Carry on without it.", is_error=True)


def _end_call(payload, config, session) -> ToolOutcome:
    return ToolOutcome(
        result="The call is ending.",
        terminal=END_CALL,
        farewell=(payload.get("farewell") or "").strip(),
    )


def _transfer(payload, config, session) -> ToolOutcome:
    if not (config.allow_transfer and config.transfer_number):
        return ToolOutcome(
            result="There is nobody available to transfer to. Help them yourself or offer a callback.",
            is_error=True,
        )
    return ToolOutcome(
        result="Transferring now.",
        terminal=TRANSFER,
        farewell=(payload.get("farewell") or "").strip(),
    )


def _save_details(payload, config, session) -> ToolOutcome:
    fields = {
        key: str(value).strip()
        for key, value in payload.items()
        if key in ("name", "email", "interest", "notes") and str(value or "").strip()
    }
    if not fields:
        return ToolOutcome(result="Nothing to save.", is_error=True)

    collected = dict(session.collected or {})
    # Notes accumulate; the single-value fields are corrections of each other,
    # because somebody spelling their name again means the second spelling.
    for key, value in fields.items():
        if key == "notes":
            existing = collected.get("notes") or ""
            collected["notes"] = f"{existing}\n{value}".strip()
        else:
            collected[key] = value

    session.collected = collected
    session.save(update_fields=["collected"])

    _update_contact(session, fields)
    return ToolOutcome(result=f"Saved: {', '.join(sorted(fields))}.")


def _update_contact(session, fields) -> None:
    """
    Push what was learned onto the contact record.

    Only fills blanks. A name typed by the business is better evidence than a
    name a speech recogniser heard down a phone line, so the transcript never
    overwrites one that is already there -- it lands in `custom_fields` where
    somebody can look at it.
    """
    contact = session.contact
    if contact is None:
        return

    updates = []
    if fields.get("name") and not contact.name:
        contact.name = fields["name"][:150]
        updates.append("name")
    if fields.get("email") and not contact.email:
        contact.email = fields["email"][:254]
        updates.append("email")

    custom = dict(contact.custom_fields or {})
    for key in ("name", "email", "interest", "notes"):
        if fields.get(key):
            custom[f"call_{key}"] = fields[key]
    if custom != (contact.custom_fields or {}):
        contact.custom_fields = custom
        updates.append("custom_fields")

    if updates:
        contact.save(update_fields=updates + ["updated_at"])


def _search_knowledge(payload, config, session) -> ToolOutcome:
    query = (payload.get("query") or "").strip()
    if not query:
        return ToolOutcome(result="No query given.", is_error=True)

    chunks = knowledge.search(session.tenant_id, query, limit=config.knowledge_chunks)
    if not chunks:
        return ToolOutcome(result="Nothing in the reference material covers that.")

    return ToolOutcome(
        result="\n\n".join(f"[{chunk.document.title}]\n{chunk.content}" for chunk in chunks)
    )


def _send_whatsapp(payload, config, session) -> ToolOutcome:
    message = (payload.get("message") or "").strip()
    if not message:
        return ToolOutcome(result="No message given.", is_error=True)
    if session.contact is None:
        return ToolOutcome(result="This call has no contact record, so nothing can be sent.", is_error=True)

    from apps.automation.services import send_automated_message
    from apps.messaging.models import Message
    from apps.whatsapp.models import WhatsAppAccount

    account = (
        WhatsAppAccount.objects.filter(
            tenant_id=session.tenant_id, status=WhatsAppAccount.Status.CONNECTED,
        )
        .order_by("pk")
        .first()
    )
    if account is None:
        return ToolOutcome(result="No WhatsApp number is connected, so nothing can be sent.", is_error=True)

    # SERVICE, not MARKETING: this is a reply to something the person asked for
    # thirty seconds ago on the phone, so it should go out now rather than wait
    # for the tenant's next sending window.
    sent = send_automated_message(
        account,
        session.contact,
        {"message_type": Message.MessageType.TEXT, "body": message},
        reference=f"voice:{session.pk}",
        kind=Message.Kind.SERVICE,
    )
    if sent is None:
        return ToolOutcome(result="The message could not be queued. Tell them you will follow up.", is_error=True)

    return ToolOutcome(result="Sent to their WhatsApp.")


def _schedule_callback(payload, config, session) -> ToolOutcome:
    when = parse_datetime((payload.get("when") or "").strip())
    if when is None:
        return ToolOutcome(
            result="That time could not be understood. Ask for a date and time and try again.", is_error=True,
        )
    if timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.get_current_timezone())
    if when <= timezone.now():
        return ToolOutcome(result="That time is in the past. Ask for a time later than now.", is_error=True)

    from .models import CallSession
    from .tasks import place_call_task

    note = (payload.get("note") or "").strip()
    callback = CallSession.objects.create(
        tenant_id=session.tenant_id,
        contact=session.contact,
        created_by_id=session.created_by_id,
        to_number=session.to_number,
        direction=CallSession.Direction.OUTBOUND,
        provider=session.provider,
        goal=note or session.goal,
        context={"callback_for_call": session.pk, "note": note},
    )
    place_call_task.apply_async(args=[callback.pk], eta=when)

    collected = dict(session.collected or {})
    collected["callback_at"] = when.isoformat()
    session.collected = collected
    session.save(update_fields=["collected"])

    return ToolOutcome(result=f"A callback is booked for {when:%d %b %Y at %H:%M}.")


_HANDLERS = {
    "end_call": _end_call,
    "transfer_to_human": _transfer,
    "save_details": _save_details,
    "search_knowledge": _search_knowledge,
    "send_whatsapp": _send_whatsapp,
    "schedule_callback": _schedule_callback,
}
