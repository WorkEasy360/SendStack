"""
What the WhatsApp agent is allowed to *do*, as opposed to say.

The calling agent already has a tool layer (`apps.voice.tools`) and this is
deliberately built to the same shape -- a `definitions()` that returns schemas
gated on the tenant's configuration, an `execute()` that never raises, and a
`ToolOutcome` carrying what the model is told. Sharing the shape is what lets
the two agents share the provider layer underneath (`apps.ai.providers`
translates tool calls for every vendor exactly once) and what makes a person
who has read one able to read the other.

What is *not* shared is the tools themselves, and the reason is worth stating
because it looks like duplication. The voice tools take a `CallSession`: they
end calls, transfer lines and speak farewells, and the one that sends a
WhatsApp message picks any connected number the tenant has, because a phone
call is not on a WhatsApp number and has no better answer. Here there is a
conversation, and picking "any connected number" would be a bug -- a customer
who has been talking to the shop's sales line would get an answer from its
support line, on a thread they have never seen. So the context is different,
the correct account is knowable, and the tools that write are different tools.

Three rules govern everything below.

**The model never chooses whose data it touches.** Every identifier that
decides ownership -- tenant, conversation, contact, WhatsApp account -- comes
from `ToolContext`, which is built from the conversation row. None of them
appear in any tool's input schema, so there is no argument the model can emit
that reaches another tenant's data. A tool that needs to look something up
filters on `context.tenant_id` itself rather than trusting anything it was
handed.

**A tool is refused before it runs, not after.** Each one declares a
capability (read, write, send) and the tenant's config grants capabilities.
An ungranted tool is never offered in `definitions()` *and* is refused in
`execute()` -- twice, because a model that saw the tool in an earlier turn's
cached prefix can still ask for it, and because the schema is a hint while the
check is the boundary.

**Nothing internal reaches the customer.** A tool that fails returns a flat
sentence the model can act on. The exception, the row id, the constraint name
and the query stay in the log where somebody with access can read them.
"""

import logging
from dataclasses import dataclass, field

from django.utils import timezone

from . import knowledge

logger = logging.getLogger("sendstack")

#: Capability a tool needs before it may run. Ordered by how much damage a
#: wrong call does, which is the order the config grants them in.
READ = "READ"
WRITE = "WRITE"
SEND = "SEND"

#: Terminal actions a tool can demand of the loop -- the agent stops talking
#: and a person takes the conversation. Modelled as a tool rather than a
#: phrase the loop greps for, for the same reason the voice agent does it:
#: "let me get someone" and "someone will get back to you" are indistinguishable
#: as text and opposite as actions.
HANDOFF = "HANDOFF"

#: Longest tool result fed back to the model. A tool that returns a wall of
#: text turns every subsequent round into an expensive one, and the model
#: cannot use what it cannot fit anyway.
MAX_RESULT_CHARS = 4000

#: Most messages one conversation's agent may send with the send tool per
#: turn. The reply itself is separate; this bounds the *extra* ones.
MAX_TOOL_SENDS_PER_TURN = 2


@dataclass
class ToolOutcome:
    """The result of one tool call, and what it does to the turn."""

    #: What the model is told happened. Fed back as the tool_result.
    result: str = ""
    #: "" or HANDOFF.
    terminal: str = ""
    is_error: bool = False
    #: Recorded on the audit row, never shown to the model or the customer.
    detail: str = ""


@dataclass
class ToolContext:
    """
    Who this turn is for, and on whose behalf.

    Built once, from the conversation, by `for_conversation`. Every field is
    derived from a database row rather than from anything the model said,
    which is the whole of the tenant-isolation guarantee: there is no path
    from a model's output into any of these values.
    """

    tenant_id: int
    conversation_id: int
    contact_id: int
    whatsapp_account_id: int
    #: The account's owner -- who the send is billed to and who the audit row
    #: attributes the action to.
    user_id: int | None = None
    #: The provider-side identity of the number this conversation is on. A
    #: QR-linked number is a paired device and carries `provider_session_id`;
    #: a Cloud API number is a `phone_number_id`. Both answer "which physical
    #: WhatsApp identity will this go out from", which is the question that
    #: matters when a tenant has several.
    device_id: str = ""

    #: The loaded rows, so tools do not each re-query them.
    conversation: object = None
    contact: object = None
    account: object = None

    #: Messages already sent by tools this turn, for the send cap.
    sends_this_turn: int = 0
    #: Names of tools already run this turn, for the audit trail.
    ran: list = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return bool(self.tenant_id and self.conversation_id and self.contact_id and self.whatsapp_account_id)


