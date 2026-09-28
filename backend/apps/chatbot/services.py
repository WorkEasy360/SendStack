"""
The operations a chatbot performs on the rest of the platform.

Everything in here either reads existing platform state (is this number
connected, is this bot within its hours, what does the tenant's AI agent look
like) or performs an operation the rest of the platform already understands
(assign this conversation, hand it to a person, add this contact to a
campaign). The engine calls these; they call the apps that own the data.

The direction of that dependency is the point. ``apps.chatbot`` knows about
``apps.inbox``, ``apps.ai``, ``apps.teams`` and ``apps.campaigns``; none of them
knows about it. The one exception is the single rung added to the automation
receiver, which reaches this app through a function call and no imports at
module level -- see ``apps/automation/receivers.py``.
"""

import logging
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from .models import Chatbot, ChatbotHandoff, ChatbotSession, ChatbotVersion, ChatbotWorkingHours

logger = logging.getLogger("sendstack")


# ---------------------------------------------------------------------------
# Which bot answers
# ---------------------------------------------------------------------------

def live_chatbot_for(account):
    """
    The published bot on this number, or None.

    One query, hitting the ``(whatsapp_account, status)`` index, run once per
    inbound message. ``published_version`` is selected in the same query
    because the engine needs it immediately and a second round trip on the
    hottest path in the feature is a round trip for nothing.
    """
    if account is None:
        return None
    return (
        Chatbot.objects
        .filter(whatsapp_account=account, status=Chatbot.Status.PUBLISHED)
        .select_related("published_version", "knowledge_base", "whatsapp_account")
        .first()
    )


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def snapshot(chatbot, flow=None) -> dict:
    """
    Everything about this bot that changes what it says, as plain data.

    Taken from the live rows, so a snapshot always reflects what the editor
    showed. ``flow`` is passed in rather than read from a column because the
    flow lives on the *draft version* -- a bot's graph is edited by writing a
    draft, not by writing the bot.
    """
    from .matching import as_dict

    return {
        "flow": flow if flow is not None else {"nodes": []},
        "rules": [as_dict(rule) for rule in chatbot.rules.all().order_by("priority", "id")],
        "settings": {
            "bot_type": chatbot.bot_type,
            "welcome_message": chatbot.welcome_message,
            "fallback_message": chatbot.fallback_message,
            "unsupported_message": chatbot.unsupported_message,
            "outside_hours_message": chatbot.outside_hours_message,
            "ai_enabled": chatbot.ai_enabled,
            "knowledge_base_id": chatbot.knowledge_base_id,
            "system_prompt": chatbot.system_prompt,
            "persona": chatbot.persona,
            "language": chatbot.language,
            "max_ai_response_chars": chatbot.max_ai_response_chars,
            "history_messages": chatbot.history_messages,
            "min_relevance": chatbot.min_relevance,
            "response_delay_min_seconds": chatbot.response_delay_min_seconds,
            "response_delay_max_seconds": chatbot.response_delay_max_seconds,
            "human_handoff_enabled": chatbot.human_handoff_enabled,
            "handoff_message": chatbot.handoff_message,
            "handoff_conditions": chatbot.handoff_conditions or {},
            "working_hours_enabled": chatbot.working_hours_enabled,
            "conversation_timeout_minutes": chatbot.conversation_timeout_minutes,
            "defer_to_automation": chatbot.defer_to_automation,
        },
        "working_hours": [
            {
                "weekday": row.weekday, "is_open": row.is_open,
                "opens_at": row.opens_at.strftime("%H:%M"), "closes_at": row.closes_at.strftime("%H:%M"),
            }
            for row in chatbot.working_hours.all()
        ],
    }


def draft_version(chatbot) -> ChatbotVersion:
    """
    The version currently being edited.

    A draft is any version that has never been published. There is at most one:
    editing the flow rewrites it rather than making another, so a morning of
    fiddling produces one draft and not forty versions nobody wants in the
    rollback list.
    """
    existing = chatbot.versions.filter(published_at__isnull=True).order_by("-version").first()
    if existing is not None:
        return existing

    highest = chatbot.versions.order_by("-version").values_list("version", flat=True).first() or 0
    base = chatbot.published_version
    return ChatbotVersion.objects.create(
        chatbot=chatbot,
        version=highest + 1,
        # A new draft starts from what is live, not from nothing -- otherwise
        # the first edit after a publish silently discards the whole flow.
        flow=(base.flow if base else {"nodes": []}),
        rules=[],
        settings={},
        working_hours=[],
    )


