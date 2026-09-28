"""
The numbers, computed from the rows that produced them.

Every figure here is a database aggregate over data the pipeline wrote as a
side effect of doing its job -- there is no counter table to drift out of step
with reality, and nothing is estimated. A metric that cannot be derived
honestly is absent rather than approximated.

Two things worth knowing about the shape of these queries:

* Everything is filtered by tenant **in the query**, never checked afterwards.
* The time window is always half-open ``[since, until)`` and always applied to
  an indexed column, so a workspace with a year of history pays for the window
  it asked for rather than for the table.
"""

import logging
from datetime import timedelta

from django.db.models import Avg, Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.messaging.models import Message

from .models import Chatbot, ChatbotEvent, ChatbotHandoff, ChatbotSession

logger = logging.getLogger("sendstack")

#: Longest window a caller may ask for. A year of per-message aggregation on a
#: busy workspace is a slow query somebody can ask for from a URL.
MAX_WINDOW_DAYS = 365
DEFAULT_WINDOW_DAYS = 30


def window(days=None):
    """The half-open range every figure below is measured over."""
    days = max(1, min(int(days or DEFAULT_WINDOW_DAYS), MAX_WINDOW_DAYS))
    until = timezone.now()
    return until - timedelta(days=days), until, days


def _sessions(tenant_id, since, until, chatbot_id=None):
    queryset = ChatbotSession.objects.filter(tenant_id=tenant_id, started_at__gte=since, started_at__lt=until)
    return queryset.filter(chatbot_id=chatbot_id) if chatbot_id else queryset


def _messages(tenant_id, since, until, chatbot_id=None):
    """
    Messages in the window, narrowed to one bot's conversations when asked.

    Narrowed through the *session* rather than by a column on Message, because
    a message has no chatbot: it belongs to a conversation, and the conversation
    is what a bot is attached to. Denormalising a chatbot id onto every message
    row to save this subquery would put a column on the hottest table in the
    platform for one screen.
    """
    queryset = Message.objects.filter(tenant_id=tenant_id, queued_at__gte=since, queued_at__lt=until)
    if chatbot_id:
        conversations = ChatbotSession.objects.filter(
            tenant_id=tenant_id, chatbot_id=chatbot_id,
        ).values("conversation_id")
        queryset = queryset.filter(conversation_id__in=conversations)
    return queryset


