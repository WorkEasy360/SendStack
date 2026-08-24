"""
The AI agent: what it is told, what it is allowed to answer, and when it stops.

The rule this file is built around is that the agent should be *reluctant*.
Every decision below -- staying out of a chat a human owns, refusing when the
knowledge base does not cover the question, handing over on its own signal,
counting its own replies -- exists because an AI answering a customer in a
business's name is the business's answer, and a confident invention costs
more than a slow human.
"""

import logging

from django.conf import settings
from django.utils import timezone

from . import knowledge
from .models import AIAgentConfig, AIConversationState, AIUsageLog
from .providers.anthropic import AIProviderError, complete

logger = logging.getLogger("multisender")

# The model is told to answer with exactly this when it cannot help. A
# sentinel is used rather than guessing from the wording because "I'm not
# sure, but..." and "I'm not sure -- let me check" mean opposite things, and a
# phrase-matching heuristic gets that wrong in both directions.
HANDOFF_TOKEN = "[[HANDOFF]]"

_BASE_INSTRUCTIONS = """You are a customer support agent replying inside WhatsApp on behalf of a business.

How to write:
- Reply in the language the customer wrote in.
- Keep it short. Two or three sentences is usually right; this is a chat, not an email.
- No markdown, no headings, no bullet symbols. Plain sentences only.
- Never invent prices, dates, stock, order status, refunds or policies.
- Never promise anything on the business's behalf that the reference material does not state.

When you cannot help -- the question is outside the reference material, the customer is upset,
they ask for a human, or answering would need account-specific information you do not have --
reply with exactly {handoff} and nothing else. Handing over is always better than guessing."""

_KNOWLEDGE_ONLY = """
Answer only from the reference material below. If it does not contain the answer, reply with exactly {handoff}."""

_NO_KNOWLEDGE_FOUND = """
There is no reference material for this question."""


def _api_key(config) -> str | None:
    return config.get_api_key() or getattr(settings, "AI_API_KEY", "") or None


def is_configured(config=None, tenant_id=None) -> bool:
    """Whether the agent could actually run: enabled, and with a key to run with."""
    config = config or AIAgentConfig.for_tenant(tenant_id)
    return bool(config.is_enabled and _api_key(config))


def build_system_prompt(config, chunks) -> str:
    parts = [_BASE_INSTRUCTIONS.format(handoff=HANDOFF_TOKEN)]

    if config.business_context.strip():
        parts.append(f"\nAbout this business:\n{config.business_context.strip()}")

    if config.answer_from_knowledge_only:
        parts.append(_KNOWLEDGE_ONLY.format(handoff=HANDOFF_TOKEN))

    if chunks:
        references = "\n\n".join(
            f"[{chunk.document.title}]\n{chunk.content}" for chunk in chunks
        )
        parts.append(f"\nReference material:\n{references}")
    elif config.answer_from_knowledge_only:
        parts.append(_NO_KNOWLEDGE_FOUND)

    return "\n".join(parts)


def build_history(conversation, limit):
    """
    The recent thread, oldest first, in the shape the API expects.

    Our outbound messages are the assistant's turns and the contact's are the
    user's, which is what lets the model follow "how much is the blue one?"
    after "we sell shirts". Media-only messages become a short placeholder so
    the turn is not silently missing from the conversation.
    """
    from apps.messaging.models import Message

    recent = list(
        Message.objects.filter(conversation=conversation)
        .exclude(status=Message.Status.FAILED)
        .order_by("-queued_at", "-id")[:limit]
    )
    recent.reverse()

    history = []
    for message in recent:
        role = "user" if message.direction == Message.Direction.INBOUND else "assistant"
        text = (message.body or message.caption or "").strip()
        if not text and message.media_url:
            text = "(sent an attachment)"
        if not text:
            continue
        # The API rejects two turns in a row from the same role, which happens
        # naturally here whenever somebody sends three messages in a row.
        if history and history[-1]["role"] == role:
            history[-1]["content"] += f"\n{text}"
        else:
            history.append({"role": role, "content": text})

    # A conversation must start with the customer, or the model is being asked
    # to continue something it never said.
    while history and history[0]["role"] == "assistant":
        history.pop(0)
    return history


