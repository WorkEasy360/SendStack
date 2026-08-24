import logging

from config.celery import app

logger = logging.getLogger("multisender")


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
            raise self.retry(exc=exc, countdown=30 * (2**self.request.retries))

        from .models import KnowledgeDocument

        KnowledgeDocument.objects.filter(pk=document_id).update(
            status=KnowledgeDocument.Status.FAILED,
            error="The file could not be read. Try uploading it again.",
        )
        return "FAILED"
