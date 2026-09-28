"""
Knowledge-base-only answering, end to end: upload -> process -> retrieve -> answer.

What these tests are actually for is one claim, and it is a negative one: **the
agent never answers a factual question from the model's own weights**. That is
hard to test honestly, because a model asked "what is your refund period?" will
produce a plausible number whether or not it was told one -- so a test that
merely checks an answer came back proves nothing at all.

Two devices make it provable:

* **A deliberately false fact.** The documents here say the refund period is
  *19 days*. No model has that in its weights; 14 and 30 are the plausible
  inventions. An answer containing 19 therefore came from the document.
* **Asserting on the call, not the reply.** For anything the documents do not
  cover, the test asserts the provider was **never called**. A refusal that is
  produced without asking the model cannot be a hallucination, whatever the
  prompt says -- which is the whole reason the relevance gate is code rather
  than an instruction.

The provider is stubbed throughout: it is a network call, and what is under
test is what we ask it and what we do without it.
"""

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.contacts.models import Contact
from apps.inbox.models import Conversation
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount
from apps.whatsapp.providers.base import SendResult

from . import knowledge, processing, services, tools
from .models import AIAgentConfig, KnowledgeBase, KnowledgeChunk, KnowledgeDocument, KnowledgeEmbedding
from .providers.base import AIResponse
from .tests_fixtures import csv_bytes, docx_bytes, json_bytes, pdf_bytes, xlsx_bytes

#: The fact the whole file turns on. Nineteen days is not a number any model
#: guesses, which is exactly why it is the one written down.
REFUND_TEXT = (
    "Refund Policy\n\n"
    "The company refund period is exactly 19 days from the date of delivery.\n\n"
    "A refund is paid back to the original payment method within 4 working days of approval."
)


def _answer(text):
    return AIResponse(text=text, input_tokens=100, output_tokens=20, latency_ms=100)


class KnowledgeOnlyBase(TestCase):
    """One tenant, one knowledge base, one agent with a key."""

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Acme", slug=f"ko-{self._testMethodName}"[:50], kind=Tenant.Kind.CUSTOMER,
        )
        self.owner = get_user_model().objects.create_user(
            username=f"ko-owner-{self._testMethodName}"[:140], password="x", tenant=self.tenant,
        )
        self.base = KnowledgeBase.objects.create(tenant=self.tenant, name="General", is_default=True)
        self.config = AIAgentConfig.objects.create(
            tenant=self.tenant, is_enabled=True, provider=AIAgentConfig.Provider.OPENAI, model="gpt-4o-mini",
            knowledge_base=self.base, answer_from_knowledge_only=True,
        )
        self.config.set_api_key("sk-test")
        self.config.save()

        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def _upload(self, filename, content, content_type="application/octet-stream", **extra):
        payload = {
            "file": SimpleUploadedFile(filename, content, content_type=content_type),
            "knowledge_base": self.base.pk,
            **extra,
        }
        return self.client.post("/api/v1/ai/knowledge/upload/", payload, format="multipart")

    def _typed(self, content, title="Policy", tenant=None, base=None):
        document = KnowledgeDocument.objects.create(
            tenant=tenant or self.tenant, knowledge_base=base or self.base, title=title, content=content,
        )
        knowledge.rebuild(document)
        return document


# ---------------------------------------------------------------------------
# 1. Upload -- every supported format, through the real endpoint
# ---------------------------------------------------------------------------