def _log(config, conversation, outcome, response=None, error=""):
    AIUsageLog.objects.create(
        tenant_id=config.tenant_id, conversation=conversation, model=config.model, outcome=outcome,
        input_tokens=getattr(response, "input_tokens", 0) or 0,
        output_tokens=getattr(response, "output_tokens", 0) or 0,
        latency_ms=getattr(response, "latency_ms", 0) or 0,
        error=error[:500],
    )


def _ask(config, system, messages, *, max_tokens=None):
    return complete(
        api_key=_api_key(config),
        model=config.model,
        system=system,
        messages=messages,
        max_tokens=max_tokens or config.max_output_tokens,
        temperature=config.temperature,
    )


# ---------------------------------------------------------------------------
# Answering a customer
# ---------------------------------------------------------------------------

def default_knowledge_base(tenant_id):
    """
    The base a tenant's documents belong to when they never made one.

    Created on first need rather than at signup, so a tenant who never uses
    the knowledge base never acquires one, and so this works for every tenant
    that existed before knowledge bases were named.
    """
    from .models import KnowledgeBase

    existing = KnowledgeBase.objects.filter(tenant_id=tenant_id).order_by("-is_default", "pk").first()
    if existing:
        return existing
    return KnowledgeBase.objects.create(tenant_id=tenant_id, name="General", is_default=True)


def should_answer(conversation, config) -> tuple[bool, str]:
    """Whether the agent may take this conversation, and why not when it may not."""
    if not config.is_enabled:
        return False, "disabled"
    if not _api_key(config):
        return False, "no api key"

    # `reply_scope` is the setting a user actually chose; `reply_when_assigned`
    # is the older boolean and still overrides it upward, so a config saved
    # before scopes existed keeps behaving exactly as it did.
    scope = getattr(config, "reply_scope", AIAgentConfig.ReplyScope.UNASSIGNED)
    if conversation.assigned_agent_id and not config.reply_when_assigned:
        if scope != AIAgentConfig.ReplyScope.EVERY:
            # Somebody is already on this. Two answers to one question, one of
            # them a bot's, is worse than a slightly slower human.
            return False, "a human agent owns this conversation"

    if scope == AIAgentConfig.ReplyScope.OUTSIDE_HOURS:
        from apps.teams.services import is_open_now

        if is_open_now(conversation.tenant_id):
            return False, "the team is open, so a person will answer"

    state = AIConversationState.objects.filter(conversation=conversation).first()
    if state and state.handed_off_at:
        return False, "already handed over"
    if state and config.max_replies_per_conversation and state.replies_sent >= config.max_replies_per_conversation:
        return False, "reply limit reached"
    return True, ""


def hand_off(conversation, config, reason: str) -> None:
    """
    Stop answering, tell the customer, and put the chat in front of a person.

    The state row is written before the message goes out: if the send fails,
    the agent must still be out of the conversation, because the alternative
    is a bot that keeps trying after it has already given up once.
    """
    state, _ = AIConversationState.objects.get_or_create(conversation=conversation)
    if state.handed_off_at:
        return

    state.handed_off_at = timezone.now()
    state.handoff_reason = reason[:200]
    state.save(update_fields=["handed_off_at", "handoff_reason"])

    if not conversation.assigned_agent_id:
        from apps.teams.services import auto_assign

        auto_assign(conversation)

    # The conversation is waiting on a person again from this moment, so the
    # response clock starts here rather than at the customer's message the
    # agent was still trying to answer.
    from apps.teams.services import on_inbound

    on_inbound(conversation)

    if config.handoff_message.strip():
        from apps.automation.services import send_automated_message
        from apps.messaging.models import Message

        send_automated_message(
            conversation.whatsapp_account, conversation.contact,
            {"message_type": Message.MessageType.TEXT, "body": config.handoff_message.strip()},
            conversation=conversation, reference=f"ai:handoff:{conversation.pk}", kind=Message.Kind.SERVICE,
        )

    logger.info("AI handed conversation %s to a person (%s).", conversation.pk, reason)


#: What the customer is told when the knowledge base has nothing, per
#: `AIAgentConfig.Fallback`. HANDOFF is absent because it sends the config's
#: own handoff message instead.
_FALLBACK_TEXT = {
    AIAgentConfig.Fallback.SAY_UNKNOWN: (
        "I'm sorry — I don't have that information to hand. Is there anything else I can help with?"
    ),
    AIAgentConfig.Fallback.ASK_SUPPORT: (
        "I'm sorry, I couldn't find that in our information. Please contact our support team and "
        "they'll be able to help."
    ),
    AIAgentConfig.Fallback.NOTIFY_TEAM: (
        "Thanks for asking — I've passed this to our team and someone will get back to you shortly."
    ),
}


