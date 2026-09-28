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

from . import agent, knowledge, tools
from .models import AIAgentConfig, AIConversationState, AIUsageLog
from .providers.registry import AIProviderError, complete, credentials

logger = logging.getLogger("sendstack")

#: The plan switch every path in this module is behind.
FEATURE = "ai_agent"


def agent_available(tenant_id) -> bool:
    """
    Whether this tenant's plan includes the AI agent.

    Asked at *execution* time, in every one of the handful of functions below
    that can actually call a model -- not once at the top of the request. An
    agent configured while the feature was included must stop the moment it
    is not, and the request that starts a reply is very often not a request
    at all: it is a webhook, a Celery task, or a workflow node reached three
    steps after somebody sent a message.
    """
    from apps.subscriptions.services import feature_enabled

    return feature_enabled(tenant_id, FEATURE)


def require_agent(tenant_id) -> None:
    """The same question, raising the API error, for the paths a person is waiting on."""
    from apps.subscriptions.services import require_feature

    require_feature(tenant_id, FEATURE)


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

#: The strict form, used when AI_KNOWLEDGE_ONLY is on. It differs from the
#: instruction above in three ways that each close a specific hole seen in
#: practice:
#:
#: * It names pretrained knowledge and forbids it outright. "Answer only from
#:   the reference material" reads to a model as a preference, and a model that
#:   knows a plausible refund window will supply it.
#: * It says the reference material outranks the conversation. Without that, an
#:   agent that said "14 days" earlier in the thread keeps saying it after the
#:   document has been corrected to 19 -- the thread is right there in the
#:   context and looks like agreement with itself.
#: * It gives one exact sentence to refuse with, so a refusal is recognisable
#:   rather than a hedge the customer reads as a soft yes.
_STRICT_KNOWLEDGE_ONLY = """
You are a Knowledge Base assistant.

Answer the customer's question using ONLY the information in the reference material below.

- Do not use your own pretrained or general knowledge to answer any factual question.
- Do not guess, infer, estimate or fill a gap with something that sounds likely.
- Do not carry a fact over from earlier in this conversation. The reference material below is the
  only authority: where the conversation and the reference material disagree, the reference
  material is correct and what was said earlier was wrong.
- Facts about our products, prices, stock, policies, timings and procedures come from the
  reference material and from nowhere else.

If the reference material does not contain enough to answer, reply with exactly:
{refusal}

Do not add anything to that sentence, and do not follow it with a guess."""

#: The knowledge-only rule, restated for an agent that has tools. Without this
#: the two instructions contradict each other -- "answer only from the
#: reference material below" forbids using what `search_knowledge` just
#: returned, and a model resolving that conflict picks one at random. The
#: sources widen; the prohibition on inventing does not.
_KNOWLEDGE_ONLY_WITH_TOOLS = """
Answer only from the reference material below and from what your tools return to you. If neither
covers it, do not guess -- use the hand_to_human tool."""

#: How an agent with tools is told to use them. Deliberately short: a long
#: list of rules about when to call a tool competes with the tool
#: descriptions, which are where that belongs and where the model looks.
_TOOL_NOTE = """
You have tools. Use them when they would give you a real answer instead of a guess -- look
something up rather than inventing it, and check our records before saying you cannot help.
Do not narrate the tool call to the customer; just use it and answer. Never claim to have done
something unless the tool actually reported that it worked."""

#: The tool note for a strict agent. The ordinary one invites the model to
#: "look something up rather than inventing it", which is right but incomplete
#: here: it does not say what to do when the lookup comes back empty, and an
#: agent told only to prefer tools treats an empty result as permission to fall
#: back on itself. This closes that -- a tool that returns nothing is a reason
#: to refuse, not a reason to answer from memory.
_STRICT_TOOL_NOTE = """
You have tools. Use search_knowledge whenever the customer asks a factual question you have not
already been given the answer to, and lookup_customer for anything about this person's own record.

What a tool returns counts as reference material and may be used. What a tool does NOT return is
not available to you: if a search comes back empty, or returns nothing that answers the question,
you still cannot answer it from your own knowledge. Reply with exactly:
{refusal}

Never use a tool's silence as licence to guess, and never claim to have done something unless the
tool reported that it worked. Do not narrate tool calls to the customer."""

_NO_KNOWLEDGE_FOUND = """
There is no reference material for this question."""

