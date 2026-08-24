"""
The customer context layer: one assembled view of who a customer is.

Every agent on this platform used to build its own idea of the person it was
talking to, and each built a different one. The WhatsApp agent knew the last
ten messages. The calling agent knew a name and whatever facts the caller
happened to pass in. Neither knew the customer had two open deals, an overdue
follow-up, a HOT score and an owner -- all of which were already sitting on
indexed columns one join away.

This module is the single answer to "who am I talking to". It is:

* **Deterministic.** No model call, no scoring, no inference. Everything here
  is a fact already recorded by some other part of the platform. Asking a
  model to work out whether a contact has an open deal would be slower, more
  expensive and less correct than the query that answers it.
* **Bounded.** Five deals, five tasks, two calls. A customer with four hundred
  tasks must not turn one WhatsApp reply into a four-hundred-line prompt --
  the prompt would cost more than the answer is worth, and the model would
  read the least important of them last.
* **Two-faced on purpose.** `to_dict()` is for APIs and tool results, where a
  caller wants fields it can index into. `to_prompt()` is for models, where
  what matters is that empty things are *absent* rather than rendered as
  "None" -- a brand-new contact should read as a few lines, not as a form with
  every box blank.

It lives in `apps.crm` because the CRM owns the customer record; the imports
of other apps are local to the functions that need them, which is the same
cycle-breaking idiom the rest of this app already uses.
"""

from dataclasses import dataclass, field

from django.utils import timezone

#: How much of each list reaches the model. Chosen so the whole customer
#: section stays around 150 tokens even for a busy account -- see the module
#: docstring on why this is capped rather than complete.
MAX_DEALS = 5
MAX_TASKS = 5
MAX_CALLS = 2
MAX_TAGS = 8

#: Past this, "when did they last reply" stops being a useful number and
#: becomes "a long time ago", which is what the prompt says instead.
STALE_DAYS = 90