def device_id_for(account) -> str:
    """
    The provider-side identity of one number.

    Two connection methods, two identifiers, one question. A QR-linked number
    is a device paired to a phone and is known by its bridge session; a Cloud
    API number is known by its `phone_number_id`. Returning whichever exists
    means a tool logging "which device did this go out on" is answerable for
    both, and answerable for a number that was re-paired this morning without
    anything being re-enrolled.
    """
    if account is None:
        return ""
    return (getattr(account, "provider_session_id", "") or getattr(account, "phone_number_id", "") or "").strip()


def for_conversation(conversation) -> ToolContext:
    """
    The context one conversation's tools run in.

    Everything comes off the conversation, including the account -- this is
    what requirement "use the account the conversation is on" reduces to in
    code, and doing it here rather than in each tool means a tool cannot get
    it wrong.
    """
    account = conversation.whatsapp_account
    return ToolContext(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.pk,
        contact_id=conversation.contact_id,
        whatsapp_account_id=conversation.whatsapp_account_id,
        user_id=getattr(account, "user_id", None),
        device_id=device_id_for(account),
        conversation=conversation,
        contact=conversation.contact,
        account=account,
    )


# ---------------------------------------------------------------------------
# What the tenant has granted
# ---------------------------------------------------------------------------

def granted(config) -> set:
    """
    The capabilities this tenant's agent has been given.

    Defaults are timid for the same reason every other default in this app is:
    an agent that can write to customer records and send messages out of the
    box is a support ticket waiting to happen, and the tenant who wants that
    can say so in one click.
    """
    if not getattr(config, "tools_enabled", False):
        return set()

    capabilities = {READ}
    if getattr(config, "allow_write_tools", False):
        capabilities.add(WRITE)
    if getattr(config, "allow_send_tools", False):
        capabilities.add(SEND)
    return capabilities


def is_allowed(name: str, config) -> bool:
    """Whether this tool may run for this tenant at all."""
    spec = _TOOLS.get(name)
    if spec is None:
        return False
    return spec["capability"] in granted(config)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

#: The tool set, in a fixed order.
#:
#: Fixed because the list is part of the cached prompt prefix -- a set that
#: reorders between turns re-bills the whole prefix every turn. Same reason
#: the voice agent fixes its order.
#:
#: No schema below contains a tenant, conversation, contact or account
#: identifier. That is not an oversight: those come from `ToolContext`, and a
#: schema that accepted one would be an argument the model could get wrong or
#: be talked into getting wrong.
_DEFINITIONS = {
    "search_knowledge": {
        "capability": READ,
        "description": (
            "Look something up in the business's own reference material -- prices, opening hours, policies, "
            "delivery times, product details. Use this whenever the customer asks a specific factual question "
            "you have not already been given the answer to. Never guess at a fact you could look up."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look up, in a few words."},
            },
            "required": ["query"],
        },
    },
    "lookup_customer": {
        "capability": READ,
        "description": (
            "Look up what our records say about the person you are talking to: their name, their recent "
            "orders and conversations, and what stage they are at with us. Use it when what they are asking "
            "depends on their own history with us -- 'where is my order', 'what did I buy last time'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    "save_customer_details": {
        "capability": WRITE,
        "description": (
            "Record something the customer just told you that is worth keeping: their name, their email "
            "address, or a note about what they need. Call this as soon as you learn it. Only ever record "
            "what they actually said -- never a guess."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The customer's name, if they gave it."},
                "email": {"type": "string", "description": "Their email address, if they gave it."},
                "note": {"type": "string", "description": "Anything else worth recording, in one line."},
            },
        },
    },
    "tag_contact": {
        "capability": WRITE,
        "description": (
            "Put a label on this customer so the team can find them later -- for example 'Pricing enquiry' "
            "or 'Wants a callback'. Use an existing label's wording where one obviously fits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "The label, two or three words."},
            },
            "required": ["tag"],
        },
    },
    "send_whatsapp_message": {
        "capability": SEND,
        "description": (
            "Send the customer an extra WhatsApp message on this same conversation, separately from your "
            "reply. Use it only for something that genuinely does not belong in the reply -- a long address, "
            "a reference number, a price list. Do not use it to say the same thing twice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The message text. Plain text, no markdown."},
            },
            "required": ["message"],
        },
    },
    "hand_to_human": {
        "capability": READ,
        "description": (
            "Hand this conversation to a person and stop answering. Use it when the customer asks for a "
            "human, when they are upset, when they are asking about something you have no reference material "
            "for, or when what they need is beyond what you can do. Handing over is always better than "
            "guessing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why. Recorded for the team, not shown to the customer."},
            },
            "required": ["reason"],
        },
    },
}