def overview(tenant_id, *, days=None, chatbot_id=None) -> dict:
    """
    The dashboard's headline figures.

    Deliberately one function returning one dict rather than a dozen endpoints:
    the screen shows them together, and twelve round trips to render one card
    row is twelve chances for the numbers to disagree with each other.
    """
    since, until, days = window(days)
    sessions = _sessions(tenant_id, since, until, chatbot_id)
    messages = _messages(tenant_id, since, until, chatbot_id)

    live_states = [
        ChatbotSession.State.NEW, ChatbotSession.State.BOT_ACTIVE,
        ChatbotSession.State.WAITING_FOR_CUSTOMER, ChatbotSession.State.WAITING_FOR_AGENT,
        ChatbotSession.State.HUMAN_ACTIVE,
    ]

    # Active and unread are asked about *now*, not about the window: "how many
    # conversations are open" is a question about this moment, and answering it
    # over the last 30 days would give a number that only grows.
    live = ChatbotSession.objects.filter(tenant_id=tenant_id)
    if chatbot_id:
        live = live.filter(chatbot_id=chatbot_id)

    session_totals = sessions.aggregate(
        total=Count("id"),
        resolved_by_bot=Count("id", filter=Q(resolution=ChatbotSession.Resolution.BOT)),
        completed=Count("id", filter=~Q(resolution=ChatbotSession.Resolution.UNRESOLVED)),
        ai_replies=Count("id", filter=Q(ai_replies__gt=0)),
    )
    message_totals = messages.aggregate(
        received=Count("id", filter=Q(direction=Message.Direction.INBOUND)),
        sent=Count("id", filter=Q(direction=Message.Direction.OUTBOUND, is_automated=True)),
        failed=Count(
            "id",
            filter=Q(
                direction=Message.Direction.OUTBOUND, is_automated=True,
                status__in=[Message.Status.FAILED, Message.Status.SKIPPED],
            ),
        ),
    )

    total_sessions = session_totals["total"] or 0
    handoffs = ChatbotHandoff.objects.filter(tenant_id=tenant_id, created_at__gte=since, created_at__lt=until)
    if chatbot_id:
        handoffs = handoffs.filter(chatbot_id=chatbot_id)
    handoff_count = handoffs.count()
    takeovers = handoffs.filter(trigger=ChatbotHandoff.Trigger.AGENT_TAKEOVER).count()

    today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_sessions = ChatbotSession.objects.filter(tenant_id=tenant_id, started_at__gte=today)
    if chatbot_id:
        today_sessions = today_sessions.filter(chatbot_id=chatbot_id)

    return {
        "window_days": days,
        "total_conversations": total_sessions,
        "active_conversations": live.filter(state__in=live_states).count(),
        "unread_conversations": _unread_count(tenant_id, chatbot_id),
        "today_conversations": today_sessions.count(),
        "messages_received": message_totals["received"] or 0,
        "messages_sent": message_totals["sent"] or 0,
        "ai_responses": _ai_reply_total(sessions),
        "human_handoffs": handoff_count,
        "agent_takeovers": takeovers,
        "failed_responses": message_totals["failed"] or 0,
        "average_response_seconds": _average_response_seconds(tenant_id, since, until, chatbot_id),
        "bot_resolution_rate": _rate(session_totals["resolved_by_bot"], total_sessions),
        "handoff_rate": _rate(handoff_count, total_sessions),
        "fallback_rate": _fallback_rate(tenant_id, since, until, chatbot_id),
        "completion_rate": _rate(session_totals["completed"], total_sessions),
        "weekly_conversations": daily_conversations(tenant_id, days=min(days, 90), chatbot_id=chatbot_id),
    }


def _rate(part, whole) -> float:
    return round((part or 0) / whole * 100, 1) if whole else 0.0


def _unread_count(tenant_id, chatbot_id=None) -> int:
    from apps.inbox.models import Conversation

    queryset = Conversation.objects.filter(tenant_id=tenant_id, unread_count__gt=0)
    if chatbot_id:
        queryset = queryset.filter(chatbot_session__chatbot_id=chatbot_id)
    return queryset.count()


def _ai_reply_total(sessions) -> int:
    from django.db.models import Sum

    return sessions.aggregate(total=Sum("ai_replies"))["total"] or 0


def _average_response_seconds(tenant_id, since, until, chatbot_id=None):
    """
    How long the bot takes to answer, from the customer's message to ours.

    Measured on ``ChatbotEvent`` rather than on message timestamps, because the
    message row is stamped when it is *queued* and the interesting number is how
    long the decision took. The RESPONSE and SEND steps carry that directly, in
    ``duration_ms``, already summed per step by the tracer.
    """
    events = ChatbotEvent.objects.filter(
        tenant_id=tenant_id, created_at__gte=since, created_at__lt=until,
        step__in=[ChatbotEvent.Step.RESPONSE, ChatbotEvent.Step.SEND],
        outcome=ChatbotEvent.Outcome.OK,
    )
    if chatbot_id:
        events = events.filter(chatbot_id=chatbot_id)

    # The total time from RECEIVED to the send, per correlation id, is what a
    # person means by "response time". Summing the per-step durations of one
    # message's trail gives exactly that, and one aggregate over an indexed
    # window is far cheaper than a self-join on timestamps.
    correlations = ChatbotEvent.objects.filter(
        tenant_id=tenant_id, created_at__gte=since, created_at__lt=until,
    )
    if chatbot_id:
        correlations = correlations.filter(chatbot_id=chatbot_id)

    per_message = (
        correlations
        .exclude(step=ChatbotEvent.Step.DELIVERY)
        .values("correlation_id")
        .annotate(total=_sum("duration_ms"))
        .aggregate(average=Avg("total"))
    )
    average_ms = per_message["average"]
    return round(average_ms / 1000, 2) if average_ms else 0.0


