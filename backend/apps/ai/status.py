"""
One answer to "is the assistant actually working?".

Assembled in one place because the question is never really about one
component. An agent that is switched on with a valid key still says nothing if
every document failed to parse; a knowledge base full of ready documents still
answers nobody if the number they message is disconnected; a perfectly
configured tenant still sees silence if the automation ladder above the agent
is answering everything first. A screen that reports those four separately
leaves the person reading it to do the join, and they cannot.

Everything here is read-only and cheap: counts and the most recent few rows.
It is called from a dashboard that may poll, so it must never call a model,
never write, and never be the slowest thing on the page.
"""

from datetime import timedelta

from django.db.models import Count, Sum
from django.utils import timezone

from . import agent, embeddings, services, tools
from .models import AIAgentConfig, AIUsageLog, KnowledgeAnswer, KnowledgeBase, KnowledgeDocument

#: Recent failures worth showing. Enough to see a pattern, few enough to read.
RECENT_ERRORS = 5


def _key_source(config) -> str:
    """Where the key being used came from -- never the key itself."""
    if config.get_api_key():
        return "tenant"
    resolved = services.resolved(config)
    if not resolved.api_key:
        return "none"
    return "environment"


def agent_status(config) -> dict:
    """
    What the agent is configured with, and whether that is enough to run.

    `model` is the resolved one rather than the stored field, because with
    OPENAI_MODEL supplying it the field is usually blank and a status panel
    showing a blank model where a model is plainly working is worse than no
    panel.
    """
    resolved = services.resolved(config)
    ready = services.is_configured(config)

    problem = ""
    if not config.is_enabled:
        problem = "The agent is switched off."
    elif not resolved.api_key:
        problem = "No API key is configured, so the agent cannot answer anybody."
    elif config.provider == AIAgentConfig.Provider.CUSTOM and not config.base_url:
        problem = "This provider needs the endpoint's base URL before it can be reached."

    return {
        "is_enabled": config.is_enabled,
        "is_ready": ready,
        "status": "CONNECTED" if ready else ("ERROR" if config.is_enabled else "OFF"),
        "problem": problem,
        "provider": config.provider,
        "model": resolved.model,
        "base_url": resolved.base_url,
        "api_key_source": _key_source(config),
        "reply_scope": config.reply_scope,
        "fallback_action": config.fallback_action,
        "answer_from_knowledge_only": config.answer_from_knowledge_only,
        "display_name": config.display_name,
        # What the agent can do as well as say. Reported as the resolved list
        # rather than the three switches, because "which tools are live" is
        # the question being asked and reconstructing it from the flags is
        # how a panel ends up disagreeing with the send path.
        "tools": {
            "enabled": bool(config.tools_enabled),
            "available": tools.available(config),
            "capabilities": sorted(tools.granted(config)),
            "max_rounds": agent.max_rounds(config),
        },
    }


def knowledge_status(tenant_id, *, knowledge_base_id=None) -> dict:
    """Documents by state, chunks, and whether retrieval is hybrid or lexical."""
    documents = KnowledgeDocument.objects.filter(tenant_id=tenant_id)
    if knowledge_base_id is not None:
        documents = documents.filter(knowledge_base_id=knowledge_base_id)

    by_status = dict(documents.values_list("status").annotate(total=Count("id")).values_list("status", "total"))
    vectors = embeddings.stats(tenant_id, knowledge_base_id=knowledge_base_id)

    failed = list(
        documents.filter(status=KnowledgeDocument.Status.FAILED)
        .order_by("-updated_at")
        .values("id", "title", "original_filename", "error")[:RECENT_ERRORS]
    )

    return {
        "bases": KnowledgeBase.objects.filter(tenant_id=tenant_id).count(),
        "documents": documents.count(),
        "ready": by_status.get(KnowledgeDocument.Status.READY, 0),
        "pending": by_status.get(KnowledgeDocument.Status.PENDING, 0),
        "processing": by_status.get(KnowledgeDocument.Status.PROCESSING, 0),
        "failed": by_status.get(KnowledgeDocument.Status.FAILED, 0),
        "chunks": vectors["chunks"],
        "retrieval_mode": vectors["mode"],
        "embeddings": vectors,
        "failed_documents": failed,
    }