def definitions(config) -> list:
    """
    The tools this tenant's agent may use, in a fixed order.

    Read from `_TOOLS` rather than from `_DEFINITIONS`, so that what is
    *offered* and what can *run* come from one registry. Split across two
    dicts they can drift, and the drift is silent in the worst direction: a
    tool described to the model that `execute` then rejects as unknown, which
    looks to everyone like the model hallucinating a tool name.
    """
    allowed = granted(config)
    return [
        {"name": name, "description": spec["description"], "input_schema": spec["input_schema"]}
        for name, spec in _TOOLS.items()
        if spec["capability"] in allowed
    ]


def available(config) -> list:
    """Just the names, for the status panel and the tests."""
    return [tool["name"] for tool in definitions(config)]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

#: What a tool that failed tells the model. Deliberately says nothing about
#: why: the model relays what it is told, so anything specific here is a
#: sentence a customer could end up reading. The real reason goes to the log
#: and the audit row.
_FAILED = "That did not work. Carry on without it, and offer to get a colleague if you need to."

_DENIED = "You are not able to do that. Carry on without it."


def execute(name: str, payload: dict, config, context: ToolContext) -> ToolOutcome:
    """
    Run one tool.

    Never raises. An unanswered tool_use is a 400 on the very next request, so
    a tool that explodes must still produce a result -- the alternative is a
    customer who gets nothing at all because a CRM write hit a constraint.
    """
    started = timezone.now()
    spec = _TOOLS.get(name)

    if spec is None:
        # A model asking for a tool that does not exist is usually one
        # confusing this agent's tools with another's -- but it is also what a
        # prompt-injection attempt looks like from here, so it is recorded
        # rather than merely refused.
        logger.info("The WhatsApp agent asked for an unknown tool %r on conversation %s.", name, context.conversation_id)
        outcome = ToolOutcome(result=f"There is no tool called {name}.", is_error=True, detail="unknown tool")

    elif spec["capability"] not in granted(config):
        # Checked here as well as in `definitions` because the two answer
        # different questions: definitions decides what to *offer*, this
        # decides what may *run*. A model working from a cached prefix, or one
        # that simply invents a plausible name, reaches this and not the other.
        logger.warning(
            "The WhatsApp agent tried %s on conversation %s without the %s capability.",
            name, context.conversation_id, spec["capability"],
        )
        outcome = ToolOutcome(result=_DENIED, is_error=True, detail=f"{spec['capability']} not granted")

    elif not context.is_complete:
        # Missing any of the four owning identifiers means this turn cannot
        # prove whose data it is about, which is exactly when not to touch any.
        logger.error("Refusing tool %s: incomplete tool context %r.", name, context)
        outcome = ToolOutcome(result=_FAILED, is_error=True, detail="incomplete context")

    else:
        try:
            outcome = spec["handler"](payload or {}, config, context)
        except Exception as exc:  # noqa: BLE001 - a broken tool must not cost the customer their reply
            logger.exception("WhatsApp tool %s failed on conversation %s.", name, context.conversation_id)
            outcome = ToolOutcome(result=_FAILED, is_error=True, detail=f"{type(exc).__name__}: {exc}"[:500])

    outcome.result = (outcome.result or "")[:MAX_RESULT_CHARS]
    context.ran.append(name)
    # Every path lands here, refusals included. A tool that was *asked for* and
    # denied is the single most interesting row in this table -- it is what an
    # agent being talked into something looks like -- and recording only the
    # successes would leave exactly that invisible.
    _record(name, payload, outcome, context, started)
    return outcome