#: The strict version of the same. Reached only by a caller that generates
#: without retrieval (`suggest_reply`, which drafts for a human to check);
#: `answer` never gets this far with nothing, because the relevance gate stops
#: it first. Spelled out anyway, so the one path that can reach it refuses
#: rather than improvises.
_STRICT_NO_KNOWLEDGE_FOUND = """
There is NO reference material for this question. You therefore cannot answer it. Reply with
exactly: {refusal}"""


def knowledge_only(config) -> bool:
    """
    Whether this reply must come from the knowledge base and nothing else.

    Deployment first, tenant second: `AI_KNOWLEDGE_ONLY` is the operator saying
    "no agent on this install invents anything", and a tenant switch that could
    turn that back off would make it a suggestion rather than a policy. With
    the deployment setting off this is exactly the tenant's own choice, which
    is the behaviour every existing install has.
    """
    if getattr(settings, "AI_KNOWLEDGE_ONLY", False):
        return True
    return bool(getattr(config, "answer_from_knowledge_only", False))


def relevance_floor(config) -> float:
    """
    How relevant retrieval must be before the model is called, 0-1.

    The tenant's own threshold, but never below the deployment's while strict
    mode is on -- otherwise "answer only from the documents" could be defeated
    by setting the tenant's own bar to zero, which is a setting a user can
    reach from a form.
    """
    tenant_floor = max(float(getattr(config, "min_relevance", 0) or 0), 0.0)
    if getattr(settings, "AI_KNOWLEDGE_ONLY", False):
        deployment_floor = max(float(getattr(settings, "AI_KNOWLEDGE_MIN_RELEVANCE", 0) or 0), 0.0)
        return max(tenant_floor, deployment_floor)
    return tenant_floor


def refusal_message(config) -> str:
    """The exact sentence a strict agent refuses with."""
    message = (getattr(config, "knowledge_only_message", "") or "").strip()
    return message or "I couldn't find this information in the Knowledge Base."


def is_refusal(text, config) -> bool:
    """
    Whether the model's reply is the strict refusal rather than an answer.

    This is the second half of knowledge-only enforcement, and it exists
    because the relevance gate cannot be the whole of it. The gate stops
    questions the documents plainly do not touch; it cannot stop a question
    that *looks* related. "What is the company warranty period?" shares
    "company" and "period" with a refund policy, scores well above any sane
    threshold, and reaches the model -- which is correct, because the only
    thing that can tell whether a refund policy answers a warranty question is
    something that can read them both.

    So the model is given one exact sentence to say when it cannot answer, and
    that sentence is recognised here and turned into a real refusal: the reply
    is not sent as an answer, nothing is recorded as answered, and the
    tenant's configured fallback runs. Matched on the sentence rather than
    guessed at from wording, for the reason `HANDOFF_TOKEN` exists -- "I'm not
    sure, but..." and "I'm not sure -- let me check" mean opposite things.
    """
    refusal = refusal_message(config).strip().lower().rstrip(".")
    cleaned = (text or "").strip().lower().rstrip(".")
    if not refusal or not cleaned:
        return False
    # `startswith` as well as equality: a model told to reply with exactly one
    # sentence will usually comply and occasionally add a courtesy after it.
    # Both are refusals, and neither should be relayed as an answer.
    return cleaned == refusal or cleaned.startswith(refusal)

#: How the agent is to treat what the CRM knows about the person it is
#: talking to. Spelled out because the two kinds of fact behave differently
#: and a model given both without being told will blur them: the customer
#: details are *verified records about this person* and may be used freely,
#: while anything about products, prices or policy still has to come from the
#: reference material. Without this note a knowledge-only agent refuses to
#: acknowledge a customer's own order, which reads as broken rather than safe.
_CUSTOMER_NOTE = """
What you know about this customer is recorded fact from our own records -- use it to answer
naturally and to judge what they need. It does not license you to invent anything about our
products, prices, stock or policies; those still come only from the reference material.
Never read this back to the customer as a list, never mention scores, internal statuses or
who owns the account, and never tell them what our team has been asked to do about them."""


def resolved(config):
    """
    The key, model and endpoint this config's next call will actually use.

    Everything that needs to know what the agent is configured with -- the send
    path, the "is it configured?" check, the status panel -- goes through here,
    so a tenant reading "gpt-4o-mini" on the dashboard is reading the model
    that will answer the next customer rather than the contents of a field.
    """
    return credentials(
        config.provider, api_key=config.get_api_key(), model=config.model, base_url=config.base_url,
    )