def whatsapp_status(tenant_id) -> dict:
    """
    Every number this tenant has, and whether the agent can answer on it.

    Read live from the accounts table rather than from any list the AI app
    keeps of its own, which is the whole reason a number connected this
    morning is answered by the agent this afternoon with nothing switched on
    and no code deployed. There is no per-account enrolment to forget.
    """
    from apps.whatsapp.models import WhatsAppAccount

    accounts = list(
        WhatsAppAccount.objects.filter(tenant_id=tenant_id)
        .order_by("-status", "name")
        .values("id", "name", "phone_number", "status", "connection_method", "provider", "last_seen_at")[:100]
    )
    connected = [account for account in accounts if account["status"] == WhatsAppAccount.Status.CONNECTED]
    return {
        "accounts": accounts,
        "total": len(accounts),
        "connected": len(connected),
        # Said explicitly because it is the thing tenants ask and the thing a
        # list of numbers does not by itself answer.
        "covered_by_agent": len(connected),
    }


def automation_status(tenant_id) -> dict:
    """Rules and workflows that could answer before the agent gets a turn."""
    from apps.automation.models import AutomationRule, FollowUpRule, Workflow

    active_rules = AutomationRule.objects.filter(whatsapp_account__tenant_id=tenant_id, is_active=True).count()
    active_workflows = Workflow.objects.filter(tenant_id=tenant_id, is_active=True).count()
    active_followups = FollowUpRule.objects.filter(whatsapp_account__tenant_id=tenant_id, is_active=True).count()
    return {
        "rules": active_rules,
        "workflows": active_workflows,
        "follow_ups": active_followups,
        "is_active": bool(active_rules or active_workflows or active_followups),
        # The order the inbound ladder actually runs in, so the panel can say
        # why a message the agent never saw was answered by something else.
        "order": ["consent", "welcome", "button", "keyword", "workflow", "ai"],
    }


def usage_status(tenant_id) -> dict:
    """The last call, what it cost, and what has been going wrong lately."""
    logs = AIUsageLog.objects.filter(tenant_id=tenant_id)
    last = logs.order_by("-created_at").first()
    totals = logs.aggregate(input_tokens=Sum("input_tokens"), output_tokens=Sum("output_tokens"))

    day_ago = timezone.now() - timedelta(days=1)
    recent = logs.filter(created_at__gte=day_ago)

    errors = list(
        logs.filter(outcome=AIUsageLog.Outcome.ERROR)
        .order_by("-created_at")
        .values("created_at", "model", "error")[:RECENT_ERRORS]
    )

    last_answer = (
        KnowledgeAnswer.objects.filter(tenant_id=tenant_id, is_test=False).order_by("-created_at").first()
    )

    return {
        "calls": logs.count(),
        "calls_last_24h": recent.count(),
        "input_tokens": totals["input_tokens"] or 0,
        "output_tokens": totals["output_tokens"] or 0,
        "by_outcome": dict(logs.values_list("outcome").annotate(total=Count("id")).values_list("outcome", "total")),
        "last_call": {
            "at": last.created_at,
            "model": last.model,
            "outcome": last.outcome,
            "latency_ms": last.latency_ms,
            "error": last.error,
        } if last else None,
        # The last thing a real customer was actually told, which is a
        # different question from the last call made and the more useful one
        # when somebody reports the agent saying something odd.
        "last_answer": {
            "at": last_answer.created_at,
            "question": last_answer.question[:200],
            "answer": last_answer.answer[:400],
            "confidence": last_answer.confidence,
            "answered": last_answer.answered,
        } if last_answer else None,
        "recent_errors": errors,
    }


def build(tenant_id, *, config=None) -> dict:
    """The whole picture, for the dashboard."""
    config = config or AIAgentConfig.for_tenant(tenant_id)
    return {
        "agent": agent_status(config),
        "knowledge": knowledge_status(tenant_id, knowledge_base_id=config.knowledge_base_id),
        "whatsapp": whatsapp_status(tenant_id),
        "automation": automation_status(tenant_id),
        "usage": usage_status(tenant_id),
        "generated_at": timezone.now(),
    }
