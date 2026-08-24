"""
Splitting documents up, and finding the pieces that answer a question.

Retrieval here is lexical -- term overlap weighted by how rare each term is
across the tenant's knowledge base -- not embeddings. That is a deliberate
trade: it needs no vector database, no embedding calls to keep in step with
edits, and no new dependency, and over the few dozen documents a business
actually maintains it finds the right paragraph. `search` and `retrieve` are
the only entry points, so swapping in embeddings later changes this file and
nothing else.

Two things this module owes the rest of the feature:

* **A score, not just an ordering.** An agent has to be able to tell "this is
  the answer" from "this is the least irrelevant thing I have", because the
  second one must produce a fallback rather than a confident invention.
* **Isolation on every query.** Every retrieval is filtered by tenant, and by
  knowledge base where one is named. There is no code path that reads a chunk
  without both.
"""

import math
import re
from collections import Counter

# Split on blank lines first: a FAQ, a price list and a policy document are
# all written in paragraphs, and a paragraph is very close to the unit that
# answers one question.
_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_WORD_RE = re.compile(r"\w+", re.UNICODE)

MAX_CHUNK_CHARS = 1200
MIN_CHUNK_CHARS = 40

#: Retrieval never scans more than this many chunks for one question. Reached
#: only by a tenant with a very large library; the alternative is one question
#: pulling a hundred thousand rows into memory.
MAX_SCANNED_CHUNKS = 5000

# Words too common to say anything about which chunk is relevant. Kept short
# and English-only on purpose: an aggressive stop list in a multilingual
# deployment removes the very terms a Hindi or Spanish question turns on.
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "does", "for", "from", "have", "how",
    "i", "if", "in", "is", "it", "me", "my", "of", "on", "or", "our", "please", "that", "the", "then", "there",
    "this", "to", "was", "we", "what", "when", "where", "which", "who", "will", "with", "you", "your",
}

#: Normalised relevance at or above which an answer is worth generating, and
#: the band above which it is worth trusting. These are the two numbers behind
#: GOOD / LIMITED / NONE; a tenant may raise the floor (AIAgentConfig.
#: min_relevance) but never sees these.
GOOD_RELEVANCE = 0.45
LIMITED_RELEVANCE = 0.15


def tokenize(text: str) -> list:
    return [word for word in _WORD_RE.findall((text or "").lower()) if word not in _STOP_WORDS and len(word) > 1]


def split(content: str) -> list:
    """
    Break a document into retrievable pieces.

    Paragraphs are kept whole where they fit, because a paragraph cut in half
    produces two chunks that each answer the question badly. Anything longer
    than the ceiling is split on sentence ends rather than mid-word.
    """
    chunks = []
    for paragraph in _PARAGRAPH_RE.split(content or ""):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= MAX_CHUNK_CHARS:
            chunks.append(paragraph)
            continue

        current = ""
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
            if current and len(current) + len(sentence) + 1 > MAX_CHUNK_CHARS:
                chunks.append(current.strip())
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
        if current.strip():
            chunks.append(current.strip())

    # A stray one-line fragment carries no context on its own; fold it into
    # the piece before it rather than storing something unusable.
    merged = []
    for chunk in chunks:
        if merged and len(chunk) < MIN_CHUNK_CHARS:
            merged[-1] = f"{merged[-1]}\n{chunk}"
        else:
            merged.append(chunk)
    return merged


def split_sections(sections) -> list:
    """
    Chunk extracted sections while keeping each piece's origin.

    Returns ``[(text, page, heading), ...]``. This is the upload path's
    equivalent of `split`, and the reason it exists separately is that page
    numbers are knowable here and nowhere downstream.
    """
    pieces = []
    for section in sections:
        for text in split(section.text):
            pieces.append((text, section.page, section.heading))
    return pieces


def rebuild(document) -> int:
    """
    Replace a document's chunks with its current text.

    Delete-then-create rather than a diff: a corrected price whose old chunk
    survived would go on being quoted at customers, and that is the single
    worst outcome this feature has. Cheap enough at these sizes to be worth
    the certainty.
    """
    from .models import KnowledgeChunk

    KnowledgeChunk.objects.filter(document=document).delete()
    pieces = split(document.content)
    KnowledgeChunk.objects.bulk_create([
        KnowledgeChunk(
            document=document, tenant_id=document.tenant_id, knowledge_base_id=document.knowledge_base_id,
            ordinal=index, content=piece,
        )
        for index, piece in enumerate(pieces)
    ])
    return len(pieces)


def rebuild_from_sections(document, sections) -> int:
    """Replace a document's chunks from extracted sections, keeping page/heading."""
    from .models import KnowledgeChunk

    KnowledgeChunk.objects.filter(document=document).delete()
    pieces = split_sections(sections)
    KnowledgeChunk.objects.bulk_create(
        [
            KnowledgeChunk(
                document=document, tenant_id=document.tenant_id, knowledge_base_id=document.knowledge_base_id,
                ordinal=index, content=text, page=page, heading=(heading or "")[:200],
            )
            for index, (text, page, heading) in enumerate(pieces)
        ],
        batch_size=500,
    )
    return len(pieces)