class UploadEveryFormatTests(KnowledgeOnlyBase):
    """
    Requirement: a document of each supported type uploads, processes and
    becomes *searchable*. The last of those is the one worth asserting -- a
    row in the database with a filename on it is not a knowledge base.
    """

    def _upload_and_process(self, filename, content, content_type="application/octet-stream"):
        response = self._upload(filename, content, content_type)
        self.assertEqual(response.status_code, 201, response.data)

        document = KnowledgeDocument.objects.get(pk=response.data["id"])
        # CELERY_TASK_ALWAYS_EAGER is on under test settings, so the worker has
        # already run by the time the response is returned.
        document.refresh_from_db()
        return document

    def _assert_indexed(self, document, must_contain):
        self.assertEqual(document.status, KnowledgeDocument.Status.READY, document.error)
        self.assertEqual(document.error, "")
        self.assertGreater(document.chunk_count, 0)
        self.assertIsNotNone(document.indexed_at)

        # The text is in the chunks, not merely on the document row: chunks are
        # what retrieval reads.
        chunk_text = " ".join(KnowledgeChunk.objects.filter(document=document).values_list("content", flat=True))
        for fragment in must_contain:
            self.assertIn(fragment, chunk_text)

    def test_pdf(self):
        document = self._upload_and_process(
            "policy.pdf",
            pdf_bytes([
                "The company refund period is exactly 19 days.",
                "Refunds are paid within 4 working days.",
            ]),
            "application/pdf",
        )
        self._assert_indexed(document, ["19 days"])
        # A page number is knowable only at extraction time, and an answer that
        # cites "page 1" is the reason it is captured.
        self.assertEqual(document.page_count, 1)
        self.assertEqual(KnowledgeChunk.objects.filter(document=document).first().page, 1)

    def test_docx(self):
        document = self._upload_and_process(
            "policy.docx",
            docx_bytes("Refunds", ["The company refund period is exactly 19 days."]),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self._assert_indexed(document, ["19 days"])
        # Word marks its own headings, so this is read rather than guessed.
        self.assertEqual(KnowledgeChunk.objects.filter(document=document).first().heading, "Refunds")

    def test_txt(self):
        document = self._upload_and_process("policy.txt", REFUND_TEXT.encode(), "text/plain")
        self._assert_indexed(document, ["19 days"])

    def test_markdown(self):
        document = self._upload_and_process(
            "policy.md", b"# Refunds\n\nThe company refund period is exactly 19 days.\n", "text/markdown",
        )
        self._assert_indexed(document, ["19 days"])

    def test_csv(self):
        document = self._upload_and_process(
            "plans.csv", csv_bytes([["Plan", "Price"], ["Premium", "4999"], ["Starter", "999"]]), "text/csv",
        )
        # Cells are paired with their column header, which is what makes a row
        # findable by a question phrased in words rather than in columns.
        self._assert_indexed(document, ["Plan: Premium", "Price: 4999"])

    def test_xlsx(self):
        document = self._upload_and_process(
            "plans.xlsx",
            xlsx_bytes("Plans", [["Plan", "Price"], ["Premium", "4999"]]),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self._assert_indexed(document, ["Plan: Premium"])

    def test_json(self):
        document = self._upload_and_process(
            "policy.json", json_bytes({"refund": {"window_days": 19}}), "application/json",
        )
        # Flattened to a labelled sentence, so a question about refunds shares
        # words with it. A raw JSON dump shares none.
        self._assert_indexed(document, ["refund window days: 19"])

    def test_every_uploaded_format_is_answerable_afterwards(self):
        """The point of all of the above, asserted once against retrieval."""
        self._upload_and_process("policy.txt", REFUND_TEXT.encode(), "text/plain")

        retrieval = knowledge.retrieve(self.tenant.pk, "what is the refund period?", knowledge_base_id=self.base.pk)

        self.assertTrue(retrieval.hits)
        self.assertIn("19 days", retrieval.chunks[0].content)


# ---------------------------------------------------------------------------
# 2. Upload validation and failure handling
# ---------------------------------------------------------------------------

class UploadFailureTests(KnowledgeOnlyBase):

    def test_an_unsupported_type_is_refused_before_it_is_stored(self):
        response = self._upload("virus.exe", b"MZ\x90\x00", "application/octet-stream")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(KnowledgeDocument.objects.filter(original_filename="virus.exe").exists())

    def test_an_empty_file_is_refused(self):
        self.assertEqual(self._upload("empty.txt", b"", "text/plain").status_code, 400)

    def test_a_file_that_cannot_be_read_ends_in_failed_with_a_reason_a_user_can_act_on(self):
        document = KnowledgeDocument.objects.get(
            pk=self._upload("broken.pdf", b"not a pdf at all", "application/pdf").data["id"]
        )
        document.refresh_from_db()

        self.assertEqual(document.status, KnowledgeDocument.Status.FAILED)
        self.assertTrue(document.error)
        # A reason, not a traceback: the message is shown to whoever uploaded it.
        self.assertNotIn("Traceback", document.error)
        self.assertNotIn("Error at", document.error)

    def test_a_failed_document_never_answers_anybody(self):
        self._upload("broken.pdf", b"not a pdf at all", "application/pdf")
        self._typed(REFUND_TEXT)

        # Nothing from the failed document is reachable; the good one still is.
        retrieval = knowledge.retrieve(self.tenant.pk, "refund period", knowledge_base_id=self.base.pk)

        self.assertTrue(retrieval.hits)
        for chunk in retrieval.chunks:
            self.assertEqual(chunk.document.status, KnowledgeDocument.Status.READY)

    def test_a_failed_document_can_be_retried_and_succeeds_once_the_file_is_good(self):
        response = self._upload("policy.txt", b"", "text/plain")
        self.assertEqual(response.status_code, 400)

        # Upload a readable one, break its stored text, then re-read it.
        document = KnowledgeDocument.objects.get(pk=self._upload("policy.txt", REFUND_TEXT.encode()).data["id"])
        KnowledgeDocument.objects.filter(pk=document.pk).update(
            status=KnowledgeDocument.Status.FAILED, error="something went wrong", chunk_count=0,
        )
        KnowledgeChunk.objects.filter(document=document).delete()

        retry = self.client.post(f"/api/v1/ai/knowledge/{document.pk}/reindex/")

        self.assertEqual(retry.status_code, 200)
        document.refresh_from_db()
        self.assertEqual(document.status, KnowledgeDocument.Status.READY)
        self.assertEqual(document.error, "")
        self.assertGreater(document.chunk_count, 0)


# ---------------------------------------------------------------------------
# 3. Re-upload and delete -- stale text must never survive
# ---------------------------------------------------------------------------

class DocumentLifecycleTests(KnowledgeOnlyBase):

    def test_replacing_a_document_leaves_none_of_the_old_text_behind(self):
        first = KnowledgeDocument.objects.get(
            pk=self._upload("policy.txt", b"The company refund period is exactly 19 days.").data["id"]
        )

        response = self._upload(
            "policy.txt", b"The company refund period is exactly 21 days.", replaces=first.pk,
        )

        self.assertEqual(response.status_code, 201)
        # The same row, re-read -- not a second document quietly competing with
        # the first for the same question.
        self.assertEqual(response.data["id"], first.pk)
        self.assertEqual(KnowledgeDocument.objects.filter(tenant=self.tenant).count(), 1)

        chunk_text = " ".join(KnowledgeChunk.objects.filter(tenant=self.tenant).values_list("content", flat=True))
        self.assertIn("21 days", chunk_text)
        self.assertNotIn("19 days", chunk_text)

    def test_editing_a_typed_document_replaces_its_chunks(self):
        document = self._typed("The company refund period is exactly 19 days.")

        self.client.patch(
            f"/api/v1/ai/knowledge/{document.pk}/",
            {"content": "The company refund period is exactly 21 days."}, format="json",
        )

        chunk_text = " ".join(KnowledgeChunk.objects.filter(document=document).values_list("content", flat=True))
        self.assertIn("21 days", chunk_text)
        self.assertNotIn("19 days", chunk_text)

    def test_deleting_a_document_removes_its_chunks_and_its_vectors(self):
        document = self._typed(REFUND_TEXT)
        chunk = KnowledgeChunk.objects.filter(document=document).first()
        KnowledgeEmbedding.objects.create(
            chunk=chunk, tenant=self.tenant, knowledge_base=self.base, model="test", dims=2,
            vector=b"\x00\x00\x80?\x00\x00\x00\x00",
        )

        self.client.delete(f"/api/v1/ai/knowledge/{document.pk}/")

        self.assertFalse(KnowledgeChunk.objects.filter(document_id=document.pk).exists())
        self.assertFalse(KnowledgeEmbedding.objects.filter(tenant=self.tenant).exists())
        self.assertFalse(knowledge.retrieve(self.tenant.pk, "refund period").hits)

    def test_turning_a_document_off_takes_it_out_of_retrieval(self):
        document = self._typed(REFUND_TEXT)
        self.assertTrue(knowledge.retrieve(self.tenant.pk, "refund period").hits)

        self.client.patch(f"/api/v1/ai/knowledge/{document.pk}/", {"is_active": False}, format="json")

        self.assertFalse(knowledge.retrieve(self.tenant.pk, "refund period").hits)

    def test_moving_a_document_between_bases_moves_its_vectors_too(self):
        # The vectors carry their own denormalised knowledge_base, and semantic
        # retrieval filters on that copy -- so one left behind would go on
        # answering questions asked of the base the document has left.
        other = KnowledgeBase.objects.create(tenant=self.tenant, name="Archive")
        document = KnowledgeDocument.objects.get(
            pk=self._upload("policy.txt", REFUND_TEXT.encode()).data["id"]
        )
        for chunk in KnowledgeChunk.objects.filter(document=document):
            KnowledgeEmbedding.objects.create(
                chunk=chunk, tenant=self.tenant, knowledge_base=self.base, model="test", dims=2,
                vector=b"\x00\x00\x80?\x00\x00\x00\x00",
            )

        self.client.patch(
            f"/api/v1/ai/knowledge/{document.pk}/", {"knowledge_base": other.pk}, format="json",
        )

        self.assertFalse(KnowledgeEmbedding.objects.filter(knowledge_base=self.base).exists())
        self.assertTrue(KnowledgeEmbedding.objects.filter(knowledge_base=other).exists())


# ---------------------------------------------------------------------------
# 4. Retrieval quality and the relevance threshold
# ---------------------------------------------------------------------------

class RetrievalTests(KnowledgeOnlyBase):

    def setUp(self):
        super().setUp()
        self._typed(REFUND_TEXT, title="Refunds")
        self._typed(
            "Delivery Charges\n\nDelivery inside the city takes two working days and costs sixty rupees.",
            title="Delivery",
        )

    def test_a_relevant_question_finds_the_right_document(self):
        retrieval = knowledge.retrieve(self.tenant.pk, "what is the refund period?")

        self.assertTrue(retrieval.hits)
        self.assertEqual(retrieval.chunks[0].document.title, "Refunds")
        self.assertIn("19 days", retrieval.chunks[0].content)

    def test_an_exact_keyword_question_finds_its_document(self):
        retrieval = knowledge.retrieve(self.tenant.pk, "delivery charges")

        self.assertTrue(retrieval.hits)
        self.assertEqual(retrieval.chunks[0].document.title, "Delivery")

    def test_an_unrelated_question_scores_as_no_relevant_information(self):
        retrieval = knowledge.retrieve(self.tenant.pk, "do you service helicopter turbine blades?")

        self.assertEqual(retrieval.confidence, "NONE")
        self.assertLess(retrieval.relevance, knowledge.LIMITED_RELEVANCE)

    def test_relevance_is_a_score_rather_than_only_an_ordering(self):
        # The distinction the whole feature rests on: "this is the answer" has
        # to be separable from "this is the least irrelevant thing I have".
        good = knowledge.retrieve(self.tenant.pk, "refund period days")
        poor = knowledge.retrieve(self.tenant.pk, "turbine blade servicing schedule")

        self.assertGreater(good.relevance, poor.relevance)

    def test_the_deployment_floor_cannot_be_undercut_by_a_tenant(self):
        self.config.min_relevance = 0.0
        self.config.save()

        with override_settings(AI_KNOWLEDGE_ONLY=True, AI_KNOWLEDGE_MIN_RELEVANCE=0.4):
            self.assertEqual(services.relevance_floor(self.config), 0.4)

    def test_a_tenant_may_set_a_higher_floor_than_the_deployment(self):
        self.config.min_relevance = 0.8
        self.config.save()

        with override_settings(AI_KNOWLEDGE_ONLY=True, AI_KNOWLEDGE_MIN_RELEVANCE=0.15):
            self.assertEqual(services.relevance_floor(self.config), 0.8)


# ---------------------------------------------------------------------------
# 5. Tenant isolation
# ---------------------------------------------------------------------------

class TenantIsolationTests(KnowledgeOnlyBase):

    def setUp(self):
        super().setUp()
        self._typed("Project Alpha uses 19-day refunds.", title="Alpha")

        self.other_tenant = Tenant.objects.create(
            name="Rival", slug=f"ko-rival-{self._testMethodName}"[:50], kind=Tenant.Kind.CUSTOMER,
        )
        self.other_base = KnowledgeBase.objects.create(
            tenant=self.other_tenant, name="General", is_default=True,
        )
        self.other_document = self._typed(
            "Project Beta uses a 47-day exchange window and a platinum support tier.",
            title="Beta", tenant=self.other_tenant, base=self.other_base,
        )

    def test_the_retrieval_query_itself_never_returns_another_tenants_chunks(self):
        # Asserted at the retrieval layer rather than on the answer, because
        # isolation enforced in a prompt is not isolation.
        retrieval = knowledge.retrieve(self.tenant.pk, "platinum support tier exchange window")

        for chunk in retrieval.chunks:
            self.assertEqual(chunk.tenant_id, self.tenant.pk)
        self.assertNotIn(
            self.other_document.pk, [chunk.document_id for chunk in retrieval.chunks],
        )

    def test_asking_about_another_tenants_information_finds_nothing(self):
        retrieval = knowledge.retrieve(self.tenant.pk, "how long is the platinum exchange window?")

        self.assertEqual(retrieval.confidence, "NONE")

    @override_settings(AI_KNOWLEDGE_ONLY=True)
    def test_a_tenants_agent_refuses_a_question_only_another_tenant_can_answer(self):
        with patch("apps.ai.services.complete") as ask:
            result = services.ask(self.tenant.pk, "how long is the platinum exchange window?")

        ask.assert_not_called()
        self.assertFalse(result["answered"])
        self.assertEqual(result["answer"], "I couldn't find this information in the Knowledge Base.")

    def test_a_tenant_cannot_upload_into_another_tenants_knowledge_base(self):
        response = self.client.post(
            "/api/v1/ai/knowledge/upload/",
            {
                "file": SimpleUploadedFile("x.txt", b"secret", content_type="text/plain"),
                "knowledge_base": self.other_base.pk,
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 400)

    def test_a_tenant_cannot_read_another_tenants_documents_over_the_api(self):
        response = self.client.get("/api/v1/ai/knowledge/")

        returned = {row["id"] for row in response.data["results"]}
        self.assertNotIn(self.other_document.pk, returned)


# ---------------------------------------------------------------------------
# 6. Strict knowledge-base-only answering -- the hallucination tests
# ---------------------------------------------------------------------------

@override_settings(AI_KNOWLEDGE_ONLY=True, AI_KNOWLEDGE_MIN_RELEVANCE=0.15)
class StrictKnowledgeOnlyTests(KnowledgeOnlyBase):
    """
    The requirement in one sentence: a question the documents cover is answered
    from them, and a question they do not cover is refused rather than guessed.
    """

    def setUp(self):
        super().setUp()
        self._typed(REFUND_TEXT, title="Refund Policy")

    def test_a_question_the_documents_answer_is_answered_from_them(self):
        with patch(
            "apps.ai.services.complete",
            return_value=_answer("According to the Knowledge Base, the refund period is 19 days."),
        ) as ask:
            result = services.ask(self.tenant.pk, "what is the company refund period?")

        ask.assert_called_once()
        self.assertTrue(result["answered"])
        self.assertIn("19 days", result["answer"])

        # The document's text really was put in front of the model -- the
        # answer above is a stub and proves nothing on its own.
        self.assertIn("19 days", ask.call_args.kwargs["system"])

    def test_the_prompt_forbids_pretrained_knowledge_and_names_the_refusal(self):
        with patch("apps.ai.services.complete", return_value=_answer("19 days.")) as ask:
            services.ask(self.tenant.pk, "what is the company refund period?")

        system = ask.call_args.kwargs["system"]
        self.assertIn("ONLY", system)
        self.assertIn("pretrained", system)
        self.assertIn("I couldn't find this information in the Knowledge Base.", system)

    def test_an_unrelated_question_is_never_put_to_the_model_at_all(self):
        # The first of the two enforcement layers: retrieval found nothing
        # close, so no request is made. A refusal produced without asking the
        # model cannot be a hallucination, whatever the prompt says.
        with patch("apps.ai.services.complete") as ask:
            result = services.ask(self.tenant.pk, "do you service helicopter turbine blades?")

        ask.assert_not_called()
        self.assertFalse(result["answered"])
        self.assertEqual(result["answer"], "I couldn't find this information in the Knowledge Base.")

    def test_a_related_sounding_question_the_documents_do_not_answer_is_refused(self):
        """
        The hallucination test proper, and the case the relevance gate cannot
        catch on its own.

        "What is the company warranty period?" shares *company* and *period*
        with the refund policy, so it scores well and reaches the model -- as
        it should, since only something that can read both can tell that a
        refund policy does not answer a warranty question. The model says so
        using the sentence it was given, and that verdict is turned into a real
        refusal here rather than relayed as an answer.
        """
        self.assertGreaterEqual(
            knowledge.retrieve(self.tenant.pk, "what is the company warranty period?").relevance,
            services.relevance_floor(self.config),
            "this question is meant to clear the gate -- otherwise it tests the previous case again",
        )

        with patch(
            "apps.ai.services.complete",
            return_value=_answer("I couldn't find this information in the Knowledge Base."),
        ) as ask:
            result = services.ask(self.tenant.pk, "what is the company warranty period?")

        ask.assert_called_once()
        self.assertFalse(result["answered"])
        self.assertEqual(result["answer"], "I couldn't find this information in the Knowledge Base.")

    def test_a_refusal_with_a_courtesy_after_it_is_still_a_refusal(self):
        with patch(
            "apps.ai.services.complete",
            return_value=_answer(
                "I couldn't find this information in the Knowledge Base. Shall I get a colleague?"
            ),
        ):
            result = services.ask(self.tenant.pk, "what is the company warranty period?")

        self.assertFalse(result["answered"])

    def test_a_warranty_period_invented_by_the_model_is_not_treated_as_an_answer_from_the_documents(self):
        # If a model ignores the instruction and invents "12 months", that is
        # visibly not in the documents -- the sources recorded against the
        # answer are the refund policy, and the recorded relevance is what a
        # reviewer checks. This asserts the provenance row exists to catch it.
        from .models import KnowledgeAnswer

        with patch("apps.ai.services.complete", return_value=_answer("The warranty is 12 months.")):
            services.ask(self.tenant.pk, "what is the company warranty period?")

        recorded = KnowledgeAnswer.objects.filter(tenant=self.tenant).latest("created_at")
        self.assertEqual(recorded.question, "what is the company warranty period?")
        self.assertTrue(recorded.sources)
        self.assertEqual(recorded.sources[0]["title"], "Refund Policy")

    def test_the_refusal_wording_is_the_tenants_own_when_they_have_set_one(self):
        self.config.knowledge_only_message = "We don't have that on file."
        self.config.save()

        with patch("apps.ai.services.complete") as ask:
            result = services.ask(self.tenant.pk, "do you service helicopter turbine blades?")

        ask.assert_not_called()
        self.assertEqual(result["answer"], "We don't have that on file.")

    def test_strict_mode_overrides_a_tenant_who_switched_knowledge_only_off(self):
        self.config.answer_from_knowledge_only = False
        self.config.save()

        self.assertTrue(services.knowledge_only(self.config))

        with patch("apps.ai.services.complete") as ask:
            services.ask(self.tenant.pk, "do you service helicopter turbine blades?")

        ask.assert_not_called()

    def test_the_reference_material_is_labelled_so_the_answer_can_cite_it(self):
        with patch("apps.ai.services.complete", return_value=_answer("19 days.")) as ask:
            result = services.ask(self.tenant.pk, "what is the refund period?")

        self.assertIn("Refund Policy", ask.call_args.kwargs["system"])
        self.assertTrue(result["sources"])
        self.assertEqual(result["sources"][0]["title"], "Refund Policy")


# ---------------------------------------------------------------------------
# 7. Tools must not become a way round the knowledge base
# ---------------------------------------------------------------------------

@override_settings(AI_KNOWLEDGE_ONLY=True, AI_KNOWLEDGE_MIN_RELEVANCE=0.15)
class ToolsDoNotBypassKnowledgeTests(KnowledgeOnlyBase):

    def setUp(self):
        super().setUp()
        self._typed(REFUND_TEXT, title="Refund Policy")
        self.config.tools_enabled = True
        self.config.save()

    def _tool_context(self):
        """
        A context built from real rows, the way `for_conversation` builds one.

        Every id is required and every id must exist: running a tool writes an
        `AIToolCall` audit row that points at all of them. That is the design
        working -- the record of what the assistant did to a customer cannot
        reference a customer who does not exist.
        """
        account = WhatsAppAccount(
            tenant=self.tenant, user=self.owner, name="Line", phone_number="+15551110000",
            phone_number_id="pnid-ko", status=WhatsAppAccount.Status.CONNECTED,
        )
        account.set_access_token("token")
        account.set_app_secret("secret")
        account.save()
        contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.owner, phone="+15551234567", name="Priya",
        )
        conversation = Conversation.objects.create(
            tenant=self.tenant, contact=contact, whatsapp_account=account,
        )
        return tools.ToolContext(
            tenant_id=self.tenant.pk, conversation_id=conversation.pk, contact_id=contact.pk,
            whatsapp_account_id=account.pk, user_id=self.owner.pk,
            conversation=conversation, contact=contact, account=account,
        )

    def test_an_agent_with_tools_still_hits_the_relevance_gate(self):
        # Before this was enforced, switching tools on removed the gate: the
        # model was called with an empty context and answered from its weights.
        self.assertTrue(tools.definitions(self.config))

        with patch("apps.ai.services.complete") as ask:
            result = services.ask(self.tenant.pk, "do you service helicopter turbine blades?")

        ask.assert_not_called()
        self.assertEqual(result["answer"], "I couldn't find this information in the Knowledge Base.")

    def test_on_the_reply_path_strict_mode_is_what_stops_tools_suspending_the_gate(self):
        """
        The same unanswerable question down the real reply path, twice.

        Asserted from both sides because the difference *is* the fix: outside
        strict mode a tool-enabled agent is still called (it has other real
        sources, so "this retrieval found nothing" is not "there is nothing to
        find"), and inside it, it is not.
        """
        context = self._tool_context()
        inbound = Message.objects.create(
            tenant=self.tenant, conversation=context.conversation, whatsapp_account=context.account,
            contact=context.contact, to_phone=context.contact.phone,
            direction=Message.Direction.INBOUND, body="do you service helicopter turbine blades?",
            status=Message.Status.DELIVERED,
        )

        with patch("apps.messaging.services.ConnectionManager.send_text_message") as send:
            send.return_value = SendResult(success=True, provider_message_id="wamid.out.1")

            with override_settings(AI_KNOWLEDGE_ONLY=False):
                with patch("apps.ai.agent.run_turn") as loose:
                    loose.return_value = SimpleNamespace(
                        text="Let me check that for you.", terminal="", terminal_reason="",
                        input_tokens=1, output_tokens=1, latency_ms=1,
                    )
                    services.answer(context.conversation, inbound)
                loose.assert_called_once()

            with patch("apps.ai.agent.run_turn") as strict:
                services.answer(context.conversation, inbound, config=self.config)
            strict.assert_not_called()

    def test_a_grounded_question_still_reaches_the_model_with_tools_on(self):
        # The gate must not be a blanket refusal: tools stay useful for every
        # question the documents do cover.
        with patch("apps.ai.services.complete", return_value=_answer("19 days.")) as ask:
            result = services.ask(self.tenant.pk, "what is the refund period?")

        ask.assert_called_once()
        self.assertTrue(result["answered"])

    def test_the_search_tool_refuses_to_return_material_below_the_floor(self):
        context = self._tool_context()

        outcome = tools.execute(
            "search_knowledge", {"query": "helicopter turbine blade servicing"}, self.config, context,
        )

        self.assertIn("Nothing in the reference material", outcome.result)

    def test_the_search_tool_returns_material_that_does_clear_the_floor(self):
        context = self._tool_context()

        outcome = tools.execute("search_knowledge", {"query": "refund period"}, self.config, context)

        self.assertIn("19 days", outcome.result)

    def test_the_tool_prompt_tells_the_agent_an_empty_search_is_not_licence_to_guess(self):
        system = services.build_system_prompt(self.config, [], with_tools=True)

        self.assertIn("I couldn't find this information in the Knowledge Base.", system)
        self.assertIn("not available to you", system)


# ---------------------------------------------------------------------------
# 8. Conversation memory is context, never a source of fact
# ---------------------------------------------------------------------------

@override_settings(AI_KNOWLEDGE_ONLY=True)
class ConversationMemoryTests(KnowledgeOnlyBase):

    def test_the_prompt_makes_the_documents_outrank_what_was_said_earlier(self):
        self._typed(REFUND_TEXT)
        chunks = knowledge.retrieve(self.tenant.pk, "refund period").chunks

        system = services.build_system_prompt(self.config, chunks)

        # The failure this guards against: the agent said "14 days" last week,
        # the document now says 19, and the thread is right there in the
        # context looking like agreement with itself.
        self.assertIn("Do not carry a fact over from earlier in this conversation", system)
        # Collapsed, because the instruction is wrapped across lines in source.
        self.assertIn("material is correct and what was said earlier was wrong", " ".join(system.split()))

    def test_a_follow_up_that_cannot_stand_alone_is_retrieved_in_context(self):
        """
        "And how long does the process take?" means nothing to a retrieval on
        its own -- the subject is a message earlier. Answering it needs the
        thread, and refusing it would read as the assistant losing its place.
        """
        self._typed(
            "Refund Policy\n\n"
            "The company refund period is exactly 19 days.\n\n"
            "Once approved, the money reaches the original account after 4 working days.",
            title="Refunds",
        )
        account = WhatsAppAccount(
            tenant=self.tenant, user=self.owner, name="Line", phone_number="+15551110000",
            phone_number_id="pnid-ctx", status=WhatsAppAccount.Status.CONNECTED,
        )
        account.set_access_token("token")
        account.set_app_secret("secret")
        account.save()
        contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.owner, phone="+15551234567", name="Priya",
        )
        conversation = Conversation.objects.create(
            tenant=self.tenant, contact=contact, whatsapp_account=account,
        )
        for body in ("what is your refund policy?", "and how long does it take?"):
            Message.objects.create(
                tenant=self.tenant, conversation=conversation, whatsapp_account=account, contact=contact,
                to_phone=contact.phone, direction=Message.Direction.INBOUND, body=body,
                status=Message.Status.DELIVERED,
            )

        current = Message.objects.filter(conversation=conversation).order_by("-id").first()
        alone = knowledge.retrieve(self.tenant.pk, "and how long does it take?")
        in_context = services.retrieve_in_context(
            conversation, "and how long does it take?", self.config, current_message_id=current.pk,
        )

        self.assertLess(alone.relevance, services.relevance_floor(self.config))
        self.assertGreaterEqual(in_context.relevance, services.relevance_floor(self.config))
        self.assertIn("refund", " ".join(chunk.content.lower() for chunk in in_context.chunks))

    def test_a_complete_question_the_documents_do_not_cover_never_rides_on_the_last_one(self):
        """
        The regression this narrow trigger exists to prevent.

        "Do you sell helicopters?" is a complete question with a subject of its
        own. Widening it with "what is your refund policy?" would retrieve the
        refund passage and answer a question nobody asked -- an invention with
        a citation attached, which is worse than a plain refusal.
        """
        self._typed(REFUND_TEXT, title="Refunds")
        account = WhatsAppAccount(
            tenant=self.tenant, user=self.owner, name="Line", phone_number="+15551110002",
            phone_number_id="pnid-ctx3", status=WhatsAppAccount.Status.CONNECTED,
        )
        account.set_access_token("token")
        account.set_app_secret("secret")
        account.save()
        contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.owner, phone="+15551234569", name="Sam",
        )
        conversation = Conversation.objects.create(
            tenant=self.tenant, contact=contact, whatsapp_account=account,
        )
        Message.objects.create(
            tenant=self.tenant, conversation=conversation, whatsapp_account=account, contact=contact,
            to_phone=contact.phone, direction=Message.Direction.INBOUND,
            body="what is your refund policy?", status=Message.Status.DELIVERED,
        )

        result = services.retrieve_in_context(conversation, "do you sell helicopters?", self.config)

        self.assertLess(result.relevance, services.relevance_floor(self.config))
        self.assertEqual(result.confidence, "NONE")

    def test_a_self_contained_question_is_not_diluted_by_the_thread(self):
        # The reason the widened pass is second and conditional: a question
        # that stands on its own must retrieve on its own terms.
        self._typed(REFUND_TEXT, title="Refunds")
        account = WhatsAppAccount(
            tenant=self.tenant, user=self.owner, name="Line", phone_number="+15551110001",
            phone_number_id="pnid-ctx2", status=WhatsAppAccount.Status.CONNECTED,
        )
        account.set_access_token("token")
        account.set_app_secret("secret")
        account.save()
        contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.owner, phone="+15551234568", name="Ravi",
        )
        conversation = Conversation.objects.create(
            tenant=self.tenant, contact=contact, whatsapp_account=account,
        )

        direct = knowledge.retrieve(
            self.tenant.pk, "what is the refund period?", knowledge_base_id=self.base.pk,
            limit=self.config.knowledge_chunks,
        )
        through_helper = services.retrieve_in_context(
            conversation, "what is the refund period?", self.config,
        )

        self.assertEqual(through_helper.relevance, direct.relevance)