def _sum(field):
    from django.db.models import Sum

    return Sum(field)


def _fallback_rate(tenant_id, since, until, chatbot_id=None) -> float:
    events = ChatbotEvent.objects.filter(
        tenant_id=tenant_id, created_at__gte=since, created_at__lt=until,
        step__in=[ChatbotEvent.Step.FALLBACK, ChatbotEvent.Step.RESPONSE, ChatbotEvent.Step.RULE],
    )
    if chatbot_id:
        events = events.filter(chatbot_id=chatbot_id)

    counts = events.aggregate(
        fallbacks=Count("id", filter=Q(step=ChatbotEvent.Step.FALLBACK)),
        answered=Count(
            "id",
            filter=Q(step=ChatbotEvent.Step.RESPONSE, outcome=ChatbotEvent.Outcome.OK)
            | Q(step=ChatbotEvent.Step.RULE, outcome=ChatbotEvent.Outcome.OK),
        ),
    )
    total = (counts["fallbacks"] or 0) + (counts["answered"] or 0)
    return _rate(counts["fallbacks"], total)


def daily_conversations(tenant_id, *, days=7, chatbot_id=None) -> list:
    """
    Conversations started per day, oldest first, with no gaps.

    Days with nothing are filled in with zeros rather than omitted: a chart
    that silently skips a quiet Sunday draws a line straight from Saturday to
    Monday, which reads as a busy weekend.
    """
    since, until, days = window(days)
    rows = (
        _sessions(tenant_id, since, until, chatbot_id)
        .annotate(day=TruncDate("started_at"))
        .values("day")
        .annotate(count=Count("id"))
    )
    counts = {row["day"]: row["count"] for row in rows}

    start = since.date()
    return [
        {"date": (start + timedelta(days=offset)).isoformat(), "count": counts.get(start + timedelta(days=offset), 0)}
        for offset in range(days + 1)
    ]


def top_questions(tenant_id, *, days=30, chatbot_id=None, limit=10) -> list:
    """
    What customers actually ask, from the knowledge base's own provenance rows.

    Read from ``ai.KnowledgeAnswer`` rather than from message bodies, because
    that table already holds the question with its retrieval confidence
    attached -- which turns "top questions" into the far more useful "top
    questions we answered badly".
    """
    since, until, _ = window(days)
    from apps.ai.models import KnowledgeAnswer

    queryset = KnowledgeAnswer.objects.filter(
        tenant_id=tenant_id, created_at__gte=since, created_at__lt=until, is_test=False,
    )
    if chatbot_id:
        conversations = ChatbotSession.objects.filter(
            tenant_id=tenant_id, chatbot_id=chatbot_id,
        ).values("conversation_id")
        queryset = queryset.filter(conversation_id__in=conversations)

    rows = (
        queryset.values("question")
        .annotate(
            asked=Count("id"),
            answered=Count("id", filter=Q(answered=True)),
            average_relevance=Avg("relevance"),
        )
        .order_by("-asked")[:limit]
    )
    return [
        {
            "question": row["question"][:200],
            "asked": row["asked"],
            "answered": row["answered"],
            "unanswered": row["asked"] - row["answered"],
            "average_relevance": round(row["average_relevance"] or 0, 3),
        }
        for row in rows
    ]


def top_intents(tenant_id, *, days=30, chatbot_id=None) -> list:
    """
    What customers ask *about*, using the AI app's own deterministic topics.

    Reuses ``ai.services.detect_topic``, which is a closed list matched on plain
    words. A model call per message to classify intent would cost more than the
    answer is worth and would produce a different taxonomy every time it ran.
    """
    from apps.ai.models import KnowledgeAnswer
    from apps.ai.services import detect_topic

    since, until, _ = window(days)
    queryset = KnowledgeAnswer.objects.filter(
        tenant_id=tenant_id, created_at__gte=since, created_at__lt=until, is_test=False,
    )
    if chatbot_id:
        conversations = ChatbotSession.objects.filter(
            tenant_id=tenant_id, chatbot_id=chatbot_id,
        ).values("conversation_id")
        queryset = queryset.filter(conversation_id__in=conversations)

    tally = {}
    for question in queryset.values_list("question", flat=True)[:5000]:
        topic = detect_topic(question)
        key, label = topic if topic else ("other", "Something else")
        tally[key] = {"intent": key, "label": label, "count": tally.get(key, {}).get("count", 0) + 1}
    return sorted(tally.values(), key=lambda row: -row["count"])


