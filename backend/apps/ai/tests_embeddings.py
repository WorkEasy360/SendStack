"""
Semantic retrieval: what it adds, what it must not break, and what happens
when it is unavailable.

The riskiest thing about adding embeddings to a working knowledge base is not
that the vectors are wrong -- it is that turning them on changes the answers
for tenants who never asked for them, and that turning them on and having the
endpoint fail leaves the agent unable to answer at all. So most of what is
here is about the *absence* of vectors being harmless.

Vectors in these tests are written by hand rather than fetched. A test that
depended on a real embedding model would be testing OpenAI's weights, would
need a network and a key, and would still not tell us whether the blend, the
isolation filters or the fallback work -- which is everything this file is
actually for.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.tenants.models import Tenant

from . import embeddings, knowledge
from .models import AIAgentConfig, KnowledgeBase, KnowledgeChunk, KnowledgeDocument, KnowledgeEmbedding

ON = {"AI_EMBEDDINGS_ENABLED": True, "AI_EMBEDDING_MODEL": "test-embed", "OPENAI_API_KEY": "sk-deployment"}

#: Two paragraphs that each clear `knowledge.MIN_CHUNK_CHARS`, so the chunker
#: keeps them apart. A shorter one is folded into the paragraph above it, which
#: silently turns a two-chunk fixture into a one-chunk one.
TWO_PARAGRAPHS = (
    "Returns are accepted within fourteen days of purchase, provided the item is unused.\n\n"
    "Delivery inside the city takes two working days and costs sixty rupees."
)


def _vector(*components):
    """A short vector, padded to a fixed width so every stored vector matches."""
    values = list(components)
    return values + [0.0] * (8 - len(values))


class _Response:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class Fixture(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Acme", slug=f"emb-{self._testMethodName}"[:50], kind=Tenant.Kind.CUSTOMER,
        )
        self.owner = get_user_model().objects.create_user(
            username=f"emb-owner-{self._testMethodName}"[:140], password="x", tenant=self.tenant,
        )
        self.base = KnowledgeBase.objects.create(tenant=self.tenant, name="General", is_default=True)

    def _document(self, content, title="Doc", tenant=None, base=None):
        """
        A chunked document, with the embedding it schedules suppressed.

        Chunking queues `embed_document_task`, and under CELERY_TASK_ALWAYS_EAGER
        -- which is how the test settings run -- "queues" means "runs now, in
        this process, against the configured endpoint". Without this patch,
        building a fixture reaches out to api.openai.com. Every test here that
        cares about indexing calls `index_document` itself with a stubbed
        transport; the rest want chunks and nothing more.
        """
        document = KnowledgeDocument.objects.create(
            tenant=tenant or self.tenant, knowledge_base=base or self.base, title=title, content=content,
        )
        with patch("apps.ai.tasks.embed_document_task.delay"):
            knowledge.rebuild(document)
        return document

    def _embed_chunk(self, chunk, vector, model="test-embed"):
        return KnowledgeEmbedding.objects.create(
            chunk=chunk, tenant_id=chunk.tenant_id, knowledge_base_id=chunk.knowledge_base_id,
            model=model, dims=len(vector), vector=embeddings.pack(vector),
        )


class VectorStorageTests(TestCase):
    def test_a_vector_survives_the_round_trip(self):
        original = [0.6, 0.8, 0.0]

        restored = embeddings.unpack(embeddings.pack(original))

        for expected, actual in zip(original, restored):
            self.assertAlmostEqual(expected, actual, places=5)

    def test_storage_normalises_to_unit_length(self):
        # Similarity is a dot product, which is only cosine if the magnitude
        # has already been divided out.
        restored = embeddings.unpack(embeddings.pack([3.0, 4.0]))

        self.assertAlmostEqual(restored[0], 0.6, places=5)
        self.assertAlmostEqual(restored[1], 0.8, places=5)

    def test_an_all_zero_vector_does_not_divide_by_zero(self):
        self.assertEqual(embeddings.unpack(embeddings.pack([0.0, 0.0])), [0.0, 0.0])

    def test_an_empty_blob_reads_back_as_no_vector(self):
        self.assertEqual(embeddings.unpack(b""), [])


class SimilarityTests(Fixture):
    @override_settings(**ON)
    def test_the_closest_vector_scores_highest(self):
        document = self._document(TWO_PARAGRAPHS)
        first, second = list(KnowledgeChunk.objects.filter(document=document).order_by("ordinal"))
        self._embed_chunk(first, _vector(1.0, 0.0))
        self._embed_chunk(second, _vector(0.0, 1.0))

        scores = embeddings.similarities(self.tenant.pk, _vector(1.0, 0.0))

        self.assertAlmostEqual(scores[first.pk], 1.0, places=5)
        self.assertNotIn(second.pk, scores)

    @override_settings(**ON)
    def test_an_opposite_vector_is_no_match_rather_than_a_negative_one(self):
        document = self._document("Returns are accepted within fourteen days.")
        chunk = KnowledgeChunk.objects.get(document=document)
        self._embed_chunk(chunk, _vector(1.0, 0.0))

        self.assertEqual(embeddings.similarities(self.tenant.pk, _vector(-1.0, 0.0)), {})

    @override_settings(**ON)
    def test_a_vector_from_another_tenant_is_never_scored(self):
        other = Tenant.objects.create(name="Rival", slug="emb-rival", kind=Tenant.Kind.CUSTOMER)
        other_base = KnowledgeBase.objects.create(tenant=other, name="General", is_default=True)
        theirs = self._document("Our secret margin is forty percent.", tenant=other, base=other_base)
        their_chunk = KnowledgeChunk.objects.get(document=theirs)
        self._embed_chunk(their_chunk, _vector(1.0, 0.0))

        scores = embeddings.similarities(self.tenant.pk, _vector(1.0, 0.0))

        self.assertEqual(scores, {})

    @override_settings(**ON)
    def test_a_vector_outside_the_named_knowledge_base_is_never_scored(self):
        summer = KnowledgeBase.objects.create(tenant=self.tenant, name="Summer")
        expired = self._document("Summer pricing is half off.", base=summer)
        chunk = KnowledgeChunk.objects.get(document=expired)
        self._embed_chunk(chunk, _vector(1.0, 0.0))

        scores = embeddings.similarities(self.tenant.pk, _vector(1.0, 0.0), knowledge_base_id=self.base.pk)

        self.assertEqual(scores, {})

    @override_settings(**ON)
    def test_vectors_from_a_different_embedding_model_are_ignored(self):
        # Two embedding models are two coordinate systems; comparing across
        # them scores nonsense rather than failing, which is worse.
        document = self._document("Returns are accepted within fourteen days.")
        chunk = KnowledgeChunk.objects.get(document=document)
        self._embed_chunk(chunk, _vector(1.0, 0.0), model="some-older-model")

        self.assertEqual(embeddings.similarities(self.tenant.pk, _vector(1.0, 0.0)), {})

    @override_settings(**ON)
    def test_a_vector_of_the_wrong_width_is_ignored(self):
        document = self._document("Returns are accepted within fourteen days.")
        chunk = KnowledgeChunk.objects.get(document=document)
        self._embed_chunk(chunk, [1.0, 0.0, 0.0])

        self.assertEqual(embeddings.similarities(self.tenant.pk, _vector(1.0, 0.0)), {})

    @override_settings(**ON)
    def test_an_unindexed_document_is_not_reachable_by_vector(self):
        document = self._document("Returns are accepted within fourteen days.")
        chunk = KnowledgeChunk.objects.get(document=document)
        self._embed_chunk(chunk, _vector(1.0, 0.0))
        KnowledgeDocument.objects.filter(pk=document.pk).update(is_active=False)

        self.assertEqual(embeddings.similarities(self.tenant.pk, _vector(1.0, 0.0)), {})


class RetrievalWithoutEmbeddingsTests(Fixture):
    """The default. Nothing about these answers may change."""

    def test_retrieval_is_lexical_when_the_feature_is_off(self):
        self._document("Returns are accepted within fourteen days of purchase.")

        with patch.object(embeddings, "embed_query") as embed_query:
            retrieval = knowledge.retrieve(self.tenant.pk, "how many days for returns?")

        embed_query.assert_not_called()
        self.assertTrue(retrieval)
        self.assertGreater(retrieval.relevance, 0)

    @override_settings(**ON)
    def test_the_feature_being_on_with_nothing_indexed_still_answers_lexically(self):
        # The state every tenant passes through the moment it is switched on.
        self._document("Returns are accepted within fourteen days of purchase.")

        with patch.object(embeddings, "embed_query", return_value=_vector(1.0)):
            retrieval = knowledge.retrieve(self.tenant.pk, "returns")

        self.assertTrue(retrieval)

    @override_settings(**ON)
    def test_an_embedding_outage_does_not_cost_the_answer(self):
        self._document("Returns are accepted within fourteen days of purchase.")

        def _boom(*args, **kwargs):
            raise embeddings.requests.RequestException("endpoint down")

        with patch.object(embeddings.requests, "post", _boom):
            retrieval = knowledge.retrieve(self.tenant.pk, "returns")

        self.assertTrue(retrieval)
        self.assertGreater(retrieval.relevance, 0)


class HybridRetrievalTests(Fixture):
    @override_settings(**ON, AI_EMBEDDING_WEIGHT=1.0)
    def test_a_chunk_sharing_no_words_with_the_question_can_still_win(self):
        # The entire reason embeddings are here: the policy never says "days".
        document = self._document(
            "Nothing about shipping at all here.\n\nYou may send an item back within a fortnight of buying it."
        )
        irrelevant, policy = list(KnowledgeChunk.objects.filter(document=document).order_by("ordinal"))
        self._embed_chunk(irrelevant, _vector(0.0, 1.0))
        self._embed_chunk(policy, _vector(1.0, 0.0))

        with patch.object(embeddings, "embed_query", return_value=_vector(1.0, 0.0)):
            retrieval = knowledge.retrieve(self.tenant.pk, "how many days do I have?")

        self.assertEqual(retrieval.chunks[0].pk, policy.pk)

    @override_settings(**ON, AI_EMBEDDING_WEIGHT=0.5)
    def test_a_blended_score_needs_both_halves_to_win(self):
        document = self._document(
            "Returns are accepted within fourteen days.\n\nA fortnight is the window for sending things back."
        )
        lexical_match, semantic_match = list(KnowledgeChunk.objects.filter(document=document).order_by("ordinal"))
        # The lexical match is also a decent semantic match; the semantic-only
        # one shares no query terms at all.
        self._embed_chunk(lexical_match, _vector(0.9, 0.4))
        self._embed_chunk(semantic_match, _vector(1.0, 0.0))

        with patch.object(embeddings, "embed_query", return_value=_vector(1.0, 0.0)):
            retrieval = knowledge.retrieve(self.tenant.pk, "returns within days")

        self.assertEqual(retrieval.chunks[0].pk, lexical_match.pk)

    @override_settings(**ON, AI_EMBEDDING_WEIGHT=0.5)
    def test_a_blended_relevance_stays_inside_zero_and_one(self):
        document = self._document("Returns are accepted within fourteen days.")
        chunk = KnowledgeChunk.objects.get(document=document)
        self._embed_chunk(chunk, _vector(1.0, 0.0))

        with patch.object(embeddings, "embed_query", return_value=_vector(1.0, 0.0)):
            retrieval = knowledge.retrieve(self.tenant.pk, "returns within fourteen days")

        self.assertGreaterEqual(retrieval.relevance, 0.0)
        self.assertLessEqual(retrieval.relevance, 1.0)

    @override_settings(**ON, AI_EMBEDDING_WEIGHT=1.0)
    def test_another_tenants_chunk_cannot_be_retrieved_by_vector_either(self):
        other = Tenant.objects.create(name="Rival", slug="emb-hybrid-rival", kind=Tenant.Kind.CUSTOMER)
        other_base = KnowledgeBase.objects.create(tenant=other, name="General", is_default=True)
        theirs = self._document("Our margin is forty percent.", tenant=other, base=other_base)
        self._embed_chunk(KnowledgeChunk.objects.get(document=theirs), _vector(1.0, 0.0))
        mine = self._document("We are open at nine.")
        self._embed_chunk(KnowledgeChunk.objects.get(document=mine), _vector(0.0, 1.0))

        with patch.object(embeddings, "embed_query", return_value=_vector(1.0, 0.0)):
            retrieval = knowledge.retrieve(self.tenant.pk, "what is the margin?")

        self.assertNotIn(theirs.pk, [chunk.document_id for chunk in retrieval.chunks])

    @override_settings(**ON, AI_EMBEDDING_WEIGHT=1.0)
    def test_a_question_of_only_stop_words_can_still_be_answered_semantically(self):
        # It has no lexical signal whatever, so before embeddings this was
        # always nothing.
        document = self._document("You may send an item back within a fortnight.")
        chunk = KnowledgeChunk.objects.get(document=document)
        self._embed_chunk(chunk, _vector(1.0, 0.0))

        with patch.object(embeddings, "embed_query", return_value=_vector(1.0, 0.0)):
            retrieval = knowledge.retrieve(self.tenant.pk, "how do I do that")

        self.assertTrue(retrieval)


class IndexingTests(Fixture):
    @override_settings(**ON)
    def test_indexing_writes_one_vector_per_chunk(self):
        document = self._document(TWO_PARAGRAPHS)
        seen = {}

        def _post(url, headers=None, json=None, timeout=None):
            seen["url"] = url
            seen["payload"] = json
            return _Response({"data": [
                {"index": index, "embedding": _vector(1.0, 0.0)} for index in range(len(json["input"]))
            ]})

        with patch.object(embeddings.requests, "post", _post):
            written = embeddings.index_document(document)

        self.assertEqual(written, 2)
        self.assertEqual(KnowledgeEmbedding.objects.filter(chunk__document=document).count(), 2)
        self.assertTrue(seen["url"].endswith("/embeddings"))
        self.assertEqual(seen["payload"]["model"], "test-embed")

    @override_settings(**ON)
    def test_reindexing_replaces_vectors_rather_than_adding_to_them(self):
        document = self._document("Returns take fourteen days.")

        def _post(url, headers=None, json=None, timeout=None):
            return _Response({"data": [
                {"index": index, "embedding": _vector(1.0, 0.0)} for index in range(len(json["input"]))
            ]})

        with patch.object(embeddings.requests, "post", _post):
            embeddings.index_document(document)
            embeddings.index_document(document)

        self.assertEqual(KnowledgeEmbedding.objects.filter(chunk__document=document).count(), 1)

    @override_settings(**ON)
    def test_vectors_returned_out_of_order_are_put_back_in_order(self):
        # Nothing downstream could detect a chunk stored under another chunk's
        # meaning, so this is checked here or not at all.
        document = self._document(TWO_PARAGRAPHS)
        first, second = list(KnowledgeChunk.objects.filter(document=document).order_by("ordinal"))

        def _post(url, headers=None, json=None, timeout=None):
            return _Response({"data": [
                {"index": 1, "embedding": _vector(0.0, 1.0)},
                {"index": 0, "embedding": _vector(1.0, 0.0)},
            ]})

        with patch.object(embeddings.requests, "post", _post):
            embeddings.index_document(document)

        self.assertAlmostEqual(embeddings.unpack(KnowledgeEmbedding.objects.get(chunk=first).vector)[0], 1.0, places=5)
        self.assertAlmostEqual(embeddings.unpack(KnowledgeEmbedding.objects.get(chunk=second).vector)[1], 1.0, places=5)

    @override_settings(**ON)
    def test_a_short_count_from_the_endpoint_is_refused(self):
        document = self._document(TWO_PARAGRAPHS)

        def _post(url, headers=None, json=None, timeout=None):
            return _Response({"data": [{"index": 0, "embedding": _vector(1.0)}]})

        with patch.object(embeddings.requests, "post", _post):
            with self.assertRaises(embeddings.EmbeddingError):
                embeddings.index_document(document)

    @override_settings(AI_EMBEDDINGS_ENABLED=False)
    def test_nothing_is_indexed_or_queued_when_the_feature_is_off(self):
        with patch("apps.ai.tasks.embed_document_task.delay") as queued:
            document = self._document("Returns take fourteen days.")

        queued.assert_not_called()
        self.assertEqual(embeddings.index_document(document), 0)

    @override_settings(**ON)
    def test_chunking_a_document_queues_its_embedding(self):
        # Calls `rebuild` directly rather than through the fixture, which
        # suppresses exactly the call this test is about.
        document = KnowledgeDocument.objects.create(
            tenant=self.tenant, knowledge_base=self.base, title="Doc", content="Returns take fourteen days.",
        )

        with patch("apps.ai.tasks.embed_document_task.delay") as queued:
            knowledge.rebuild(document)

        queued.assert_called_once_with(document.pk)

    @override_settings(AI_EMBEDDINGS_ENABLED=True, OPENAI_API_KEY="", AI_API_KEY="")
    def test_a_tenant_with_no_usable_key_is_left_lexical_rather_than_failing(self):
        document = self._document("Returns take fourteen days.")

        self.assertEqual(embeddings.index_document(document), 0)


class CredentialTests(Fixture):
    @override_settings(**ON)
    def test_an_anthropic_tenant_embeds_with_the_deployments_openai_key(self):
        # Anthropic has no embeddings API, so this is the only way a tenant on
        # Claude gets semantic retrieval at all.
        config = AIAgentConfig.objects.create(
            tenant=self.tenant, provider=AIAgentConfig.Provider.ANTHROPIC,
        )
        config.set_api_key("sk-ant-tenant")
        config.save()

        key, base = embeddings.credentials_for(config)

        self.assertEqual(key, "sk-deployment")
        self.assertEqual(base, "https://api.openai.com/v1")

    @override_settings(**ON)
    def test_an_openai_tenant_embeds_with_their_own_key(self):
        config = AIAgentConfig.objects.create(tenant=self.tenant, provider=AIAgentConfig.Provider.OPENAI)
        config.set_api_key("sk-tenant")
        config.save()

        key, _ = embeddings.credentials_for(config)

        self.assertEqual(key, "sk-tenant")

    @override_settings(**ON)
    def test_a_custom_tenant_embeds_against_their_own_endpoint(self):
        config = AIAgentConfig.objects.create(
            tenant=self.tenant, provider=AIAgentConfig.Provider.CUSTOM, base_url="http://localhost:11434/v1",
        )
        config.set_api_key("local-key")
        config.save()

        key, base = embeddings.credentials_for(config)

        self.assertEqual(key, "local-key")
        self.assertEqual(base, "http://localhost:11434/v1")


class StatsTests(Fixture):
    @override_settings(AI_EMBEDDINGS_ENABLED=False)
    def test_the_mode_reads_lexical_when_the_feature_is_off(self):
        self._document("Returns take fourteen days.")

        self.assertEqual(embeddings.stats(self.tenant.pk)["mode"], "lexical")

    @override_settings(**ON)
    def test_the_mode_reads_lexical_until_something_is_actually_indexed(self):
        # "On but empty" looks exactly like "off" from outside, which is why
        # the panel reports what retrieval is doing rather than what is set.
        self._document("Returns take fourteen days.")

        stats = embeddings.stats(self.tenant.pk)

        self.assertTrue(stats["enabled"])
        self.assertEqual(stats["embedded"], 0)
        self.assertEqual(stats["mode"], "lexical")

    @override_settings(**ON)
    def test_the_mode_reads_hybrid_once_vectors_exist(self):
        document = self._document("Returns take fourteen days.")
        self._embed_chunk(KnowledgeChunk.objects.get(document=document), _vector(1.0, 0.0))

        stats = embeddings.stats(self.tenant.pk)

        self.assertEqual(stats["embedded"], 1)
        self.assertEqual(stats["mode"], "hybrid")
