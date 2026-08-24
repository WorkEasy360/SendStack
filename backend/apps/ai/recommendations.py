"""
"You uploaded a warranty document. Answer warranty questions automatically?"

Read from the documents a tenant actually uploaded, matched against the same
closed topic list the CRM logging uses (`services.TOPICS`), so a suggestion
only ever appears because a real document mentions the thing.

Like the automation recommendations, nothing here acts: every suggestion is a
link to a switch the user still has to flip.
"""

from .models import KnowledgeAnswer, KnowledgeBase, KnowledgeDocument
from .services import TOPICS

#: A topic has to appear in a document this many times before it is worth
#: suggesting an automation for. One passing mention of "price" in a terms
#: document is not a price list.
MIN_TOPIC_MENTIONS = 3


def _topic_counts(documents) -> dict:
    counts = {}
    for document in documents:
        haystack = f" {document.title.lower()} \n{(document.content or '').lower()}"
        for key, words, label in TOPICS:
            hits = sum(haystack.count(word) for word in words)
            if hits:
                current = counts.setdefault(key, {"label": label, "mentions": 0, "documents": set()})
                current["mentions"] += hits
                current["documents"].add(document.title)
    return counts


def build(tenant, *, knowledge_base=None) -> list:
    """Suggestions for one tenant, optionally narrowed to one knowledge base."""
    documents = KnowledgeDocument.objects.filter(
        tenant=tenant, is_active=True, status=KnowledgeDocument.Status.READY,
    ).only("id", "title", "content")
    if knowledge_base is not None:
        documents = documents.filter(knowledge_base=knowledge_base)

    documents = list(documents[:200])
    out = []

    if not documents:
        return out

    config = getattr(tenant, "ai_agent", None)
    agent_on = bool(config and config.is_enabled)

    # The first and biggest one: there is knowledge here and nothing is using it.
    if not agent_on:
        total_chunks = sum(document.chunk_count for document in documents)
        out.append({
            "key": "enable_auto_reply",
            "title": (
                f"{len(documents)} document is indexed and ready" if len(documents) == 1
                else f"{len(documents)} documents are indexed and ready"
            ),
            "detail": (
                f"{total_chunks} passages are searchable, but auto-reply is off — nothing is answering customers "
                "from them yet."
            ),
            "action": "enable",
            "action_label": "Turn on auto-reply",
            "tone": "green",
        })

    for key, data in sorted(_topic_counts(documents).items(), key=lambda item: -item[1]["mentions"]):
        if data["mentions"] < MIN_TOPIC_MENTIONS:
            continue
        sources = sorted(data["documents"])[:3]
        out.append({
            "key": f"topic_{key}",
            "title": f"Your knowledge base covers {data['label'].lower().replace(' enquiry', '').replace(' issue', '')}",
            "detail": f"Found in {', '.join(sources)}. Customers asking about this can be answered automatically.",
            "action": "test",
            "action_label": "Test it",
            "topic": key,
            "tone": "blue",
        })

    # Questions the base has already failed to answer are the most actionable
    # thing here: they are a list of documents the tenant is missing, written
    # by their own customers.
    unanswered = list(
        KnowledgeAnswer.objects.filter(
            tenant=tenant, confidence=KnowledgeAnswer.Confidence.NONE, is_test=False,
        ).order_by("-created_at").values_list("question", flat=True)[:5]
    )
    if len(unanswered) >= 3:
        out.append({
            "key": "gaps",
            "title": f"{len(unanswered)} recent questions couldn't be answered",
            "detail": "For example: " + " · ".join(question[:60] for question in unanswered[:3]),
            "action": "gaps",
            "action_label": "See what's missing",
            "tone": "amber",
        })

    failed = KnowledgeDocument.objects.filter(
        tenant=tenant, status=KnowledgeDocument.Status.FAILED,
    ).count()
    if failed:
        out.append({
            "key": "failed_documents",
            "title": f"{failed} document{'s' if failed != 1 else ''} could not be read",
            "detail": "Nothing in them is searchable. Open the knowledge base to see why.",
            "action": "review",
            "action_label": "Review them",
            "tone": "red",
        })

    return out


def ensure_base(tenant):
    """The tenant's default base, for callers that need one to exist."""
    from .services import default_knowledge_base

    return default_knowledge_base(tenant.pk) if not KnowledgeBase.objects.filter(tenant=tenant).exists() else None