class Hit:
    """One retrieved chunk and how well it matched."""

    __slots__ = ("chunk", "score")

    def __init__(self, chunk, score):
        self.chunk = chunk
        self.score = score

    def __repr__(self):
        return f"Hit({self.chunk.pk}, {self.score:.3f})"


class Retrieval:
    """
    What one question found: the chunks, how relevant they were, and the
    documents behind them.

    `relevance` is normalised 0-1 rather than a raw TF-IDF total, because a
    raw score varies with query length and library size and so cannot be
    compared against a fixed threshold. `confidence` is the only form of it
    anything outside this module should branch on.
    """

    __slots__ = ("hits", "relevance", "confidence")

    def __init__(self, hits, relevance, confidence):
        self.hits = hits
        self.relevance = relevance
        self.confidence = confidence

    @property
    def chunks(self):
        return [hit.chunk for hit in self.hits]

    @property
    def sources(self) -> list:
        """One entry per distinct document, best match first, for provenance."""
        seen, out = set(), []
        for hit in self.hits:
            document = hit.chunk.document
            if document.pk in seen:
                continue
            seen.add(document.pk)
            out.append({
                "document_id": document.pk,
                "title": document.title,
                "filename": document.original_filename or "",
                "page": hit.chunk.page,
                "citation": hit.chunk.citation,
            })
        return out

    def __bool__(self):
        return bool(self.hits)


def _band(relevance: float) -> str:
    from .models import KnowledgeAnswer

    if relevance >= GOOD_RELEVANCE:
        return KnowledgeAnswer.Confidence.GOOD
    if relevance >= LIMITED_RELEVANCE:
        return KnowledgeAnswer.Confidence.LIMITED
    return KnowledgeAnswer.Confidence.NONE


def retrieve(tenant_id, query: str, *, knowledge_base_id=None, limit: int = 5) -> Retrieval:
    """
    The chunks most likely to answer `query`, with a relevance band.

    Scored with plain TF-IDF over the tenant's own knowledge base: a term that
    appears in every document says nothing about which one to read, while a
    term appearing in two says a great deal. Chunks matching none of the
    query's terms are not returned at all -- padding the model's context with
    unrelated text is how an agent ends up confidently answering from the
    wrong page.

    The normalisation is deliberately conservative: the best chunk's score is
    divided by the score it *would* have if it contained every distinctive
    term in the question. A question whose terms appear nowhere lands at 0,
    and a question answered squarely by one paragraph lands near 1.
    """
    from .models import KnowledgeChunk

    terms = set(tokenize(query))
    if not terms:
        return Retrieval([], 0.0, _band(0.0))

    chunks = KnowledgeChunk.objects.filter(
        # Both filters, always. Isolation is not a feature of the caller.
        tenant_id=tenant_id,
        document__is_active=True,
        document__status="READY",
    )
    if knowledge_base_id is not None:
        chunks = chunks.filter(knowledge_base_id=knowledge_base_id, knowledge_base__is_active=True)

    chunks = list(
        chunks.select_related("document")
        .only(
            "id", "content", "page", "heading", "document__id", "document__title",
            "document__source_url", "document__original_filename",
        )[:MAX_SCANNED_CHUNKS]
    )
    if not chunks:
        return Retrieval([], 0.0, _band(0.0))

    tokenized = [(chunk, Counter(tokenize(chunk.content))) for chunk in chunks]
    document_frequency = Counter()
    for _, counts in tokenized:
        document_frequency.update(counts.keys() & terms)

    total = len(tokenized)
    # The score a chunk would get if it contained every query term once --
    # the yardstick the best hit is measured against.
    perfect = sum(math.log(1 + total / (1 + document_frequency[term])) for term in terms) or 1.0

    scored = []
    for chunk, counts in tokenized:
        score = 0.0
        for term in terms:
            occurrences = counts.get(term, 0)
            if not occurrences:
                continue
            # Smoothed IDF, and a sub-linear term count so one chunk repeating
            # a word twenty times does not outrank the one that answers.
            idf = math.log(1 + total / (1 + document_frequency[term]))
            score += (1 + math.log(occurrences)) * idf
        if score > 0:
            scored.append((score, chunk))

    if not scored:
        return Retrieval([], 0.0, _band(0.0))

    scored.sort(key=lambda pair: (-pair[0], pair[1].pk))
    top = scored[:limit]
    relevance = min(top[0][0] / perfect, 1.0)
    return Retrieval([Hit(chunk, score) for score, chunk in top], relevance, _band(relevance))


def search(tenant_id, query: str, limit: int = 5, knowledge_base_id=None) -> list:
    """Backwards-compatible chunk list. New code should use `retrieve`."""
    return retrieve(tenant_id, query, knowledge_base_id=knowledge_base_id, limit=limit).chunks
