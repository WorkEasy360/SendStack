"""
Give every tenant that already has documents a knowledge base to hold them.

Before this, documents were a single flat pool per tenant and retrieval read
all of them. Naming bases is only useful if every existing document is *in*
one — otherwise `knowledge_base_id` is NULL, a config that names a base
retrieves nothing, and a tenant's whole knowledge silently disappears the
first time they organise it.

Chunks get the same base as their document, since the column is denormalised
for the retrieval filter and would otherwise have to be recovered by a join
that the fast path deliberately avoids.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    KnowledgeBase = apps.get_model("ai", "KnowledgeBase")
    KnowledgeDocument = apps.get_model("ai", "KnowledgeDocument")
    KnowledgeChunk = apps.get_model("ai", "KnowledgeChunk")

    tenant_ids = set(
        KnowledgeDocument.objects.filter(knowledge_base__isnull=True).values_list("tenant_id", flat=True)
    )
    for tenant_id in tenant_ids:
        base = KnowledgeBase.objects.filter(tenant_id=tenant_id).order_by("-is_default", "pk").first()
        if base is None:
            base = KnowledgeBase.objects.create(
                tenant_id=tenant_id,
                name="General",
                description="Everything the assistant knew before knowledge bases were named.",
                is_default=True,
            )
        KnowledgeDocument.objects.filter(tenant_id=tenant_id, knowledge_base__isnull=True).update(knowledge_base=base)
        KnowledgeChunk.objects.filter(tenant_id=tenant_id, knowledge_base__isnull=True).update(knowledge_base=base)

    # Documents that predate the upload path are typed text, already indexed,
    # and therefore READY -- but their chunk_count was never written, and a
    # document reporting "0 chunks" in the UI reads as broken.
    for document in KnowledgeDocument.objects.filter(source_type="TEXT").only("id"):
        count = KnowledgeChunk.objects.filter(document_id=document.pk).count()
        KnowledgeDocument.objects.filter(pk=document.pk).update(chunk_count=count, status="READY")


def unbackfill(apps, schema_editor):
    # Nothing to undo: clearing the column would take working knowledge bases
    # apart, and the forward path simply rebuilds what it set.
    pass


class Migration(migrations.Migration):
    dependencies = [("ai", "0002_aiagentconfig_crm_logging_enabled_and_more")]

    operations = [migrations.RunPython(backfill, unbackfill)]