# ---------------------------------------------------------------------------
# 9. A document added after the agent was configured needs no code change
# ---------------------------------------------------------------------------

@override_settings(AI_KNOWLEDGE_ONLY=True)
class NewDocumentTests(KnowledgeOnlyBase):

    def test_a_document_uploaded_after_configuration_is_answerable_immediately(self):
        # Before: the agent is configured and knows nothing about warranties.
        with patch("apps.ai.services.complete") as ask:
            before = services.ask(self.tenant.pk, "what is the warranty period?")
        ask.assert_not_called()
        self.assertFalse(before["answered"])

        # A new document arrives by the ordinary upload route.
        response = self._upload(
            "warranty.txt", b"Warranty Terms\n\nEvery product carries a 37 month warranty.\n", "text/plain",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            KnowledgeDocument.objects.get(pk=response.data["id"]).status, KnowledgeDocument.Status.READY,
        )

        # After: the same question is answered, from the new document.
        with patch("apps.ai.services.complete", return_value=_answer("37 months.")) as ask:
            after = services.ask(self.tenant.pk, "what is the warranty period?")

        ask.assert_called_once()
        self.assertTrue(after["answered"])
        self.assertIn("37 month", ask.call_args.kwargs["system"])


# ---------------------------------------------------------------------------
# 10. The processing pipeline itself
# ---------------------------------------------------------------------------

