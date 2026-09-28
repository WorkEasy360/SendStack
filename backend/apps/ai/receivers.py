"""
Deleting a document has to delete the document.

Dropping the row cascades the chunks, so nothing can be *answered* from a
deleted file -- but Django never touches the stored bytes, so a "deleted"
price list keeps sitting in the media volume. Uploaded documents are private
tenant data and deletion has to mean deletion.

Registering the receiver has a second, load-bearing effect: it stops Django
fast-deleting `KnowledgeDocument` rows, so the file goes on a cascade too
(deleting a knowledge base, or a whole tenant) and not only on an explicit
single delete.
"""

import logging

from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import KnowledgeDocument

logger = logging.getLogger("sendstack")


@receiver(post_delete, sender=KnowledgeDocument, dispatch_uid="ai_delete_knowledge_document_file")
def delete_document_file(sender, instance, **kwargs):
    """Remove the stored upload once its document row is gone."""
    if not instance.file:
        return
    try:
        instance.file.delete(save=False)
    except Exception:  # noqa: BLE001 - a file already gone must never fail the delete
        logger.warning(
            "Could not remove the stored file for knowledge document %s", instance.pk, exc_info=True,
        )