@transaction.atomic
def publish(chatbot, *, user=None, notes: str = "") -> ChatbotVersion:
    """
    Freeze the current configuration and make it the one answering customers.

    The draft is filled in from the live rows at this moment and stamped, and
    the bot's status moves to PUBLISHED. Wrapped in a transaction because the
    version row and the pointer to it must move together: a bot pointing at a
    half-written version would answer customers out of it.
    """
    version = draft_version(chatbot)
    data = snapshot(chatbot, flow=version.flow)

    version.rules = data["rules"]
    version.settings = data["settings"]
    version.working_hours = data["working_hours"]
    version.notes = (notes or "")[:300]
    version.published_at = timezone.now()
    version.published_by = user
    version.save()

    chatbot.published_version = version
    chatbot.status = Chatbot.Status.PUBLISHED
    chatbot.save(update_fields=["published_version", "status", "updated_at"])
    logger.info("Chatbot %s published version %s.", chatbot.pk, version.version)
    return version


@transaction.atomic
def rollback(chatbot, version: ChatbotVersion, *, user=None) -> ChatbotVersion:
    """
    Put a previously published version back in front of customers.

    The old version is pointed at rather than copied forward, so the version
    list stays a record of what was actually live and when -- copying would
    make v5 and v7 identical rows with no way to say which one the customer on
    Tuesday was talking to.
    """
    if version.chatbot_id != chatbot.pk:
        raise ValueError("That version belongs to a different chatbot.")
    if version.published_at is None:
        raise ValueError("That version was never published.")

    chatbot.published_version = version
    chatbot.status = Chatbot.Status.PUBLISHED
    chatbot.save(update_fields=["published_version", "status", "updated_at"])
    logger.info("Chatbot %s rolled back to version %s.", chatbot.pk, version.version)
    return version


def settings_of(version, chatbot=None):
    """
    The behaviour a running session should use.

    A published version's frozen settings when there is one, and the live
    chatbot's own fields otherwise -- which is what the rule tester and the
    preview need, since they run against a draft nobody has published.

    Returns something with attribute access, because everything downstream
    (``outbound.delay_for``, ``ai_reply``) was written against a Chatbot and
    should not have to learn a second shape.
    """
    if version is not None and version.settings:
        return _FrozenSettings(version.settings, chatbot)
    return chatbot


class _FrozenSettings:
    """A published snapshot, wearing the Chatbot interface the engine expects."""

    __slots__ = ("_data", "_chatbot")

    def __init__(self, data: dict, chatbot):
        self._data = data or {}
        self._chatbot = chatbot

    def __getattr__(self, name):
        if name in self._data:
            return self._data[name]
        # Anything the snapshot does not carry -- the id, the tenant, the
        # account -- comes from the live row. Those are identity, not
        # behaviour, and freezing them would point a running session at a bot
        # that has since been moved to another number.
        return getattr(self._chatbot, name)

    def __repr__(self):
        return f"<frozen settings for chatbot {getattr(self._chatbot, 'pk', None)}>"


# ---------------------------------------------------------------------------
# Business hours
# ---------------------------------------------------------------------------

def is_within_hours(chatbot, version=None, now=None) -> bool:
    """
    Whether this bot is on duty.

    Answered in the tenant's own timezone -- the same one quiet hours and the
    team's working hours use, so a business is never simultaneously open by one
    clock and closed by another. A bot with hours enabled but no rows saved
    counts as always open: enabling the switch and saving nothing has not
    expressed a closing time.
    """
    config = settings_of(version, chatbot)
    if not getattr(config, "working_hours_enabled", False):
        return True

    rows = _hours_rows(chatbot, version)
    if not rows:
        return True

    from apps.compliance.services import tenant_zone

    local = (now or timezone.now()).astimezone(tenant_zone(chatbot.tenant_id))
    today = rows.get(local.weekday())
    if today is None or not today["is_open"]:
        return False

    opens, closes = today["opens_at"], today["closes_at"]
    if opens <= closes:
        return opens <= local.time() < closes
    # A window that wraps past midnight, e.g. a night shift.
    return local.time() >= opens or local.time() < closes


