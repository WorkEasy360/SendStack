"""
The document pipeline: upload -> extract -> clean -> chunk -> index -> ready.

Runs on the worker, never in the request. A 300-page PDF takes seconds to
parse and the browser that uploaded it should have moved on long before that;
more importantly, a parser that hangs on a malformed file must not hang a
gunicorn thread.

Every failure ends with the document in FAILED carrying a sentence the person
who uploaded it can act on ("that PDF is a scan, paste the text instead"),
never a stack trace and never a silently empty knowledge base.
"""

import logging

from django.utils import timezone

from . import extraction, knowledge
from .models import KnowledgeDocument

logger = logging.getLogger("sendstack")


def _finish(document, status, *, error="", chunks=0, pages=0):
    document.status = status
    document.error = (error or "")[:500]
    document.chunk_count = chunks
    document.page_count = pages or document.page_count
    document.indexed_at = timezone.now() if status == KnowledgeDocument.Status.READY else document.indexed_at
    document.save(update_fields=["status", "error", "chunk_count", "page_count", "indexed_at", "updated_at"])


def process_document(document_id: int) -> str:
    """
    Read one uploaded document and index it. Returns the final status.

    Safe to run twice: chunking is delete-then-create, so a re-index replaces
    what was there rather than doubling it. That is also what makes this the
    single entry point for both the first upload and every later "re-index".
    """
    document = KnowledgeDocument.objects.filter(pk=document_id).first()
    if document is None:
        return "MISSING"

    if document.source_type != KnowledgeDocument.Source.FILE:
        # A typed document has nothing to extract; it is already its own text.
        count = knowledge.rebuild(document)
        _finish(document, KnowledgeDocument.Status.READY, chunks=count)
        return document.status

    if not document.file:
        _finish(document, KnowledgeDocument.Status.FAILED, error="The uploaded file is missing.")
        return document.status

    document.status = KnowledgeDocument.Status.PROCESSING
    document.error = ""
    document.save(update_fields=["status", "error", "updated_at"])

    filename = document.original_filename or document.file.name
    try:
        with document.file.open("rb") as handle:
            sections = extraction.extract(handle, filename)
    except extraction.ExtractionError as exc:
        logger.info("Knowledge document %s could not be read: %s", document.pk, exc)
        _finish(document, KnowledgeDocument.Status.FAILED, error=str(exc))
        return document.status
    except FileNotFoundError:
        # The row exists but the bytes do not. Re-reading will never fix this,
        # so the message says the only thing that will: upload it again. The
        # path goes to the log rather than the screen -- it is an operator's
        # clue (a media volume that lost its contents, or an API server that
        # does not share storage with this worker), not the uploader's.
        logger.error(
            "Knowledge document %s has no file at %s -- storage is not shared with whatever took the upload.",
            document.pk, getattr(document.file, "name", ""),
        )
        _finish(
            document, KnowledgeDocument.Status.FAILED,
            error="The uploaded file is no longer on the server. Please upload it again.",
        )
        return document.status
    except OSError:
        logger.exception("Knowledge document %s could not be opened from storage", document.pk)
        _finish(
            document, KnowledgeDocument.Status.FAILED,
            error="The uploaded file could not be opened on the server. Please upload it again.",
        )
        return document.status
    except Exception as exc:  # noqa: BLE001 - a parser can raise anything at all
        logger.exception("Knowledge document %s failed unexpectedly", document.pk)
        _finish(document, KnowledgeDocument.Status.FAILED, error=f"The file could not be read ({exc.__class__.__name__}).")
        return document.status

    if not sections:
        _finish(document, KnowledgeDocument.Status.FAILED, error="No readable text was found in that file.")
        return document.status

    # `content` stays authoritative for everything that reads a document as a
    # whole (the editor, search, exports), so the extracted text is written
    # back to it as well as chunked. Truncated to keep one enormous upload
    # from making every list query that touches the column expensive.
    document.content = "\n\n".join(section.text for section in sections)[: extraction.MAX_TEXT_CHARS]
    document.save(update_fields=["content", "updated_at"])

    count = knowledge.rebuild_from_sections(document, sections)
    pages = max((section.page or 0) for section in sections) if sections else 0

    if not count:
        _finish(document, KnowledgeDocument.Status.FAILED, error="That file produced nothing worth indexing.", pages=pages)
        return document.status

    _finish(document, KnowledgeDocument.Status.READY, chunks=count, pages=pages)
    logger.info("Knowledge document %s indexed: %s chunks from %s.", document.pk, count, filename)
    return document.status