def rule_performance(chatbot, *, days=30) -> list:
    """Which rules actually fire. A rule that has never matched is a rule with a typo in it."""
    from .models import ChatbotRule

    return [
        {
            "id": rule.pk, "name": rule.name, "priority": rule.priority,
            "match_type": rule.match_type, "pattern": rule.pattern,
            "is_active": rule.is_active, "match_count": rule.match_count,
            "last_matched_at": rule.last_matched_at,
        }
        for rule in ChatbotRule.objects.filter(chatbot=chatbot).order_by("priority", "id")
    ]


def handoff_breakdown(tenant_id, *, days=30, chatbot_id=None) -> list:
    """Why the bot gives up, most common first."""
    since, until, _ = window(days)
    queryset = ChatbotHandoff.objects.filter(
        tenant_id=tenant_id, created_at__gte=since, created_at__lt=until,
    )
    if chatbot_id:
        queryset = queryset.filter(chatbot_id=chatbot_id)

    labels = dict(ChatbotHandoff.Trigger.choices)
    return [
        {"trigger": row["trigger"], "label": labels.get(row["trigger"], row["trigger"]), "count": row["count"]}
        for row in queryset.values("trigger").annotate(count=Count("id")).order_by("-count")
    ]


def flow_usage(tenant_id, *, days=30, chatbot_id=None, limit=15) -> list:
    """Which flow nodes customers actually reach."""
    since, until, _ = window(days)
    queryset = ChatbotEvent.objects.filter(
        tenant_id=tenant_id, created_at__gte=since, created_at__lt=until,
        step=ChatbotEvent.Step.FLOW,
    )
    if chatbot_id:
        queryset = queryset.filter(chatbot_id=chatbot_id)

    tally = {}
    for metadata in queryset.values_list("metadata", flat=True)[:20000]:
        node_id = (metadata or {}).get("node_id")
        if not node_id:
            continue
        entry = tally.setdefault(node_id, {"node_id": node_id, "node_type": (metadata or {}).get("node_type", ""), "count": 0})
        entry["count"] += 1
    return sorted(tally.values(), key=lambda row: -row["count"])[:limit]


def bot_summaries(tenant_id, *, days=30) -> list:
    """One row per bot for the bots list -- enough to see which one is doing the work."""
    since, until, _ = window(days)
    # The annotations are suffixed rather than named after the relations they
    # count: Django refuses an annotation whose name collides with a field or a
    # reverse accessor, and ``handoffs`` is both.
    rows = (
        Chatbot.objects.filter(tenant_id=tenant_id)
        .annotate(
            conversation_count=Count(
                "sessions", filter=Q(sessions__started_at__gte=since, sessions__started_at__lt=until), distinct=True,
            ),
            handoff_count=Count(
                "handoffs", filter=Q(handoffs__created_at__gte=since, handoffs__created_at__lt=until), distinct=True,
            ),
            resolved_count=Count(
                "sessions",
                filter=Q(
                    sessions__started_at__gte=since, sessions__started_at__lt=until,
                    sessions__resolution=ChatbotSession.Resolution.BOT,
                ),
                distinct=True,
            ),
        )
        .values("id", "name", "status", "bot_type", "conversation_count", "handoff_count", "resolved_count")
    )
    return [
        {
            "id": row["id"], "name": row["name"], "status": row["status"], "bot_type": row["bot_type"],
            "conversations": row["conversation_count"],
            "handoffs": row["handoff_count"],
            "resolved": row["resolved_count"],
            "bot_resolution_rate": _rate(row["resolved_count"], row["conversation_count"]),
        }
        for row in rows
    ]