def _api_key(config) -> str | None:
    return resolved(config).api_key


def is_configured(config=None, tenant_id=None) -> bool:
    """Whether the agent could actually run: enabled, and with a key to run with."""
    config = config or AIAgentConfig.for_tenant(tenant_id)
    if config.provider == AIAgentConfig.Provider.CUSTOM and not config.base_url:
        return False
    return bool(config.is_enabled and _api_key(config))


def business_profile(tenant_id):
    """
    The tenant's structured account of itself, or an unsaved blank one.

    Never writes: this is read on the inbound-message path, and a read that
    creates a row is a read that can deadlock two workers answering the same
    customer at once.
    """
    from apps.tenants.models import BusinessProfile

    return BusinessProfile.for_tenant(tenant_id)


def customer_context(conversation):
    """
    Who the agent is talking to, assembled from the CRM.

    Wrapped in a guard because this is decoration on an answer, not the
    answer: a customer whose context cannot be built should still get a reply
    from the knowledge base, exactly as they did before this existed.
    """
    try:
        from apps.crm import context as crm_context

        return crm_context.for_conversation(conversation)
    except Exception:  # noqa: BLE001 - never cost a customer their answer
        logger.exception("Could not build customer context for conversation %s", getattr(conversation, "pk", None))
        return None


def build_system_prompt(config, chunks, *, customer=None, profile=None, with_tools=False) -> str:
    """
    Everything the agent is told before it sees the conversation.

    The order is the priority order the platform commits to: standing business
    rules first, then verified reference material, then what our records say
    about this customer, and only then the conversation itself. A model reads
    what it is given in order, and this is the order in which those sources
    should win when they disagree.

    `customer` and `profile` are optional so the two callers that legitimately
    have neither -- the admin test panel, which asks a question with no
    customer attached -- keep working unchanged.
    """
    parts = [_BASE_INSTRUCTIONS.format(handoff=HANDOFF_TOKEN)]

    # The structured profile is the business's own account of itself and comes
    # first; `business_context` is the older free-text box and is still
    # appended, so nothing a tenant typed before the profile existed is lost.
    if profile is not None:
        business = profile.to_prompt()
        if business:
            parts.append(f"\nAbout this business:\n{business}")

    if config.business_context.strip():
        parts.append(f"\nAbout this business:\n{config.business_context.strip()}")

    strict = getattr(settings, "AI_KNOWLEDGE_ONLY", False)
    if strict:
        # The strict instruction replaces both of the softer ones, including
        # the with-tools variant: what a tool returns is knowledge-base
        # material too (see `_STRICT_TOOL_NOTE`), so there is no conflict left
        # for a separate instruction to resolve.
        parts.append(_STRICT_KNOWLEDGE_ONLY.format(refusal=refusal_message(config)))
    elif config.answer_from_knowledge_only:
        parts.append(
            _KNOWLEDGE_ONLY_WITH_TOOLS if with_tools else _KNOWLEDGE_ONLY.format(handoff=HANDOFF_TOKEN)
        )

    if with_tools:
        parts.append(_STRICT_TOOL_NOTE.format(refusal=refusal_message(config)) if strict else _TOOL_NOTE)

    if customer:
        parts.append(f"\nWho you are talking to:\n{customer.to_prompt()}")
        parts.append(_CUSTOMER_NOTE)

    if chunks:
        # Each passage is labelled with its citation rather than just its
        # document title, so the model can say *which* document and page an
        # answer came from, and so two passages from one file are
        # distinguishable in the context.
        references = "\n\n".join(
            f"[{_reference_label(chunk)}]\n{chunk.content}" for chunk in chunks
        )
        parts.append(f"\nReference material:\n{references}")
    elif strict:
        parts.append(_STRICT_NO_KNOWLEDGE_FOUND.format(refusal=refusal_message(config)))
    elif config.answer_from_knowledge_only:
        parts.append(_NO_KNOWLEDGE_FOUND)

    return "\n".join(parts)