def _hours_rows(chatbot, version) -> dict:
    """``{weekday: {...}}``, from the frozen snapshot where there is one."""
    import datetime

    def _time(value):
        if isinstance(value, datetime.time):
            return value
        hour, _, minute = str(value).partition(":")
        return datetime.time(int(hour), int(minute or 0))

    if version is not None and version.working_hours:
        return {
            int(row["weekday"]): {
                "is_open": bool(row.get("is_open", True)),
                "opens_at": _time(row.get("opens_at", "09:00")),
                "closes_at": _time(row.get("closes_at", "18:00")),
            }
            for row in version.working_hours
        }
    return {
        row.weekday: {"is_open": row.is_open, "opens_at": row.opens_at, "closes_at": row.closes_at}
        for row in ChatbotWorkingHours.objects.filter(chatbot=chatbot)
    }


# ---------------------------------------------------------------------------
# Handing over
# ---------------------------------------------------------------------------

#: The handoff conditions, and what each does when nothing has been configured.
#: Defaults are the cautious ones: a customer who asks for a person always gets
#: one, and a bot that has failed three times in a row stops trying.
DEFAULT_HANDOFF_CONDITIONS = {
    "customer_request": True,
    "repeated_fallback": 3,
    "negative_sentiment": False,
    "keywords": "",
    "max_bot_replies": 0,
    "tool_failure": False,
    "ai_unavailable": True,
}


def handoff_rules(config) -> dict:
    """The bot's handoff conditions, with every unset key filled from the defaults."""
    stored = getattr(config, "handoff_conditions", None) or {}
    return {**DEFAULT_HANDOFF_CONDITIONS, **{k: v for k, v in stored.items() if k in DEFAULT_HANDOFF_CONDITIONS}}


def hand_off(ctx, *, trigger, detail: str = "", message=None) -> bool:
    """
    Stop answering, tell the customer, and put the chat in front of a person.

    The state is written *before* the message goes out, for the reason
    ``ai.services.hand_off`` does the same: if the send fails, the bot must
    still be out of the conversation. A bot that keeps trying after it has
    already given up once is worse than one that goes quiet.
    """
    session = ctx.session
    config = ctx.chatbot_settings

    if session.handed_off_at is not None:
        return False

    now = timezone.now()
    ChatbotSession.objects.filter(pk=session.pk).update(
        state=ChatbotSession.State.HUMAN_ACTIVE,
        handed_off_at=now,
        handoff_reason=(detail or trigger)[:200],
        delivery_token=session.delivery_token + 1,   # cancels anything still pending
    )
    session.state = ChatbotSession.State.HUMAN_ACTIVE
    session.handed_off_at = now
    session.handoff_reason = (detail or trigger)[:200]
    session.delivery_token += 1

    handoff = ChatbotHandoff.objects.create(
        tenant_id=session.tenant_id, conversation=ctx.conversation, chatbot=ctx.chatbot,
        session=session, trigger=trigger, detail=detail[:300],
    )

    if not ctx.conversation.assigned_agent_id:
        try:
            from apps.teams.services import auto_assign

            auto_assign(ctx.conversation)
            ctx.conversation.refresh_from_db(fields=["assigned_agent"])
            if ctx.conversation.assigned_agent_id:
                ChatbotHandoff.objects.filter(pk=handoff.pk).update(
                    assigned_to_id=ctx.conversation.assigned_agent_id,
                )
        except Exception:  # noqa: BLE001 - routing must not cost the handoff
            logger.exception("Could not auto-assign conversation %s after a handoff.", ctx.conversation.pk)

    # The conversation is waiting on a person from this moment, so the response
    # clock restarts here rather than at the message the bot was still trying
    # to answer.
    try:
        from apps.teams.services import on_inbound

        on_inbound(ctx.conversation)
    except Exception:  # noqa: BLE001
        logger.exception("Could not restart the response clock for conversation %s.", ctx.conversation.pk)

    text = message if message is not None else getattr(config, "handoff_message", "")
    if (text or "").strip():
        from . import outbound, variables

        outbound.send_now(
            ctx,
            outbound.text_spec(variables.render(text, ctx.render_context())),
            reference=f"chatbot:handoff:{ctx.conversation.pk}",
        )

    _notify_agents(ctx, trigger)
    ctx.trace.step(
        ctx.trace_steps.HANDOFF, detail or "Handed to a person.", trigger=trigger,
        assigned_to=ctx.conversation.assigned_agent_id,
    )
    _publish(session.tenant_id, "chatbot.handoff", {
        "conversation": ctx.conversation.pk, "chatbot": getattr(ctx.chatbot, "pk", None),
        "trigger": trigger, "state": ChatbotSession.State.HUMAN_ACTIVE,
    })
    return True


