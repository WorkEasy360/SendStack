"""
Semantic retrieval: turning chunks and questions into vectors, and comparing
them.

This is a **second opinion, not a replacement**. `knowledge.retrieve` stays
lexical at its core and blends what this module returns, because the two fail
in opposite directions and a business knowledge base needs both:

* Lexical retrieval is exact and cheap and never wrong about a product code,
  an order number or a model name -- and it cannot answer "how long do I have
  to send it back?" out of a policy that only ever says "returns".
* Semantic retrieval answers that question and is confidently vague about the
  difference between the XR-200 and the XR-2000.

Blending them means a chunk has to do well on both to win, which is the whole
point. See `AI_EMBEDDING_WEIGHT`.

Everything here is optional and fails soft. With `AI_EMBEDDINGS_ENABLED` off,
no vector is ever written and retrieval behaves exactly as it did before this
module existed. With it on but the endpoint unreachable, a question still gets
its lexical answer -- an outage at the embedding provider must not turn into
an agent that cannot answer anybody.

The vector store is the database. There is no Pinecone, no pgvector, no second
service to run: a business knowledge base is a few thousand chunks, similarity
against a few thousand unit vectors is milliseconds of arithmetic, and the
operational cost of a vector database is not repaid at that size. The seam is
`similarities()` -- if a tenant ever arrives with a library big enough to
need one, that one function is what changes.
"""

import logging
import math
import struct
import time

import requests
from django.conf import settings

from .providers.registry import CUSTOM, OPENAI, OPENROUTER, AIProviderError

logger = logging.getLogger("sendstack")

#: Never score more vectors than this for one question. The same ceiling
#: `knowledge.MAX_SCANNED_CHUNKS` puts on the lexical pass, for the same
#: reason: one question must not pull a hundred thousand rows into memory.
MAX_SCANNED_VECTORS = 5000

#: Longest text sent to the embedding endpoint for one chunk. Chunks are
#: capped well below this by `knowledge.MAX_CHUNK_CHARS`; this is the backstop
#: for a chunk that arrived some other way.
MAX_EMBED_CHARS = 8000