def _reference_label(chunk) -> str:
    """How one retrieved passage is named in the prompt. Never raises."""
    try:
        return chunk.citation or chunk.document.title
    except Exception:  # noqa: BLE001 - a missing document must not cost the reply
        return "reference"


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
    # The resolved model, not the stored field: with OPENAI_MODEL supplying it
    # the field is often blank, and a usage log that cannot say which model
    # spent the tokens is not a usage log.
    AIUsageLog.objects.create(
        tenant_id=config.tenant_id, conversation=conversation, model=resolved(config).model[:100], outcome=outcome,
        input_tokens=getattr(response, "input_tokens", 0) or 0,
        output_tokens=getattr(response, "output_tokens", 0) or 0,
        latency_ms=getattr(response, "latency_ms", 0) or 0,
        error=error[:500],
    )


def _ask(config, system, messages, *, max_tokens=None):
    settings_for_call = resolved(config)
    return complete(
        provider=config.provider,
        api_key=settings_for_call.api_key,
        model=settings_for_call.model,
        base_url=settings_for_call.base_url,
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

    # In strict mode the customer is told, in one plain sentence, that this is
    # not in the knowledge base -- rather than handed a friendlier phrasing
    # that leaves them unsure whether they were answered. Handing over is still
    # a handover; it is the *wording* of the "we don't know" replies that this
    # replaces, so "Ask support" and "Notify the team" keep doing their extra
    # work and only their text changes.
    if getattr(settings, "AI_KNOWLEDGE_ONLY", False) and action != AIAgentConfig.Fallback.HANDOFF:
        if action == AIAgentConfig.Fallback.NOTIFY_TEAM:
            _notify_team(conversation, question)

        from apps.automation.services import send_automated_message
        from apps.messaging.models import Message

        body = refusal_message(config)
        sent = send_automated_message(
            conversation.whatsapp_account, conversation.contact,
            {"message_type": Message.MessageType.TEXT, "body": body},
            conversation=conversation, reference=f"ai:fallback:{conversation.pk}", kind=Message.Kind.SERVICE,
        )
        _record_answer(
            config, conversation, inbound_message, retrieval,
            question=question, answer_text=body, message=sent, fallback=action,
        )
        logger.info(
            "Knowledge base had nothing for conversation %s; refused (knowledge-only).", conversation.pk,
        )
        return False

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


#: Openings that mean "still on the last subject": the question does not name
#: what it is about because the previous message already did.
_FOLLOW_UP_OPENERS = (
    "and ", "also ", "so ", "then ", "what about", "how about", "and what", "and how",
    "ok and", "okay and", "but ", "plus ",
)

#: Words that stand in for a subject named earlier. A question whose only noun
#: is one of these is not a question a retrieval can answer on its own.
_ANAPHORA = {"it", "that", "this", "them", "those", "these", "they", "one", "ones", "same"}


def _is_follow_up(question: str) -> bool:
    """
    Whether this question leans on the previous one for its subject.

    Deliberately narrow. Widening a retrieval with the conversation is useful
    for "and how long does it take?" and actively harmful for "do you sell
    helicopters?" -- the second is a complete question that the documents
    simply do not answer, and letting it ride on the previous message's terms
    would retrieve the *last* subject and answer a question nobody asked. That
    is a hallucination with a citation attached, which is worse than a refusal.
    So the signal has to be an explicit one, not "the question scored badly".
    """
    lowered = (question or "").strip().lower()
    if not lowered:
        return False
    if lowered.startswith(_FOLLOW_UP_OPENERS):
        return True
    # No opener, but no subject of its own either: every content word is a
    # stand-in for something named earlier.
    terms = knowledge.tokenize(lowered)
    return bool(terms) and len(terms) <= 4 and all(term in _ANAPHORA for term in terms)


def _previous_questions(conversation, current_message_id=None, limit=2) -> list:
    """
    The customer's own recent messages, newest last.

    Read from the messages directly rather than through `build_history`, which
    merges consecutive turns from the same role -- correct for a transcript,
    and wrong here, where three messages in a row from one customer are three
    separate attempts at a subject.

    Only their words: our own replies quote the knowledge base back, so
    including them would retrieve the passage already used instead of the one
    the follow-up is reaching for.
    """
    from apps.messaging.models import Message

    messages = (
        Message.objects.filter(conversation=conversation, direction=Message.Direction.INBOUND)
        .exclude(status=Message.Status.FAILED)
        .exclude(pk=current_message_id)
        .order_by("-queued_at", "-id")
        .values_list("body", flat=True)[:limit]
    )
    return [body.strip() for body in reversed(list(messages)) if (body or "").strip()]


def retrieve_in_context(conversation, question, config, *, current_message_id=None):
    """
    Retrieve for `question`, reading the thread when the question needs it.

    "And how long does processing take?" is perfectly clear to the person who
    asked it and almost meaningless to a retrieval: the subject is a message
    earlier. Searched alone it finds nothing, the relevance gate fires, and a
    customer mid-conversation is told their follow-up is not in the knowledge
    base -- which reads as the assistant losing the thread, because it is.

    The widened pass runs only for a question that actually leans on the one
    before it (see `_is_follow_up`) and only when it did badly alone, and its
    result is used only if it is better. So a self-contained question keeps
    exactly the retrieval it had before this existed.
    """
    retrieval = knowledge.retrieve(
        conversation.tenant_id, question,
        knowledge_base_id=config.knowledge_base_id, limit=config.knowledge_chunks,
    )
    if retrieval.relevance >= relevance_floor(config) or not _is_follow_up(question):
        return retrieval

    try:
        previous = _previous_questions(conversation, current_message_id)
    except Exception:  # noqa: BLE001 - context is a bonus, never the reply's price
        logger.exception("Could not read conversation %s for context retrieval", conversation.pk)
        return retrieval

    if not previous:
        return retrieval

    widened = knowledge.retrieve(
        conversation.tenant_id, f"{' '.join(previous)} {question}",
        knowledge_base_id=config.knowledge_base_id, limit=config.knowledge_chunks,
    )
    return widened if widened.relevance > retrieval.relevance else retrieval


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
    # Before `should_answer`, and before anything is read or logged: a plan
    # without the agent has no agent, so there is nothing here to decline.
    # This is the choke point every automated reply passes through -- the
    # inbound ladder, the Celery task, and a workflow's AI_REPLY node all
    # arrive here -- which is why it is the one that matters most.
    if not agent_available(conversation.tenant_id):
        logger.info(
            "AI did not answer conversation %s: the plan does not include the AI agent.", conversation.pk,
        )
        return False

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

    retrieval = retrieve_in_context(
        conversation, question, config, current_message_id=inbound_message.pk,
    )
    _log_crm(config, conversation, question, retrieval)

    # Whether this tenant's agent has any tools at all. Computed once because
    # it changes three things below: whether the relevance gate short-circuits,
    # what the model is told, and which loop runs.
    tool_definitions = tools.definitions(config)

    # The relevance gate: retrieval has to have found something this good
    # before the model is called at all. Below it there is nothing to answer
    # from, so a call could only produce an invention or a refusal we can write
    # ourselves -- and would be billed either way.
    floor = relevance_floor(config)
    strict = getattr(settings, "AI_KNOWLEDGE_ONLY", False)
    below_floor = not retrieval or retrieval.relevance < floor

    # Whether tools suspend the gate is exactly the AI_KNOWLEDGE_ONLY question.
    #
    # With it off, they do, and that is deliberate: a tool-enabled agent has
    # real sources beyond this one retrieval -- the customer's own record, a
    # second look at the knowledge base for a term the first query missed -- so
    # "the documents do not cover this message" does not imply "there is
    # nothing to find". The model is still told to hand over rather than guess,
    # `hand_to_human` is always available, and an empty answer still lands in
    # `_send_fallback` below.
    #
    # With it on, they do not. That reasoning is sound about *retrieval* and
    # wrong about *hallucination*: it leaves the model called with an empty
    # context and its own weights, trusting a prompt to stop it answering from
    # them. This is the "tools must not be used to bypass the knowledge base"
    # rule, and it is enforced here rather than in the prompt because a rule
    # the model is merely asked to follow is not a rule.
    gate_applies = knowledge_only(config) and (strict or not tool_definitions)
    if gate_applies and below_floor:
        return _send_fallback(conversation, config, retrieval, inbound_message, question)

    system = build_system_prompt(
        config, retrieval.chunks,
        customer=customer_context(conversation), profile=business_profile(conversation.tenant_id),
        with_tools=bool(tool_definitions),
    )
    history = build_history(conversation, config.history_messages)
    if not history:
        history = [{"role": "user", "content": question}]

    turn = None
    try:
        if tool_definitions:
            # Everything about ownership the tools will ever see, built from
            # the conversation row. Nothing the model returns can change it.
            turn = agent.run_turn(config, tools.for_conversation(conversation), system, history)
            response = turn
            text = turn.text
        else:
            response = _ask(config, system, history)
            text = (response.text or "").strip()
    except AIProviderError as exc:
        # A provider outage must not leave the customer with silence: hand the
        # conversation to a person, which is what would have happened had the
        # agent been switched off in the first place.
        logger.warning("AI call failed for conversation %s: %s", conversation.pk, exc)
        _log(config, conversation, AIUsageLog.Outcome.ERROR, error=str(exc))
        if config.handoff_enabled:
            hand_off(conversation, config, "the assistant was unavailable")
        return False

    # The agent asked, through a tool, to be taken off this conversation. Done
    # here rather than inside the tool so that it goes through the same
    # `hand_off` every other handoff does -- assignment, the response clock,
    # and the tenant's own handoff message.
    if turn is not None and turn.terminal == tools.HANDOFF:
        _log(config, conversation, AIUsageLog.Outcome.HANDOFF, response=turn)
        if config.handoff_enabled:
            hand_off(conversation, config, turn.terminal_reason or "the assistant asked for a person")
            _record_answer(
                config, conversation, inbound_message, retrieval,
                question=question, fallback=AIAgentConfig.Fallback.HANDOFF,
            )
            return False
        # Handing over is switched off, so the customer would otherwise get
        # silence from an agent that has just decided it cannot help.
        return _send_fallback(conversation, config, retrieval, inbound_message, question)

    if not text or HANDOFF_TOKEN in text or (strict and is_refusal(text, config)):
        _log(config, conversation, AIUsageLog.Outcome.HANDOFF, response=response)
        # The model read the retrieved material and still could not answer,
        # which is exactly the case the fallback exists for. In strict mode
        # that verdict most often arrives as the refusal sentence rather than
        # the handoff token, because the retrieved material was related enough
        # to reach the model and still did not contain the answer.
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
    require_agent(tenant_id)
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

    # The same gate the customer-facing path applies, read from the same two
    # helpers -- a test panel that used a laxer threshold would answer
    # questions here that go unanswered on WhatsApp, which is worse than not
    # having a test panel.
    floor = relevance_floor(config)
    if not retrieval or retrieval.relevance < floor:
        result["answer"] = refusal_message(config) if getattr(settings, "AI_KNOWLEDGE_ONLY", False) else ""
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

    system = build_system_prompt(config, retrieval.chunks, profile=business_profile(tenant_id))
    try:
        response = _ask(config, system, [{"role": "user", "content": question}])
    except AIProviderError as exc:
        _log(config, None, AIUsageLog.Outcome.ERROR, error=str(exc))
        result["detail"] = str(exc)
        _record_answer(config, None, None, retrieval, question=question, is_test=True)
        return result

    text = (response.text or "").strip()
    _log(config, None, AIUsageLog.Outcome.ANSWERED, response=response)
    strict = getattr(settings, "AI_KNOWLEDGE_ONLY", False)
    if not text or HANDOFF_TOKEN in text or (strict and is_refusal(text, config)):
        result["answer"] = refusal_message(config) if strict else ""
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

    # Checked here as well as inside `answer` so a plan without the agent
    # queues no work at all, rather than paying for a worker to find out.
    if not agent_available(conversation.tenant_id):
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
    require_agent(conversation.tenant_id)
    config = AIAgentConfig.for_tenant(conversation.tenant_id)
    if not _api_key(config):
        raise AIProviderError("No API key is configured for the AI agent.")

    history = build_history(conversation, config.history_messages)
    if not history:
        raise AIProviderError("There is nothing in this conversation to reply to yet.")

    last_customer_message = next(
        (turn["content"] for turn in reversed(history) if turn["role"] == "user"), "",
    )
    # Retrieved through the same gate the customer-facing path uses. A draft
    # built from material too weak to answer with is a draft that quietly
    # invents, and it is more dangerous than a live reply, not less: an agent
    # reading a fluent suggestion is being asked to spot a fabrication in
    # somebody else's words.
    suggestion_retrieval = knowledge.retrieve(
        conversation.tenant_id, last_customer_message,
        knowledge_base_id=config.knowledge_base_id, limit=config.knowledge_chunks,
    )
    chunks = suggestion_retrieval.chunks if suggestion_retrieval.relevance >= relevance_floor(config) else []

    system = build_system_prompt(
        config, chunks,
        customer=customer_context(conversation), profile=business_profile(conversation.tenant_id),
    ) + (
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
    require_agent(conversation.tenant_id)
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