def _notify_agents(ctx, trigger) -> None:
    """Tell whoever now owns the conversation that it is theirs. Never raises."""
    try:
        from apps.notifications.models import Notification
        from apps.notifications.services import notify

        recipient = ctx.conversation.assigned_agent
        if recipient is None:
            return
        notify(
            recipient,
            kind=Notification.Kind.SYSTEM,
            title="A chatbot handed you a conversation",
            body=f"{ctx.contact.name or ctx.contact.phone} needs a person ({trigger.replace('_', ' ').lower()}).",
            link=f"/chatbot/inbox?conversation={ctx.conversation.pk}",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not notify an agent about conversation %s.", ctx.conversation.pk)


def take_over(conversation, user) -> ChatbotSession:
    """
    A person takes the conversation off the bot.

    Bumps the delivery token, which is what cancels a reply the bot has already
    composed and scheduled. Without that, an agent could take over and then
    watch the bot say something over the top of them two seconds later.
    """
    session = ensure_session(conversation)
    now = timezone.now()
    ChatbotSession.objects.filter(pk=session.pk).update(
        state=ChatbotSession.State.HUMAN_ACTIVE,
        handed_off_at=session.handed_off_at or now,
        handoff_reason=session.handoff_reason or "An agent took over.",
        delivery_token=session.delivery_token + 1,
    )
    session.refresh_from_db()

    ChatbotHandoff.objects.create(
        tenant_id=session.tenant_id, conversation=conversation, chatbot=session.chatbot,
        session=session, trigger=ChatbotHandoff.Trigger.AGENT_TAKEOVER,
        detail=f"{getattr(user, 'username', 'An agent')} took over.", assigned_to=user,
    )
    if not conversation.assigned_agent_id and user is not None:
        conversation.assigned_agent = user
        conversation.save(update_fields=["assigned_agent", "updated_at"])

    _publish(session.tenant_id, "chatbot.state", {
        "conversation": conversation.pk, "state": session.state,
    })
    return session


def resume_bot(conversation, user=None) -> ChatbotSession:
    """
    Hand the conversation back to the bot.

    Clears the handoff and returns the session to BOT_ACTIVE. The open handoff
    rows are closed at the same time, which is what makes "how long do agents
    hold a handed-over chat?" answerable.
    """
    session = ensure_session(conversation)
    now = timezone.now()
    ChatbotSession.objects.filter(pk=session.pk).update(
        state=ChatbotSession.State.BOT_ACTIVE,
        handed_off_at=None,
        handoff_reason="",
        returned_to_bot_at=now,
        # A bot resuming starts from a clean slate: the three failures that got
        # it handed over must not hand it straight back on the next message.
        consecutive_fallbacks=0,
        delivery_token=session.delivery_token + 1,
    )
    ChatbotHandoff.objects.filter(session=session, returned_at__isnull=True).update(returned_at=now)
    session.refresh_from_db()

    _publish(session.tenant_id, "chatbot.state", {
        "conversation": conversation.pk, "state": session.state,
    })
    logger.info("Conversation %s returned to the bot by %s.", conversation.pk, getattr(user, "username", "system"))
    return session


def resolve_session(session, resolution) -> ChatbotSession:
    """Mark a session finished, by the bot or by a person."""
    ChatbotSession.objects.filter(pk=session.pk).update(
        state=ChatbotSession.State.RESOLVED, resolution=resolution, ended_at=timezone.now(),
        delivery_token=session.delivery_token + 1,
    )
    session.refresh_from_db()
    return session


def transfer(ctx, chatbot_id) -> bool:
    """Move this conversation onto a different bot, and start it there."""
    target = Chatbot.objects.filter(
        pk=chatbot_id, tenant_id=ctx.session.tenant_id, status=Chatbot.Status.PUBLISHED,
    ).select_related("published_version").first()
    if target is None:
        ctx.trace.failed(ctx.trace_steps.ACTION, "The chatbot to transfer to is not published.")
        return False

    ChatbotSession.objects.filter(pk=ctx.session.pk).update(
        chatbot=target, version=target.published_version,
        current_node_id="", awaiting_node_id="", consecutive_fallbacks=0,
        state=ChatbotSession.State.BOT_ACTIVE,
    )
    ctx.session.refresh_from_db()
    ctx.trace.step(ctx.trace_steps.ACTION, f"Transferred to '{target.name}'.", chatbot_id=target.pk)
    return True


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def ensure_session(conversation, chatbot=None) -> ChatbotSession:
    """
    This conversation's session, created if it has none.

    ``get_or_create`` rather than a plain create, because two workers can be
    handed two messages from the same customer within milliseconds of each
    other and the OneToOne is what stops them both making one.
    """
    session, created = ChatbotSession.objects.get_or_create(
        conversation=conversation,
        defaults={
            "tenant_id": conversation.tenant_id,
            "chatbot": chatbot,
            "version": getattr(chatbot, "published_version", None),
        },
    )
    if not created and chatbot is not None and session.chatbot_id is None:
        ChatbotSession.objects.filter(pk=session.pk).update(
            chatbot=chatbot, version=chatbot.published_version,
        )
        session.chatbot = chatbot
        session.version = chatbot.published_version
    return session


def has_timed_out(session, config, now=None) -> bool:
    """
    Whether the customer has been silent long enough to start again.

    Measured from the last thing *either side* said, not just the customer's
    message: a bot that answered at 11pm and got no reply has a conversation
    that went cold at 11pm, and treating a message the next afternoon as a
    continuation of it produces a bot that answers a question from yesterday.
    """
    minutes = int(getattr(config, "conversation_timeout_minutes", 0) or 0)
    if minutes <= 0:
        return False
    last = max(
        [moment for moment in (session.last_customer_message_at, session.last_bot_message_at) if moment],
        default=None,
    )
    if last is None:
        return False
    return (now or timezone.now()) - last > timezone.timedelta(minutes=minutes)


def restart_session(session, chatbot) -> ChatbotSession:
    """Begin a fresh conversation on the same row, keeping the customer's own facts."""
    ChatbotSession.objects.filter(pk=session.pk).update(
        chatbot=chatbot,
        version=getattr(chatbot, "published_version", None),
        state=ChatbotSession.State.NEW,
        resolution=ChatbotSession.Resolution.UNRESOLVED,
        current_node_id="", awaiting_node_id="",
        # Variables are conversation-scoped by definition, so a new
        # conversation starts without them. Anything meant to outlive this was
        # written to the contact by a CUSTOMER-scoped SET_VARIABLE.
        variables={},
        summary="", summary_message_count=0,
        consecutive_fallbacks=0, ai_replies=0, bot_replies=0,
        handed_off_at=None, handoff_reason="", ended_at=None,
        delivery_token=session.delivery_token + 1,
    )
    session.refresh_from_db()
    return session


# ---------------------------------------------------------------------------
# Reaching the rest of the platform
# ---------------------------------------------------------------------------

def assign(conversation, agent_id=None) -> None:
    """Put a conversation in front of a specific person, or let the routing policy choose."""
    from apps.inbox.services import assign_agent

    if agent_id:
        from django.contrib.auth import get_user_model

        agent = get_user_model().objects.filter(
            pk=agent_id, tenant_id=conversation.tenant_id, is_active=True,
        ).first()
        if agent is not None:
            assign_agent(conversation, agent)
        return

    from apps.teams.services import auto_assign

    auto_assign(conversation)


def add_to_campaign(campaign_id, contact, *, tenant_id) -> bool:
    """
    Put one contact into a campaign's audience.

    Adding a recipient, never launching: a rule that launched a bulk send
    because one person typed a keyword would turn a single inbound message into
    ten thousand outbound ones.
    """
    from apps.campaigns.models import Campaign

    campaign = Campaign.objects.filter(pk=campaign_id, tenant_id=tenant_id).first()
    if campaign is None:
        return False
    if hasattr(campaign, "contacts"):
        if campaign.contacts.filter(pk=contact.pk).exists():
            return False
        campaign.contacts.add(contact)
        return True
    logger.warning("Campaign %s has no direct contact audience to add to.", campaign_id)
    return False


def effective_ai_config(chatbot, tenant_id):
    """
    The AI configuration one chatbot answers with.

    The tenant's own ``AIAgentConfig`` provides the things that are genuinely
    tenant-wide -- the provider, the API key, the model, the tool grants, the
    knowledge-only policy -- and the chatbot overrides the things that are
    per-bot: which documents, how it sounds, how long the answer may be, how
    much history it reads.

    Crucially this returns an **unsaved copy**. Mutating the tenant's row to
    carry one bot's prompt would change every other bot in the workspace, and
    the row is read on other paths concurrently.
    """
    from apps.ai.models import AIAgentConfig

    base = AIAgentConfig.for_tenant(tenant_id)
    if chatbot is None:
        return base

    # `copy` rather than a new instance: an unsaved default has no pk, and a
    # saved one must not be re-saved from here.
    import copy as _copy

    config = _copy.copy(base)
    config.pk = base.pk

    knowledge_base_id = getattr(chatbot, "knowledge_base_id", None)
    if knowledge_base_id:
        config.knowledge_base_id = knowledge_base_id

    config.history_messages = int(getattr(chatbot, "history_messages", base.history_messages) or base.history_messages)
    config.min_relevance = float(getattr(chatbot, "min_relevance", base.min_relevance))
    config.handoff_enabled = bool(getattr(chatbot, "human_handoff_enabled", base.handoff_enabled))
    if (getattr(chatbot, "handoff_message", "") or "").strip():
        config.handoff_message = chatbot.handoff_message

    # The chatbot answers when the *chatbot* says it may, which the engine has
    # already decided by the time the model is called. Leaving the tenant-wide
    # scope in place here would make a bot go silent on an assigned
    # conversation the agent had explicitly handed back to it.
    config.reply_scope = AIAgentConfig.ReplyScope.EVERY
    config.reply_when_assigned = True
    # The engine owns the reply limit (``handoff_conditions.max_bot_replies``),
    # counted on the session. Two limits counted in two places would hand over
    # at whichever fired first, which is a bug nobody could explain.
    config.max_replies_per_conversation = 0
    return config


def ai_available(tenant_id) -> bool:
    """Whether this tenant's plan includes the AI agent at all."""
    from apps.ai.services import agent_available

    return agent_available(tenant_id)


def _publish(tenant_id, event_type, data) -> None:
    """Announce a state change on the tenant's live stream. Never raises."""
    try:
        from apps.realtime import events

        events.publish(tenant_id, event_type, data)
    except Exception:  # noqa: BLE001
        logger.debug("Could not publish %s.", event_type, exc_info=True)


# ---------------------------------------------------------------------------
# Previewing a draft
# ---------------------------------------------------------------------------

#: A number no real customer can hold, so a preview conversation can never
#: collide with a live one. +999 is a reserved ITU code assigned to nobody.
PREVIEW_PHONE = "+9990000000"

#: Most turns one preview may replay. A preview rebuilds the whole conversation
#: from scratch on every message (see ``simulate``), so this bounds both the
#: work one request does and the number of AI calls a long test can run up.
MAX_PREVIEW_TURNS = 25


class _Discard(Exception):
    """Carries a finished preview out of the transaction that is about to die."""

    def __init__(self, payload):
        super().__init__("preview finished")
        self.payload = payload


def simulate(chatbot, turns, *, version=None) -> dict:
    """
    Replay a transcript through the real engine and keep none of it.

    **Why replay rather than remember.** A conversation's state -- which node it
    is parked at, what it has learned, how many times it has spoken -- lives in
    ``ChatbotSession``, and a preview must not leave sessions, conversations,
    contacts or messages behind in a workspace's real inbox. So the whole
    transcript is replayed from the first message on every request, inside a
    transaction that is deliberately rolled back. Turn three genuinely follows
    turn two because turn two just ran, not because anything was stored.

    The cost is honest and bounded: an *n*-message preview runs *n* turns, which
    is why ``MAX_PREVIEW_TURNS`` exists and why an AI node is called again on
    each replay. For a person testing a flow in a builder that is the right
    trade -- the alternative is a fake conversation state that agrees with the
    engine right up until the day it does not.

    Nothing reaches a phone. The session carries ``variables.SIMULATION_KEY``,
    which ``outbound.send_now`` reads to capture the message into the trail
    instead of handing it to the send pipeline.
    """
    try:
        with transaction.atomic():
            raise _Discard(_run_preview(chatbot, turns, version=version))
    except _Discard as finished:
        # The savepoint is gone and with it every row below; the payload was
        # built in memory before the rollback, which is the only reason it can
        # be returned at all.
        return finished.payload


def _run_preview(chatbot, turns, *, version=None) -> dict:
    from apps.contacts.models import Contact
    from apps.inbox.models import Conversation
    from apps.messaging.models import Message

    from . import engine, variables
    from .models import ChatbotEvent

    version = version or draft_version(chatbot)
    account = chatbot.whatsapp_account

    contact = Contact.objects.create(
        tenant=chatbot.tenant, phone=PREVIEW_PHONE, name="Preview",
        created_by_id=getattr(account, "user_id", None),
    )
    conversation = Conversation.objects.create(
        tenant=chatbot.tenant, whatsapp_account=account, contact=contact,
    )
    session = ensure_session(conversation, chatbot)
    ChatbotSession.objects.filter(pk=session.pk).update(
        version=version, variables={variables.SIMULATION_KEY: True},
    )

    transcript, steps = [], []
    for index, turn in enumerate(turns[:MAX_PREVIEW_TURNS], start=1):
        text = str(turn.get("text") or "")
        button_id = str(turn.get("button_id") or "")

        inbound = Message.objects.create(
            tenant=chatbot.tenant, conversation=conversation, whatsapp_account=account,
            contact=contact, to_phone=contact.phone,
            direction=Message.Direction.INBOUND, message_type=Message.MessageType.TEXT,
            body=text, status=Message.Status.DELIVERED,
            replied_button_id=button_id,
            interaction_type=Message.Interaction.NATIVE_BUTTON if button_id else "",
            delivered_at=timezone.now(),
        )
        transcript.append({"role": "customer", "text": text, "button_id": button_id, "turn": index})

        correlation_id = f"preview{index:03d}" + uuid4().hex[:20]
        # The turn lock is a cache key on the conversation id, and this
        # conversation is one nothing else can reach -- so the lock is taken and
        # released per turn exactly as it is in production, with no contention.
        outcome = engine.handle(inbound.pk, correlation_id, preview=(chatbot, version))

        rows = list(
            ChatbotEvent.objects
            .filter(conversation=conversation, correlation_id=correlation_id)
            .order_by("created_at", "id")
        )
        for row in rows:
            steps.append({
                "turn": index, "step": row.step, "outcome": row.outcome,
                "detail": row.detail, "metadata": row.metadata, "duration_ms": row.duration_ms,
            })
            if row.step == ChatbotEvent.Step.SEND and row.metadata.get("simulated") and "body" in row.metadata:
                transcript.append({
                    "role": "bot", "turn": index,
                    "text": row.metadata.get("body") or "",
                    "buttons": row.metadata.get("buttons") or [],
                    "media": row.metadata.get("media") or [],
                })

        if outcome in ("HANDOFF", "END", "TRANSFER"):
            # The conversation is over for the bot. Replaying further messages
            # would exercise a path a real customer could not reach, which is
            # exactly the kind of false confidence a preview must not give.
            transcript.append({"role": "system", "turn": index, "text": _preview_note(outcome)})
            break

    session.refresh_from_db()
    return {
        "transcript": transcript,
        "steps": steps,
        "state": session.state,
        "current_node_id": session.current_node_id,
        "awaiting_node_id": session.awaiting_node_id,
        "variables": {
            name: value for name, value in (session.variables or {}).items()
            if not name.startswith("__")
        },
        "version": version.version,
    }


def _preview_note(outcome: str) -> str:
    return {
        "HANDOFF": "The bot handed this conversation to a person and stopped replying.",
        "END": "The bot ended the conversation.",
        "TRANSFER": "The conversation was transferred to another chatbot.",
    }.get(outcome, outcome)


# ---------------------------------------------------------------------------
# Copying and carrying a bot around
# ---------------------------------------------------------------------------

#: The export's own format marker and revision. Checked on import so a file
#: from a future version of this product fails with a sentence rather than by
#: half-loading a shape this code does not understand.
EXPORT_FORMAT = "sendstack.chatbot.flow"
EXPORT_VERSION = 1

#: Fields on an action that name a row in *this* deployment. Stripped on export
#: because they mean nothing anywhere else, and stripped again on import
#: because a file that carries one is either stale or hostile -- and an id that
#: happened to exist in the importing workspace would silently point a new bot
#: at somebody else's campaign.
_PORTABLE_STRIP = (
    "knowledge_base_id", "campaign_id", "workflow_id", "chatbot_id", "agent_id", "asset_key",
    "headers", "payload",
)


@transaction.atomic
def duplicate(chatbot, *, user=None) -> Chatbot:
    """
    Copy a bot's configuration, and none of what happened to it.

    The name has " (copy)" appended and a number after that if needed, because
    ``unique_chatbot_name_per_tenant`` is a real constraint and an
    IntegrityError is a worse answer than "Sales Bot (copy 2)".
    """
    source_id = chatbot.pk
    hours = list(chatbot.working_hours.all())
    rules = list(chatbot.rules.all())
    draft_flow = draft_version(chatbot).flow or {"nodes": []}

    copy = Chatbot.objects.get(pk=source_id)
    copy.pk = None
    copy._state.adding = True
    copy.name = _available_name(chatbot.tenant_id, chatbot.name)
    # Always a draft, never pointing at the original's published snapshot: a
    # version row belongs to the bot that published it, and two bots sharing
    # one would make a rollback on either of them move the other.
    copy.status = Chatbot.Status.DRAFT
    copy.published_version = None
    copy.created_by = user
    copy.save()

    ChatbotWorkingHours.objects.bulk_create([
        ChatbotWorkingHours(
            chatbot=copy, weekday=row.weekday, is_open=row.is_open,
            opens_at=row.opens_at, closes_at=row.closes_at,
        )
        for row in hours
    ])

    for rule in rules:
        rule.pk = None
        rule._state.adding = True
        rule.chatbot = copy
        # Counters describe the original's history, not the copy's.
        rule.match_count = 0
        rule.last_matched_at = None
        rule.save()

    new_draft = draft_version(copy)
    new_draft.flow = draft_flow
    new_draft.save(update_fields=["flow"])

    logger.info("Chatbot %s duplicated from %s.", copy.pk, source_id)
    return copy


def _available_name(tenant_id, name: str) -> str:
    base = f"{name} (copy)"[:150]
    if not Chatbot.objects.filter(tenant_id=tenant_id, name=base).exists():
        return base

    for suffix in range(2, 50):
        candidate = f"{name} (copy {suffix})"[:150]
        if not Chatbot.objects.filter(tenant_id=tenant_id, name=candidate).exists():
            return candidate
    # Fifty copies of one bot is not a case worth a clever answer; a timestamp
    # is unique and readable, which is all that is left to want.
    return f"{name} (copy {int(timezone.now().timestamp())})"[:150]


def export_flow(chatbot) -> dict:
    """
    The draft graph, portable and stripped of everything local.

    Node positions are kept -- a graph that arrives as a pile at the origin is
    a graph somebody has to lay out again -- and every id and credential is
    dropped. See ``_portable_node``.
    """
    flow = draft_version(chatbot).flow or {"nodes": []}
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": timezone.now().isoformat(),
        "chatbot": {
            "name": chatbot.name,
            "description": chatbot.description,
            "bot_type": chatbot.bot_type,
            "language": chatbot.language,
        },
        "nodes": [_portable_node(node) for node in flow.get("nodes") or []],
    }