def _record_answer(config, conversation, inbound_message, retrieval, *, question, answer_text="",
                   message=None, answered=False, fallback="", is_test=False):
    """Write the provenance row. Never raises -- a lost audit row is not worth losing a reply over."""
    from .models import KnowledgeAnswer

    try:
        return KnowledgeAnswer.objects.create(
            tenant_id=config.tenant_id,
            knowledge_base_id=config.knowledge_base_id,
            conversation=conversation,
            contact=getattr(conversation, "contact", None) if conversation else None,
            message=message,
            question=question[:4000],
            answer=(answer_text or "")[:8000],
            confidence=retrieval.confidence,
            relevance=round(retrieval.relevance, 4),
            sources=retrieval.sources,
            answered=answered,
            fallback_used=fallback,
            is_test=is_test,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not record the knowledge answer for conversation %s", getattr(conversation, "pk", None))
        return None


def _send_fallback(conversation, config, retrieval, inbound_message, question) -> bool:
    """
    Tell the customer we cannot answer, without the model.

    This is the cost-control path as well as the honesty one: when retrieval
    found nothing relevant, calling the model would produce either a refusal
    we can write ourselves or an invention we do not want, and either way it
    would be billed.

    Always returns **False**, because `answer()` reports whether the agent
    *answered the question* -- and neither a handoff nor "I don't know" is an
    answer. The customer is still spoken to; the caller is told the question
    went unanswered.
    """
    action = getattr(config, "fallback_action", AIAgentConfig.Fallback.HANDOFF)

    if action == AIAgentConfig.Fallback.HANDOFF:
        if config.handoff_enabled:
            hand_off(conversation, config, "the knowledge base does not cover this")
            _record_answer(config, conversation, inbound_message, retrieval, question=question, fallback=action)
            return False
        # Handing over is switched off, so the customer would otherwise get
        # silence. Saying we don't know is the honest floor.
        action = AIAgentConfig.Fallback.SAY_UNKNOWN

    if action == AIAgentConfig.Fallback.NOTIFY_TEAM:
        _notify_team(conversation, question)

    from apps.automation.services import send_automated_message
    from apps.messaging.models import Message

    body = _FALLBACK_TEXT.get(action, _FALLBACK_TEXT[AIAgentConfig.Fallback.SAY_UNKNOWN])
    sent = send_automated_message(
        conversation.whatsapp_account, conversation.contact,
        {"message_type": Message.MessageType.TEXT, "body": body},
        conversation=conversation, reference=f"ai:fallback:{conversation.pk}", kind=Message.Kind.SERVICE,
    )
    _record_answer(
        config, conversation, inbound_message, retrieval,
        question=question, answer_text=body, message=sent, fallback=action,
    )
    logger.info("Knowledge base had nothing for conversation %s; used fallback %s.", conversation.pk, action)
    return False


#: Most people told about one unanswered question. A notification everybody
#: gets is a notification nobody reads.
MAX_NOTIFIED = 5


def _notify_team(conversation, question) -> None:
    """
    Put an unanswered question in front of a person without answering it.

    Whoever owns the conversation if anybody does; otherwise the team. It
    deliberately does not depend on auto-assignment, which most tenants leave
    off -- "notify a team member" that quietly notifies nobody because the
    routing policy is OFF is the worst kind of setting.
    """
    try:
        from apps.notifications.models import Notification
        from apps.notifications.services import notify
        from apps.teams.models import TeamMember
        from apps.teams.services import auto_assign

        if not conversation.assigned_agent_id:
            auto_assign(conversation)
            conversation.refresh_from_db(fields=["assigned_agent"])

        recipients = []
        if conversation.assigned_agent:
            recipients = [conversation.assigned_agent]
        else:
            members = (
                TeamMember.objects.filter(tenant_id=conversation.tenant_id, is_available=True)
                .select_related("user")
                .order_by("-role", "pk")[:MAX_NOTIFIED]
            )
            recipients = [member.user for member in members if member.user_id]

        for recipient in recipients:
            notify(
                recipient,
                kind=Notification.Kind.SYSTEM,
                title="A customer asked something the knowledge base could not answer",
                body=question[:300],
                link=f"/inbox?conversation={conversation.pk}",
            )
    except Exception:  # noqa: BLE001 - a missed notification must not cost the reply
        logger.exception("Could not notify the team about conversation %s", conversation.pk)


#: Topic -> the words that suggest it, and the tag applied when tagging is on.
#: A closed list, matched on plain words: "what did customers ask about this
#: week" is a genuinely useful question, and answering it with a model call
#: per message would cost more than the answers are worth.
TOPICS = (
    ("pricing", ("price", "pricing", "cost", "rate", "charges", "how much", "quotation", "quote"), "Pricing enquiry"),
    ("refund", ("refund", "return", "money back", "cancel my order", "cancellation"), "Refund enquiry"),
    ("warranty", ("warranty", "guarantee", "repair", "replacement"), "Warranty enquiry"),
    ("delivery", ("delivery", "shipping", "courier", "dispatch", "track my order"), "Delivery enquiry"),
    ("product", ("product", "catalogue", "catalog", "specification", "features", "in stock", "available"), "Product enquiry"),
    ("support", ("not working", "broken", "issue", "problem", "complaint", "faulty"), "Support issue"),
)

#: Topics worth a lead-score nudge, because somebody asking is somebody buying.
_BUYING_TOPICS = {"pricing", "product"}


def detect_topic(question: str):
    """The topic a question is about, or None. Deterministic, no model call."""
    lowered = f" {(question or '').lower()} "
    for key, words, label in TOPICS:
        if any(word in lowered for word in words):
            return key, label
    return None


def _log_crm(config, conversation, question, retrieval) -> None:
    """
    Put the question on the customer's CRM timeline, and optionally tag them.

    Two separate switches because they are two different intrusions: writing
    a timeline entry is a record of something that happened, while adding a
    tag changes how the contact is segmented and campaigned to. The first is
    on by default, the second is not.
    """
    if not getattr(config, "crm_logging_enabled", False):
        return
    contact = getattr(conversation, "contact", None)
    if contact is None:
        return

    topic = detect_topic(question)
    try:
        from apps.crm import activity
        from apps.crm.models import Activity

        label = topic[1] if topic else "Asked the assistant a question"
        activity.record(
            contact, Activity.Kind.AUTOMATION, label,
            metadata={
                "question": question[:300],
                "topic": topic[0] if topic else "",
                "confidence": retrieval.confidence,
                "sources": [source["title"] for source in retrieval.sources][:5],
            },
        )

        if topic and getattr(config, "crm_tagging_enabled", False):
            from apps.contacts.models import ContactTag

            tag, _ = ContactTag.objects.get_or_create(
                tenant_id=contact.tenant_id, name=topic[1], defaults={"color": "#2563eb"},
            )
            contact.tags.add(tag)

        if topic and topic[0] in _BUYING_TOPICS:
            from apps.crm.services import rescore

            rescore(contact)
    except Exception:  # noqa: BLE001 - CRM bookkeeping never costs a customer their answer
        logger.exception("Could not record the knowledge question on contact %s", getattr(contact, "pk", None))


def answer(conversation, inbound_message, *, config=None) -> bool:
    """
    Produce and send one reply. Returns whether the agent said anything.

    `config` lets a caller override the tenant's agent for this one reply --
    a workflow AI_REPLY node naming a different knowledge base, for instance.
    Left alone it is the tenant's own configuration.

    The order below is the whole safety design:

      1. May the agent speak at all? (scope, handoff, reply limit)
      2. What does the knowledge base actually have? (retrieval + relevance)
      3. **If it has nothing, answer without the model.** No call is made, so
         there is nothing for the model to invent and nothing to be billed for.
      4. Only then generate, from the retrieved text and nothing else.

    Every early return leaves the conversation exactly as it was, so the
    normal automation path (keyword rules, workflows) is never disturbed by
    the agent declining to speak.
    """
    config = config or AIAgentConfig.for_tenant(conversation.tenant_id)
    permitted, reason = should_answer(conversation, config)
    if not permitted:
        if reason == "reply limit reached" and config.handoff_enabled:
            hand_off(conversation, config, reason)
        else:
            logger.info("AI declined conversation %s: %s.", conversation.pk, reason)
        return False

    question = (inbound_message.body or "").strip()
    if not question:
        return False

    # Somebody asking for a person gets one, without a model call and without
    # being told what the documents say instead.
    if config.handoff_enabled and _wants_a_human(question):
        hand_off(conversation, config, "the customer asked for a person")
        return False

    retrieval = knowledge.retrieve(
        conversation.tenant_id, question,
        knowledge_base_id=config.knowledge_base_id, limit=config.knowledge_chunks,
    )
    _log_crm(config, conversation, question, retrieval)

    floor = max(float(getattr(config, "min_relevance", 0) or 0), 0.0)
    if config.answer_from_knowledge_only and (not retrieval or retrieval.relevance < floor):
        return _send_fallback(conversation, config, retrieval, inbound_message, question)

    system = build_system_prompt(config, retrieval.chunks)
    history = build_history(conversation, config.history_messages)
    if not history:
        history = [{"role": "user", "content": question}]

    try:
        response = _ask(config, system, history)
    except AIProviderError as exc:
        # A provider outage must not leave the customer with silence: hand the
        # conversation to a person, which is what would have happened had the
        # agent been switched off in the first place.
        logger.warning("AI call failed for conversation %s: %s", conversation.pk, exc)
        _log(config, conversation, AIUsageLog.Outcome.ERROR, error=str(exc))
        if config.handoff_enabled:
            hand_off(conversation, config, "the assistant was unavailable")
        return False

    text = (response.text or "").strip()
    if not text or HANDOFF_TOKEN in text:
        _log(config, conversation, AIUsageLog.Outcome.HANDOFF, response=response)
        # The model read the retrieved material and still could not answer,
        # which is exactly the case the fallback exists for.
        return _send_fallback(conversation, config, retrieval, inbound_message, question)

    from apps.automation.services import send_automated_message
    from apps.messaging.models import Message

    sent = send_automated_message(
        conversation.whatsapp_account, conversation.contact,
        {"message_type": Message.MessageType.TEXT, "body": text},
        conversation=conversation, reference=f"ai:reply:{conversation.pk}", kind=Message.Kind.SERVICE,
    )
    if sent is None:
        _log(config, conversation, AIUsageLog.Outcome.ERROR, response=response, error="the reply could not be queued")
        return False

    state, _ = AIConversationState.objects.get_or_create(conversation=conversation)
    AIConversationState.objects.filter(pk=state.pk).update(
        replies_sent=state.replies_sent + 1, last_reply_at=timezone.now(),
    )
    _log(config, conversation, AIUsageLog.Outcome.ANSWERED, response=response)
    _record_answer(
        config, conversation, inbound_message, retrieval,
        question=question, answer_text=text, message=sent, answered=True,
    )
    return True


#: Phrases that mean "stop talking to the robot". Matched before retrieval so
#: the request costs nothing and is never answered with a document quote.
_HUMAN_REQUESTS = (
    "speak to someone", "speak to a human", "talk to someone", "talk to a human", "talk to a person",
    "real person", "customer care", "customer service", "human agent", "speak to an agent",
    "talk to an agent", "connect me", "put me through", "agent please",
)


def _wants_a_human(question: str) -> bool:
    lowered = f" {question.lower()} "
    return any(phrase in lowered for phrase in _HUMAN_REQUESTS)


def ask(tenant_id, question: str, *, knowledge_base_id=None, config=None) -> dict:
    """
    Answer a question from the knowledge base **without sending anything**.

    This is what the test panel runs, and it is the same retrieval and the
    same prompt the customer-facing path uses -- a test that exercised a
    different code path would be worse than no test, because it would build
    confidence in behaviour that is not the behaviour.
    """
    config = config or AIAgentConfig.for_tenant(tenant_id)
    retrieval = knowledge.retrieve(
        tenant_id, question,
        knowledge_base_id=knowledge_base_id if knowledge_base_id is not None else config.knowledge_base_id,
        limit=config.knowledge_chunks,
    )

    result = {
        "question": question,
        "confidence": retrieval.confidence,
        "relevance": round(retrieval.relevance, 3),
        "sources": retrieval.sources,
        "excerpts": [
            {"citation": hit.chunk.citation, "content": hit.chunk.content[:600]} for hit in retrieval.hits
        ],
        "answer": "",
        "answered": False,
        "detail": "",
    }

    floor = max(float(getattr(config, "min_relevance", 0) or 0), 0.0)
    if not retrieval or retrieval.relevance < floor:
        result["detail"] = (
            "Nothing in this knowledge base is close enough to answer that. On WhatsApp the customer would "
            "get your fallback instead."
        )
        _record_answer(config, None, None, retrieval, question=question, is_test=True)
        return result

    if not _api_key(config):
        result["detail"] = (
            "Found relevant information, but no API key is configured, so no answer could be written."
        )
        _record_answer(config, None, None, retrieval, question=question, is_test=True)
        return result

    system = build_system_prompt(config, retrieval.chunks)
    try:
        response = _ask(config, system, [{"role": "user", "content": question}])
    except AIProviderError as exc:
        _log(config, None, AIUsageLog.Outcome.ERROR, error=str(exc))
        result["detail"] = str(exc)
        _record_answer(config, None, None, retrieval, question=question, is_test=True)
        return result

    text = (response.text or "").strip()
    _log(config, None, AIUsageLog.Outcome.ANSWERED, response=response)
    if not text or HANDOFF_TOKEN in text:
        result["detail"] = (
            "The assistant read the material and decided it could not answer, so the customer would get "
            "your fallback."
        )
        _record_answer(config, None, None, retrieval, question=question, is_test=True)
        return result

    result["answer"] = text
    result["answered"] = True
    _record_answer(config, None, None, retrieval, question=question, answer_text=text, answered=True, is_test=True)
    return result


def maybe_reply(inbound_message) -> bool:
    """
    Entry point from the automation receiver.

    Queued rather than run inline: a model call takes seconds, and the webhook
    that delivered this message is waiting on the response.
    """
    conversation = inbound_message.conversation
    if conversation is None or not is_configured(tenant_id=conversation.tenant_id):
        return False

    from .tasks import generate_reply_task

    generate_reply_task.delay(inbound_message.pk)
    return True


# ---------------------------------------------------------------------------
# Helping an agent, rather than replacing one
# ---------------------------------------------------------------------------

def suggest_reply(conversation) -> str:
    """
    Draft a reply for an agent to read, edit, and choose to send.

    Deliberately not subject to `should_answer`: an agent asking for a
    suggestion has already decided the agent should be involved, and the
    conversation being assigned to them is the reason they are asking.
    """
    config = AIAgentConfig.for_tenant(conversation.tenant_id)
    if not _api_key(config):
        raise AIProviderError("No API key is configured for the AI agent.")

    history = build_history(conversation, config.history_messages)
    if not history:
        raise AIProviderError("There is nothing in this conversation to reply to yet.")

    last_customer_message = next(
        (turn["content"] for turn in reversed(history) if turn["role"] == "user"), "",
    )
    chunks = knowledge.search(conversation.tenant_id, last_customer_message, limit=config.knowledge_chunks)

    system = build_system_prompt(config, chunks) + (
        "\n\nYou are drafting a reply for a human agent to review before it is sent. "
        f"Write the reply itself and nothing else. If you cannot draft one, reply with exactly {HANDOFF_TOKEN}."
    )

    try:
        response = _ask(config, system, history)
    except AIProviderError as exc:
        _log(config, conversation, AIUsageLog.Outcome.ERROR, error=str(exc))
        raise

    _log(config, conversation, AIUsageLog.Outcome.SUGGESTED, response=response)
    text = (response.text or "").strip()
    if not text or HANDOFF_TOKEN in text:
        raise AIProviderError("The assistant does not have enough to go on for this conversation.")
    return text


def summarise(conversation) -> str:
    """A few lines an agent can read instead of scrolling a long thread."""
    config = AIAgentConfig.for_tenant(conversation.tenant_id)
    if not _api_key(config):
        raise AIProviderError("No API key is configured for the AI agent.")

    # A summary is worth more the longer the thread, so it reads further back
    # than a reply does.
    history = build_history(conversation, max(config.history_messages, 40))
    if not history:
        raise AIProviderError("There is nothing in this conversation to summarise yet.")

    transcript = "\n".join(
        f"{'Customer' if turn['role'] == 'user' else 'Us'}: {turn['content']}" for turn in history
    )
    system = (
        "Summarise this WhatsApp support conversation for an agent picking it up cold. "
        "Cover what the customer wants, what has been said, and what is still outstanding. "
        "Three or four short lines, plain text, no markdown. Do not invent anything that is not in the transcript."
    )

    try:
        response = _ask(config, system, [{"role": "user", "content": transcript}], max_tokens=300)
    except AIProviderError as exc:
        _log(config, conversation, AIUsageLog.Outcome.ERROR, error=str(exc))
        raise

    _log(config, conversation, AIUsageLog.Outcome.SUMMARISED, response=response)
    return (response.text or "").strip()