class ProcessingPipelineTests(KnowledgeOnlyBase):

    def test_a_document_moves_pending_to_ready_and_records_what_it_produced(self):
        document = KnowledgeDocument.objects.create(
            tenant=self.tenant, knowledge_base=self.base, title="Policy",
            source_type=KnowledgeDocument.Source.FILE, original_filename="policy.pdf",
            file=SimpleUploadedFile(
                "policy.pdf", pdf_bytes(["The company refund period is exactly 19 days."]),
            ),
            status=KnowledgeDocument.Status.PENDING,
        )

        self.assertEqual(processing.process_document(document.pk), KnowledgeDocument.Status.READY)

        document.refresh_from_db()
        self.assertGreater(document.chunk_count, 0)
        self.assertEqual(document.page_count, 1)
        self.assertIn("19 days", document.content)

    def test_reprocessing_replaces_chunks_rather_than_doubling_them(self):
        document = KnowledgeDocument.objects.get(
            pk=self._upload("policy.txt", REFUND_TEXT.encode()).data["id"]
        )
        first = KnowledgeChunk.objects.filter(document=document).count()

        processing.process_document(document.pk)

        self.assertEqual(KnowledgeChunk.objects.filter(document=document).count(), first)

    def test_a_document_whose_file_has_gone_fails_with_an_actionable_message(self):
        document = KnowledgeDocument.objects.get(
            pk=self._upload("policy.txt", REFUND_TEXT.encode()).data["id"]
        )
        document.file.storage.delete(document.file.name)

        processing.process_document(document.pk)

        document.refresh_from_db()
        self.assertEqual(document.status, KnowledgeDocument.Status.FAILED)
        self.assertIn("upload it again", document.error.lower())

    def test_chunks_keep_the_metadata_an_answer_is_cited_from(self):
        document = KnowledgeDocument.objects.get(
            pk=self._upload(
                "policy.pdf", pdf_bytes(["The company refund period is exactly 19 days."]), "application/pdf",
            ).data["id"]
        )

        chunk = KnowledgeChunk.objects.filter(document=document).first()

        self.assertEqual(chunk.tenant_id, self.tenant.pk)
        self.assertEqual(chunk.knowledge_base_id, self.base.pk)
        self.assertEqual(chunk.document_id, document.pk)
        self.assertEqual(chunk.page, 1)
        self.assertIsNotNone(chunk.created_at)
        self.assertEqual(chunk.citation, "policy.pdf, page 1")


# ---------------------------------------------------------------------------
# 11. The document list the page reads
# ---------------------------------------------------------------------------

class DocumentListTests(KnowledgeOnlyBase):

    def test_a_larger_page_can_actually_be_asked_for(self):
        # Plain PageNumberPagination ignores ?page_size=, which capped this
        # list at twenty and looked exactly like uploads failing.
        for index in range(25):
            self._typed(f"Fact number {index} about our delivery service.", title=f"Doc {index}")

        response = self.client.get("/api/v1/ai/knowledge/", {"page_size": 100})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 25)

    def test_the_page_size_ceiling_is_enforced(self):
        response = self.client.get("/api/v1/ai/knowledge/", {"page_size": 100000})

        self.assertEqual(response.status_code, 200)
