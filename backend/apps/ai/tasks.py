import logging

from config.celery import app
from core.backoff import retry_delay

logger = logging.getLogger("sendstack")


@app.task
def generate_reply_task(message_id):
    """
    Answer one inbound message.

    Runs on the worker because a model call takes seconds and the webhook that
    delivered the message is not going to wait for it. Failures are logged
    rather than retried: by the time a retry ran the customer would have moved
    on, and `answer()` already hands the conversation to a person when the
    provider is unreachable.
    """
    from apps.messaging.models import Message

    from . import services

    try:
        message = Message.objects.select_related("conversation", "contact", "whatsapp_account").get(pk=message_id)
    except Message.DoesNotExist:
        return
    if message.conversation is None:
        return
    # Re-checked here rather than trusted from whoever queued this. A task
    # sitting in the broker when a plan is downgraded must not run on the old
    # plan's terms, and `services.answer` checks again below -- entitlement is
    # decided when the work happens, never when it was asked for.

    try:
        services.answer(message.conversation, message)
    except Exception:
        logger.exception("AI reply failed for message %s.", message_id)


@app.task
def rebuild_document_chunks_task(document_id):
    from . import knowledge
    from .models import KnowledgeDocument

    document = KnowledgeDocument.objects.filter(pk=document_id).first()
    if document is not None:
        knowledge.rebuild(document)


@app.task(bind=True, max_retries=3)
def embed_document_task(self, document_id):
    """
    Turn one document's chunks into vectors.

    Retried, and retried further apart each time, because the failure this
    hits in practice is a rate limit on the embedding endpoint rather than a
    bad document -- a whole knowledge base uploaded at once is a burst of
    exactly the shape that gets throttled.

    A document that never succeeds is left with no vectors and stays
    perfectly usable: retrieval falls back to lexical for it. That is why this
    never touches the document's own status -- "READY" means the text was
    read, and marking a readable document FAILED because a vector endpoint was
    down would take a working knowledge base offline over an optimisation.
    """
    from . import embeddings
    from .models import KnowledgeDocument

    if not embeddings.is_enabled():
        return 0

    document = KnowledgeDocument.objects.filter(pk=document_id).select_related("knowledge_base").first()
    if document is None:
        return 0

    # Embedding is a paid call to a vector endpoint made on the tenant's
    # behalf, so it belongs to the AI feature even though the upload that
    # triggered it does not. A tenant without the agent keeps their documents,
    # keeps the lexical retrieval that reads them, and is not billed for
    # vectors nothing of theirs will ever query.
    from . import services

    if not services.agent_available(document.tenant_id):
        return 0

    try:
        return embeddings.index_document(document)
    except embeddings.EmbeddingError as exc:
        logger.warning("Could not embed document %s: %s", document_id, exc)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=retry_delay(self.request.retries, base=60))
        return 0
    except Exception:
        logger.exception("Embedding document %s crashed.", document_id)
        return 0


@app.task(bind=True, max_retries=2)
def process_document_task(self, document_id):
    """
    Extract and index one uploaded document.

    Retried twice, unlike the reply task: this failure is usually storage
    being briefly unavailable rather than a bad file, and the user is watching
    a status badge that would otherwise say FAILED for a transient blip. A
    genuinely unreadable file never reaches the retry -- `process_document`
    catches that and records the reason itself.
    """
    from .processing import process_document

    try:
        return process_document(document_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Knowledge document %s processing crashed.", document_id)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=retry_delay(self.request.retries, base=30))

        from .models import KnowledgeDocument

        KnowledgeDocument.objects.filter(pk=document_id).update(
            status=KnowledgeDocument.Status.FAILED,
            error="The file could not be read. Try uploading it again.",
        )
        return "FAILED"