def _record(name, payload, outcome, context, started) -> None:
    """
    Write the audit row for one tool call.

    Never raises: losing the audit row is bad, losing the customer's answer
    because the audit row could not be written is worse. Kept as its own table
    rather than folded into `AIUsageLog` for the same reason `KnowledgeAnswer`
    is -- that one counts tokens and answers a billing question, this one
    records actions and answers "what did the agent actually do to this
    customer's record", and a table that tries to be both serves neither.
    """
    from .models import AIToolCall

    try:
        AIToolCall.objects.create(
            tenant_id=context.tenant_id,
            conversation_id=context.conversation_id,
            contact_id=context.contact_id,
            whatsapp_account_id=context.whatsapp_account_id,
            device_id=context.device_id[:255],
            name=name[:100],
            arguments=_redact(payload),
            result=(outcome.result or "")[:2000],
            is_error=outcome.is_error,
            detail=(outcome.detail or "")[:500],
            duration_ms=int((timezone.now() - started).total_seconds() * 1000),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not record the tool call %s on conversation %s.", name, context.conversation_id)


#: Argument names whose values are never written to the audit row. None of the
#: current tools take a secret, but the audit table outlives the tool set and
#: a tool added later should not silently start persisting one.
_SECRET_ARGS = ("password", "token", "secret", "api_key", "key", "authorization")


def _redact(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    return {
        key: ("[redacted]" if any(word in str(key).lower() for word in _SECRET_ARGS) else str(value)[:500])
        for key, value in payload.items()
    }


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

def _search_knowledge(payload, config, context) -> ToolOutcome:
    """
    Retrieval on demand, as opposed to the retrieval that already happened.

    This is what makes RAG and tools coexist rather than compete: the turn
    still starts with a retrieval on the customer's message, and this exists
    for the second thing they turn out to need -- "and how much is delivery?"
    after a question about opening hours. The scoping is the same call the
    reply path makes, so a tenant's knowledge-base selection applies here too.
    """
    query = (payload.get("query") or "").strip()
    if not query:
        return ToolOutcome(result="No query given.", is_error=True)

    retrieval = knowledge.retrieve(
        context.tenant_id, query,
        knowledge_base_id=config.knowledge_base_id, limit=config.knowledge_chunks,
    )

    # The same relevance floor the reply path applies, for the same reason:
    # retrieval returns the *least irrelevant* thing it has, not necessarily
    # something that answers the question. Handing a barely-matching passage to
    # the model is how an agent ends up quoting the delivery policy at somebody
    # asking about warranties -- and under AI_KNOWLEDGE_ONLY it would do it
    # while believing it was grounded, which is worse than an open refusal.
    from .services import relevance_floor

    if not retrieval or not retrieval.chunks or retrieval.relevance < relevance_floor(config):
        # Not an error: "we have nothing on that" is a true and useful answer,
        # and flagging it as a failure would push the model to retry it.
        return ToolOutcome(
            result="Nothing in the reference material covers that.",
            detail=f"relevance={round(retrieval.relevance, 3) if retrieval else 0.0} below floor",
        )

    return ToolOutcome(
        # Labelled with the citation, so an answer built from this can say
        # which document and page it came from.
        result="\n\n".join(
            f"[{chunk.citation or chunk.document.title}]\n{chunk.content}" for chunk in retrieval.chunks
        ),
        detail=f"relevance={round(retrieval.relevance, 3)}",
    )


def _lookup_customer(payload, config, context) -> ToolOutcome:
    """
    What the CRM knows about this contact.

    Takes no arguments at all, which is the point: *which* customer is not a
    question the model gets to answer. It is the one on this conversation, and
    the only way to reach anybody else's record would be to change this line.
    """
    from apps.crm import context as crm_context

    customer = crm_context.for_conversation(context.conversation)
    if customer is None:
        return ToolOutcome(result="There is nothing on file for this customer yet.")

    return ToolOutcome(result=customer.to_prompt() or "There is nothing on file for this customer yet.")


def _save_customer_details(payload, config, context) -> ToolOutcome:
    """
    Fill in blanks on the contact record.

    Only blanks. A name typed in by the business is better evidence than a
    name inferred from a chat message, so an existing value is never
    overwritten -- the model's version lands in `custom_fields`, where a
    person can see it and decide. Same rule the voice agent's equivalent
    follows, for the same reason.
    """
    contact = context.contact
    if contact is None or contact.tenant_id != context.tenant_id:
        return ToolOutcome(result=_FAILED, is_error=True, detail="contact missing or cross-tenant")

    fields = {
        key: str(payload.get(key) or "").strip()
        for key in ("name", "email", "note")
        if str(payload.get(key) or "").strip()
    }
    if not fields:
        return ToolOutcome(result="Nothing to save.", is_error=True)

    updates = []
    if fields.get("name") and not contact.name:
        contact.name = fields["name"][:150]
        updates.append("name")
    if fields.get("email") and not contact.email:
        contact.email = fields["email"][:254]
        updates.append("email")

    custom = dict(contact.custom_fields or {})
    for key, value in fields.items():
        custom[f"chat_{key}"] = value[:500]
    if custom != (contact.custom_fields or {}):
        contact.custom_fields = custom
        updates.append("custom_fields")

    if updates:
        contact.save(update_fields=updates + ["updated_at"])

    _timeline(context, "The assistant recorded customer details", {"fields": sorted(fields)})
    return ToolOutcome(result=f"Saved: {', '.join(sorted(fields))}.")


def _tag_contact(payload, config, context) -> ToolOutcome:
    """
    Label the contact.

    The tag is looked up and created within `context.tenant_id`, so two
    tenants asking for "VIP" get two tags and neither can see the other's.
    """
    from apps.contacts.models import ContactTag

    name = (payload.get("tag") or "").strip()[:50]
    if not name:
        return ToolOutcome(result="No label given.", is_error=True)

    contact = context.contact
    if contact is None or contact.tenant_id != context.tenant_id:
        return ToolOutcome(result=_FAILED, is_error=True, detail="contact missing or cross-tenant")

    tag, _ = ContactTag.objects.get_or_create(
        tenant_id=context.tenant_id, name=name, defaults={"color": "#2563eb"},
    )
    contact.tags.add(tag)
    _timeline(context, f"The assistant tagged this customer {name}", {"tag": name})
    return ToolOutcome(result=f"Tagged as {name}.")


def _send_whatsapp_message(payload, config, context) -> ToolOutcome:
    """
    An extra message on this conversation, on the number the conversation is on.

    `context.account` rather than "the tenant's first connected number" is the
    whole of requirement 11: a tenant with a sales line and a support line has
    a customer who is talking to exactly one of them, and answering from the
    other is a message from a number they have never seen, on a thread that
    does not exist, about something they said somewhere else.

    Two guards, both about loops:

    * The number is checked against the tenant's own connected accounts. An
      agent that messages one of its own numbers has just written itself an
      inbound message, which the ladder answers, which sends another -- a loop
      that costs real money and real send quota and is invisible until the bill.
    * Sends are capped per turn. The model restating itself is normal; three
      copies of it arriving on somebody's phone is not.
    """
    from apps.automation.services import send_automated_message
    from apps.messaging.models import Message
    from apps.whatsapp.models import WhatsAppAccount

    text = (payload.get("message") or "").strip()
    if not text:
        return ToolOutcome(result="No message given.", is_error=True)

    if context.sends_this_turn >= MAX_TOOL_SENDS_PER_TURN:
        return ToolOutcome(
            result="You have already sent an extra message this turn. Put the rest in your reply.",
            is_error=True, detail="per-turn send cap",
        )

    account = context.account
    if account is None or account.pk != context.whatsapp_account_id:
        return ToolOutcome(result=_FAILED, is_error=True, detail="conversation has no usable account")

    contact = context.contact
    if contact is None or contact.tenant_id != context.tenant_id:
        return ToolOutcome(result=_FAILED, is_error=True, detail="contact missing or cross-tenant")

    # The loop guard. Sending to one of our own connected numbers turns an
    # outbound message into an inbound one and starts the ladder again.
    own_numbers = set(
        WhatsAppAccount.objects.filter(tenant_id=context.tenant_id)
        .exclude(phone_number__isnull=True)
        .exclude(phone_number="")
        .values_list("phone_number", flat=True)
    )
    if _normalise(contact.phone) in {_normalise(number) for number in own_numbers}:
        logger.warning(
            "Refused a tool send from conversation %s: %s is one of tenant %s's own numbers.",
            context.conversation_id, contact.phone, context.tenant_id,
        )
        return ToolOutcome(result=_FAILED, is_error=True, detail="refused: would message our own number")

    # SERVICE, not MARKETING: this answers something the customer asked
    # seconds ago, so it goes now rather than waiting for a sending window.
    sent = send_automated_message(
        account, contact,
        {"message_type": Message.MessageType.TEXT, "body": text},
        conversation=context.conversation,
        reference=f"ai:tool:{context.conversation_id}",
        kind=Message.Kind.SERVICE,
    )
    if sent is None:
        # Most often out of credit, or the number disconnected between the
        # turn starting and the tool running. Neither is the customer's problem
        # and neither is safe to describe to them.
        return ToolOutcome(result="The message could not be sent. Tell them you will follow up.",
                           is_error=True, detail="send_automated_message declined")

    context.sends_this_turn += 1
    return ToolOutcome(result="Sent.", detail=f"message={sent.pk} account={account.pk} device={context.device_id}")


def _hand_to_human(payload, config, context) -> ToolOutcome:
    """
    Stop, and put a person on it.

    The handoff itself is not performed here -- the loop does it once the turn
    ends, through `services.hand_off`, which is the same path every other
    handoff takes. Doing it here would mean a tool that half-ends a turn that
    then carries on talking.
    """
    reason = (payload.get("reason") or "").strip() or "the assistant asked for a person"
    return ToolOutcome(result="A colleague will take this conversation.", terminal=HANDOFF, detail=reason[:200])


def _timeline(context, summary, metadata) -> None:
    """Put an action the agent took on the customer's CRM timeline."""
    if not getattr(context.conversation, "tenant_id", None):
        return
    try:
        from apps.crm import activity
        from apps.crm.models import Activity

        activity.record(context.contact, Activity.Kind.AUTOMATION, summary, metadata=metadata or {})
    except Exception:  # noqa: BLE001 - bookkeeping never costs a customer their answer
        logger.exception("Could not record a tool action on contact %s", context.contact_id)


def _normalise(phone) -> str:
    """A phone number reduced to its digits, for comparing ours against theirs."""
    return "".join(character for character in str(phone or "") if character.isdigit())


_TOOLS = {
    name: {**spec, "handler": handler}
    for name, spec, handler in (
        ("search_knowledge", _DEFINITIONS["search_knowledge"], _search_knowledge),
        ("lookup_customer", _DEFINITIONS["lookup_customer"], _lookup_customer),
        ("save_customer_details", _DEFINITIONS["save_customer_details"], _save_customer_details),
        ("tag_contact", _DEFINITIONS["tag_contact"], _tag_contact),
        ("send_whatsapp_message", _DEFINITIONS["send_whatsapp_message"], _send_whatsapp_message),
        ("hand_to_human", _DEFINITIONS["hand_to_human"], _hand_to_human),
    )
}