class EmbeddingError(AIProviderError):
    """Raised when text could not be embedded. Always caught by retrieval."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    return bool(getattr(settings, "AI_EMBEDDINGS_ENABLED", False))


def model_name() -> str:
    return (getattr(settings, "AI_EMBEDDING_MODEL", "") or "text-embedding-3-small").strip()


def weight() -> float:
    """How much of a blended score the vector similarity is worth, clamped 0-1."""
    try:
        raw = float(getattr(settings, "AI_EMBEDDING_WEIGHT", 0.5))
    except (TypeError, ValueError):
        return 0.5
    return min(max(raw, 0.0), 1.0)


def credentials_for(config):
    """
    The key and endpoint to embed with, or ``(None, "")`` if there are none.

    Embeddings need an OpenAI-*shaped* endpoint, which is not the same
    question as which vendor writes the tenant's replies: Anthropic has no
    embeddings API at all. So a tenant whose agent is on OpenAI, OpenRouter or
    a custom endpoint embeds with their own key, and everybody else -- an
    Anthropic tenant included -- falls through to the deployment's OpenAI
    credentials. A deployment that set OPENAI_API_KEY therefore gets semantic
    retrieval for every tenant, whichever vendor each of them answers with.
    """
    from .providers import openai as openai_client

    tenant_key = (config.get_api_key() or "").strip() if config is not None else ""
    provider = getattr(config, "provider", None) or getattr(config, "model_provider", None)

    if tenant_key and provider in (OPENAI, OPENROUTER, CUSTOM):
        base = (getattr(config, "base_url", "") or "").strip()
        if provider == OPENROUTER:
            # OpenRouter proxies OpenAI's embeddings at its own origin.
            base = "https://openrouter.ai/api/v1"
        return tenant_key, (base or openai_client.default_base_url())

    deployment_key = (getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    if deployment_key:
        return deployment_key, openai_client.default_base_url()
    return None, ""


def available_for(config) -> bool:
    """Whether this tenant could embed anything right now."""
    if not is_enabled():
        return False
    key, _ = credentials_for(config)
    return bool(key)


# ---------------------------------------------------------------------------
# Storage format
# ---------------------------------------------------------------------------

def pack(vector) -> bytes:
    """
    A vector as float32 bytes, normalised to unit length.

    Normalising here rather than at read time is what lets `similarities`
    compare with a plain dot product: the magnitude carries no meaning for
    similarity, and taking it out once at write time removes a square root per
    chunk per question.
    """
    magnitude = math.sqrt(sum(component * component for component in vector))
    if not magnitude:
        # An all-zero vector is what a degenerate embedding looks like. Stored
        # as-is; it will simply never be similar to anything.
        return struct.pack(f"<{len(vector)}f", *vector)
    return struct.pack(f"<{len(vector)}f", *(component / magnitude for component in vector))


def unpack(blob) -> list:
    """The float list behind a stored vector."""
    raw = bytes(blob or b"")
    count = len(raw) // 4
    if not count:
        return []
    return list(struct.unpack(f"<{count}f", raw[: count * 4]))


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

def embed(texts, *, api_key, base_url, model=None, timeout=None) -> list:
    """
    Vectors for a list of texts, in the order given.

    One request for the whole batch -- the endpoint accepts a list, and a
    request per chunk turns indexing a 300-page PDF into a thousand round
    trips.
    """
    cleaned = [(text or "")[:MAX_EMBED_CHARS] for text in texts]
    if not cleaned:
        return []
    if not api_key:
        raise EmbeddingError("No API key is available to create embeddings with.")

    url = f"{(base_url or '').strip().rstrip('/')}/embeddings"
    started = time.monotonic()
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
            json={"model": model or model_name(), "input": cleaned},
            timeout=timeout or getattr(settings, "AI_REQUEST_TIMEOUT_SECONDS", 30),
        )
    except requests.RequestException as exc:
        raise EmbeddingError(f"Could not reach {url}: {exc}") from exc

    if response.status_code != 200:
        detail = ""
        try:
            detail = (response.json().get("error") or {}).get("message", "")
        except ValueError:
            detail = response.text[:200]
        raise EmbeddingError(f"The embedding endpoint returned {response.status_code}: {detail}")

    try:
        data = response.json()
    except ValueError as exc:
        raise EmbeddingError("The embedding endpoint returned a response that was not JSON.") from exc

    rows = data.get("data")
    if not isinstance(rows, list) or len(rows) != len(cleaned):
        raise EmbeddingError("The embedding endpoint returned a different number of vectors than texts sent.")

    # The API documents `index` on every row and is not guaranteed to return
    # them in order; sorting is cheap insurance against a chunk being stored
    # under another chunk's meaning, which nothing downstream could detect.
    rows = sorted(rows, key=lambda row: row.get("index", 0))
    vectors = [row.get("embedding") or [] for row in rows]
    logger.debug("Embedded %s texts in %sms.", len(cleaned), int((time.monotonic() - started) * 1000))
    return vectors


def embed_query(query: str, config) -> list:
    """
    One question as a vector, or ``[]`` if it could not be made.

    Never raises. A question that cannot be embedded still has a lexical
    answer, and an embedding outage that silenced the agent entirely would be
    a worse failure than the one it was meant to fix.
    """
    if not is_enabled() or not (query or "").strip():
        return []
    key, base = credentials_for(config)
    if not key:
        return []
    try:
        vectors = embed([query], api_key=key, base_url=base)
    except EmbeddingError as exc:
        logger.warning("Could not embed a question, falling back to lexical retrieval: %s", exc)
        return []
    return vectors[0] if vectors else []


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def index_document(document, *, config=None) -> int:
    """
    Embed every chunk of one document. Returns how many vectors were written.

    Replaces rather than tops up, because chunks are themselves replaced on
    every edit (see `knowledge.rebuild`): a vector left behind by a corrected
    price is the same failure as a chunk left behind, and worse for being
    invisible.
    """
    from .models import AIAgentConfig, KnowledgeChunk, KnowledgeEmbedding

    if not is_enabled():
        return 0

    config = config or AIAgentConfig.for_tenant(document.tenant_id)
    key, base = credentials_for(config)
    if not key:
        logger.info(
            "Embeddings are on but tenant %s has no usable key; document %s stays lexical-only.",
            document.tenant_id, document.pk,
        )
        return 0

    chunks = list(KnowledgeChunk.objects.filter(document=document).only("id", "content").order_by("ordinal", "id"))
    KnowledgeEmbedding.objects.filter(chunk__document=document).delete()
    if not chunks:
        return 0

    name = model_name()
    batch_size = max(int(getattr(settings, "AI_EMBEDDING_BATCH_SIZE", 64) or 64), 1)
    written = 0

    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        vectors = embed([chunk.content for chunk in batch], api_key=key, base_url=base, model=name)
        rows = []
        for chunk, vector in zip(batch, vectors):
            if not vector:
                continue
            rows.append(
                KnowledgeEmbedding(
                    chunk=chunk, tenant_id=document.tenant_id, knowledge_base_id=document.knowledge_base_id,
                    model=name, dims=len(vector), vector=pack(vector),
                )
            )
        KnowledgeEmbedding.objects.bulk_create(rows, batch_size=200)
        written += len(rows)

    logger.info("Embedded %s of %s chunks for document %s.", written, len(chunks), document.pk)
    return written


def schedule(document) -> None:
    """
    Queue the embedding of a document's chunks, if that is worth doing.

    Called from `knowledge.rebuild` and `rebuild_from_sections`, which are the
    only two places chunks come into existence -- so a document indexed by any
    path, present or future, gets its vectors without that path having to know
    embeddings exist.

    Guarded on `is_enabled()` before the queue is touched, so a deployment
    with the feature off enqueues nothing at all rather than a task per
    document that wakes a worker to decide it has no work.
    """
    if not is_enabled() or document is None or not getattr(document, "pk", None):
        return
    try:
        from .tasks import embed_document_task

        embed_document_task.delay(document.pk)
    except Exception:  # noqa: BLE001 - a broker outage must not fail the upload
        logger.exception("Could not queue embedding for document %s", getattr(document, "pk", None))


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------

def similarities(tenant_id, query_vector, *, knowledge_base_id=None, chunk_ids=None) -> dict:
    """
    ``{chunk_id: similarity}`` for one tenant's stored vectors, 0-1.

    Both filters that matter are applied in the database, not in Python: a
    query never sees a vector belonging to another tenant, and never sees one
    outside the named knowledge base. That is the same guarantee
    `knowledge.retrieve` makes about chunks, made in the same place, because
    isolation implemented twice is isolation that can be right once.

    `chunk_ids` narrows to a candidate set where the caller already has one.
    Left out, every vector the tenant has is considered -- which is the point
    of semantic retrieval: the chunk that answers the question may share no
    word with it, so it cannot be found by narrowing to lexical matches first.

    Similarity is the dot product, because stored vectors are unit length (see
    `pack`), then clamped to 0-1: a negative similarity means "about the
    opposite of this", which for retrieval is the same as no match.
    """
    from .models import KnowledgeEmbedding

    if not query_vector:
        return {}

    query = list(query_vector)
    magnitude = math.sqrt(sum(component * component for component in query))
    if not magnitude:
        return {}
    query = [component / magnitude for component in query]

    rows = KnowledgeEmbedding.objects.filter(
        tenant_id=tenant_id,
        model=model_name(),
        dims=len(query),
        chunk__document__is_active=True,
        chunk__document__status="READY",
    )
    if knowledge_base_id is not None:
        rows = rows.filter(knowledge_base_id=knowledge_base_id, knowledge_base__is_active=True)
    if chunk_ids is not None:
        rows = rows.filter(chunk_id__in=list(chunk_ids))

    scores = {}
    for chunk_id, blob in rows.values_list("chunk_id", "vector")[:MAX_SCANNED_VECTORS]:
        vector = unpack(blob)
        if len(vector) != len(query):
            continue
        dot = sum(a * b for a, b in zip(query, vector))
        if dot > 0:
            scores[chunk_id] = min(dot, 1.0)
    return scores


def stats(tenant_id, *, knowledge_base_id=None) -> dict:
    """How much of a tenant's knowledge base is embedded -- for the status panel."""
    from .models import KnowledgeChunk, KnowledgeEmbedding

    chunks = KnowledgeChunk.objects.filter(tenant_id=tenant_id)
    vectors = KnowledgeEmbedding.objects.filter(tenant_id=tenant_id, model=model_name())
    if knowledge_base_id is not None:
        chunks = chunks.filter(knowledge_base_id=knowledge_base_id)
        vectors = vectors.filter(knowledge_base_id=knowledge_base_id)

    total = chunks.count()
    embedded = vectors.count()
    return {
        "enabled": is_enabled(),
        "model": model_name() if is_enabled() else "",
        "weight": weight() if is_enabled() else 0.0,
        "chunks": total,
        "embedded": embedded,
        # What retrieval is actually doing right now, rather than what is
        # configured: "on but nothing indexed yet" is the state a tenant needs
        # to be able to see, because it looks exactly like "off" from outside.
        "mode": "hybrid" if (is_enabled() and embedded) else "lexical",
    }