def _days_since(moment, now):
    if not moment:
        return None
    return max(0, int((now - moment).total_seconds() // 86400))


def _ago(days) -> str:
    """A length of time the way a person says it, for the prompt."""
    if days is None:
        return "never"
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days > STALE_DAYS:
        return f"over {STALE_DAYS} days ago"
    return f"{days} days ago"


@dataclass
class CustomerContext:
    """
    One customer, as every agent and screen on the platform should see them.

    Deliberately a flat dataclass rather than the Contact model itself: it is
    handed to prompts and tool results, and a model instance in either of
    those places is an invitation to lazily load a relation on a code path
    that is already inside a request the carrier will hang up on.
    """

    contact_id: int = 0
    name: str = ""
    phone: str = ""
    company: str = ""
    email: str = ""
    country: str = ""

    owner: str = ""
    lifecycle: str = ""
    lifecycle_label: str = ""
    lead_score: int = 0
    score_band: str = ""
    tags: list = field(default_factory=list)
    source: str = ""
    subscribed: bool = True

    last_inbound_at: object = None
    last_outbound_at: object = None
    days_since_reply: object = None
    days_since_contact: object = None

    open_deals: list = field(default_factory=list)
    open_deal_value: object = 0
    won_deal_count: int = 0

    open_tasks: list = field(default_factory=list)
    recent_calls: list = field(default_factory=list)

    #: The live thread, when this context was built for one. None from a place
    #: with no conversation in hand -- an outbound call, the CRM API.
    conversation: object = None

    def to_dict(self) -> dict:
        """The shape an API response or a tool result wants."""
        return {
            "contact_id": self.contact_id,
            "name": self.name,
            "phone": self.phone,
            "company": self.company,
            "email": self.email,
            "country": self.country,
            "owner": self.owner,
            "lifecycle": self.lifecycle,
            "lifecycle_label": self.lifecycle_label,
            "lead_score": self.lead_score,
            "score_band": self.score_band,
            "tags": list(self.tags),
            "source": self.source,
            "subscribed": self.subscribed,
            "last_inbound_at": self.last_inbound_at,
            "last_outbound_at": self.last_outbound_at,
            "days_since_reply": self.days_since_reply,
            "days_since_contact": self.days_since_contact,
            "open_deals": list(self.open_deals),
            "open_deal_value": self.open_deal_value,
            "won_deal_count": self.won_deal_count,
            "open_tasks": list(self.open_tasks),
            "recent_calls": list(self.recent_calls),
            "conversation": self.conversation,
        }

    def to_prompt(self) -> str:
        """
        The customer, written for a model to read.

        Empty sections are omitted rather than rendered blank. A model told
        "Open deals: none. Tasks: none. Tags: none." spends attention on three
        facts that do not exist, and a new contact -- the commonest case on
        any account -- is exactly the one where that noise is largest.
        """
        lines = []

        who = self.name or "This customer"
        identity = [who]
        if self.company:
            identity.append(f"at {self.company}")
        lines.append(" ".join(identity) + f" ({self.phone}).")

        standing = []
        if self.lifecycle_label:
            standing.append(self.lifecycle_label.lower())
        if self.score_band:
            standing.append(f"{self.score_band.lower()} lead")
        if standing:
            lines.append(f"Status: {', '.join(standing)}.")

        if self.owner:
            lines.append(f"Looked after by {self.owner}.")

        if self.tags:
            lines.append(f"Tagged: {', '.join(self.tags)}.")

        if self.last_inbound_at or self.last_outbound_at:
            lines.append(
                f"They last wrote {_ago(self.days_since_reply)}; "
                f"we last wrote {_ago(self.days_since_contact)}."
            )

        if self.open_deals:
            deals = "; ".join(
                f"{deal['name']} ({deal['stage']}"
                + (f", {deal['currency']} {deal['value']:,.0f}" if deal.get("value") else "")
                + ")"
                for deal in self.open_deals
            )
            lines.append(f"Open sales: {deals}.")

        if self.won_deal_count:
            lines.append(f"They have bought from us before ({self.won_deal_count} closed sales).")

        if self.open_tasks:
            tasks = "; ".join(
                task["title"] + (" (overdue)" if task.get("overdue") else "")
                for task in self.open_tasks
            )
            lines.append(f"Outstanding with our team: {tasks}.")

        for call in self.recent_calls:
            summary = call.get("summary") or ""
            if summary:
                lines.append(f"On a recent phone call: {summary}")

        return "\n".join(lines)

    def __bool__(self):
        return bool(self.contact_id)


def build(contact, *, conversation=None) -> CustomerContext:
    """
    Assemble one customer.

    `contact` carries its own tenant, and every query below is filtered by the
    contact itself, so this cannot reach across tenants however it is called.
    That matters because the results end up in a prompt: a leaked row here
    would not be a wrong API response, it would be one customer's details read
    out loud to another.
    """
    if contact is None:
        return CustomerContext()

    from apps.crm.models import Deal, Task

    now = timezone.now()

    context = CustomerContext(
        contact_id=contact.pk,
        name=contact.name or "",
        phone=contact.phone or "",
        company=contact.company or "",
        email=contact.email or "",
        country=contact.country or "",
        lifecycle=contact.lifecycle_status,
        lifecycle_label=contact.get_lifecycle_status_display(),
        lead_score=contact.lead_score or 0,
        score_band=contact.score_band,
        source=contact.source or "",
        subscribed=bool(contact.subscribed),
        last_inbound_at=contact.last_inbound_at,
        last_outbound_at=contact.last_outbound_at,
        days_since_reply=_days_since(contact.last_inbound_at, now),
        days_since_contact=_days_since(contact.last_outbound_at, now),
    )

    owner = getattr(contact, "owner", None)
    if owner is not None:
        context.owner = owner.get_full_name() or owner.username

    context.tags = [tag.name for tag in contact.tags.all()[:MAX_TAGS]]

    deals = list(
        Deal.objects.filter(contact=contact, status=Deal.Status.OPEN)
        .select_related("stage")
        .order_by("-value", "-updated_at")[:MAX_DEALS]
    )
    context.open_deals = [
        {
            "id": deal.pk,
            "name": deal.name,
            "stage": deal.stage.name,
            "value": deal.value,
            "currency": deal.currency,
            "expected_close_date": deal.expected_close_date,
        }
        for deal in deals
    ]
    context.open_deal_value = sum((deal.value or 0) for deal in deals)
    context.won_deal_count = Deal.objects.filter(contact=contact, status=Deal.Status.WON).count()

    tasks = list(
        Task.objects.filter(contact=contact, status=Task.Status.OPEN).order_by("due_at")[:MAX_TASKS]
    )
    context.open_tasks = [
        {
            "id": task.pk,
            "title": task.title,
            "kind": task.kind,
            "due_at": task.due_at,
            "overdue": task.due_at < now,
        }
        for task in tasks
    ]

    context.recent_calls = _recent_calls(contact)

    if conversation is not None:
        agent = getattr(conversation, "assigned_agent", None)
        context.conversation = {
            "id": conversation.pk,
            "status": conversation.status,
            "assigned_to": (agent.get_full_name() or agent.username) if agent else "",
            "awaiting_reply_since": conversation.awaiting_reply_since,
            "unread_count": conversation.unread_count,
        }

    return context


def _recent_calls(contact) -> list:
    """
    What was said on the phone recently, where anything was.

    Wrapped because the voice app is optional in a way the CRM is not: a
    deployment that has never placed a call should not have WhatsApp replies
    fail on an import, and a call row that predates summaries should not
    render as an empty quote in the prompt.
    """
    try:
        from apps.voice.models import CallSession
    except Exception:  # noqa: BLE001 - voice is not a hard dependency of the CRM
        return []

    sessions = (
        CallSession.objects.filter(contact=contact)
        .exclude(summary="")
        .order_by("-created_at")[:MAX_CALLS]
    )
    return [
        {
            "id": session.pk,
            "direction": session.direction,
            "outcome": session.outcome,
            "ended_at": session.ended_at,
            "summary": session.summary,
        }
        for session in sessions
    ]


def for_conversation(conversation) -> CustomerContext:
    """The customer on the other end of a thread. The commonest entry point."""
    if conversation is None:
        return CustomerContext()
    return build(getattr(conversation, "contact", None), conversation=conversation)