def _portable_node(node: dict) -> dict:
    """One node with every local reference and secret removed."""
    actions = []
    for action in node.get("actions") or []:
        cleaned = {key: value for key, value in action.items() if key not in _PORTABLE_STRIP}
        actions.append(cleaned)
    return {**node, "actions": actions}


def import_flow(chatbot, payload) -> dict:
    """
    Load an exported graph into this bot's draft, or raise ``ValueError``.

    Three gates, in order, and the order is the point:

    1. the envelope is one this code understands;
    2. every node is stripped of ids and credentials again -- a file is not
       trusted to have been produced by ``export_flow``;
    3. ``flow.validate`` runs with **this** tenant, which is where every
       surviving reference is checked and where a graph that cannot run is
       refused.

    Nothing about the bot other than its draft graph is touched. An import that
    also applied the file's settings would silently switch somebody's live bot
    to another language or another bot type.
    """
    from . import flow as flow_module

    if not isinstance(payload, dict):
        raise ValueError("That is not a chatbot flow file.")
    if payload.get("format") != EXPORT_FORMAT:
        raise ValueError("That file is not a chatbot flow export.")
    try:
        file_version = int(payload.get("version") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("That file does not say which format version it is.") from exc
    if file_version > EXPORT_VERSION:
        raise ValueError(
            f"That file was exported by a newer version of this product (format {file_version})."
        )

    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("That file has no nodes.")

    stripped = [_portable_node(node) for node in nodes if isinstance(node, dict)]
    try:
        flow = flow_module.validate({"nodes": stripped}, tenant_id=chatbot.tenant_id, chatbot=chatbot)
    # Action validation runs inside flow validation and raises ActionError,
    # which is also a ValueError.  Imports must report that as a user-facing
    # invalid file rather than turning it into an internal server error.
    except ValueError as exc:
        raise ValueError(f"That flow cannot be imported: {exc}") from exc

    draft = draft_version(chatbot)
    draft.flow = flow
    draft.save(update_fields=["flow"])
    return flow
