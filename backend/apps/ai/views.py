from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework import generics, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import log_event
from apps.inbox.models import Conversation

from . import knowledge, services
from . import recommendations as ai_recommendations
from . import status as ai_status
from .models import (
    AIAgentConfig,
    AIUsageLog,
    KnowledgeAnswer,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeEmbedding,
)
from .providers.base import AIProviderError
from .serializers import (
    AIAgentConfigSerializer,
    AIUsageLogSerializer,
    ConversationActionSerializer,
    KnowledgeAnswerSerializer,
    KnowledgeAskSerializer,
    KnowledgeBaseSerializer,
    KnowledgeDocumentSerializer,
    KnowledgeSearchSerializer,
    KnowledgeUploadSerializer,
)
from .tasks import process_document_task


class AIAgentConfigView(generics.RetrieveUpdateAPIView):
    """
    One agent per tenant, reachable without knowing its id.

    Readable on every plan and writable only on a plan that includes the
    agent. Reading has to stay open: the settings screen renders the locked
    state from this response, and a customer being shown what they would get
    is the point of showing it at all. Writing does not -- configuring a
    feature you do not have is how a downgraded tenant quietly keeps one.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = AIAgentConfigSerializer

    def get_object(self):
        config, _ = AIAgentConfig.objects.get_or_create(tenant=self.request.user.tenant)
        return config

    def update(self, request, *args, **kwargs):
        services.require_agent(request.user.tenant_id)
        return super().update(request, *args, **kwargs)


class ClearAPIKeyView(APIView):
    """Deleting the key is its own action, because a blank field means 'unchanged'."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        # Not gated: removing a key is always allowed. A tenant who has lost
        # the feature should still be able to take their own credential back
        # out of somebody else's database.
        config, _ = AIAgentConfig.objects.get_or_create(tenant=request.user.tenant)
        config.set_api_key(None)
        config.save(update_fields=["api_key_encrypted", "updated_at"])
        return Response(AIAgentConfigSerializer(config).data)


class KnowledgeBaseViewSet(viewsets.ModelViewSet):
    """
    The named collections a tenant keeps their documents in.

    Listing them creates the first one, so a user who opens the page sees a
    place to put a document rather than an empty screen with a form in front
    of the thing they wanted.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = KnowledgeBaseSerializer
    search_fields = ["name", "description"]

    def get_queryset(self):
        return (
            KnowledgeBase.objects.filter(tenant=self.request.user.tenant)
            .annotate(
                document_count=Count("documents", distinct=True),
                chunk_total=Count("chunks", distinct=True),
                processing_count=Count(
                    "documents",
                    filter=Q(documents__status__in=[
                        KnowledgeDocument.Status.PENDING, KnowledgeDocument.Status.PROCESSING,
                    ]),
                    distinct=True,
                ),
                failed_count=Count(
                    "documents", filter=Q(documents__status=KnowledgeDocument.Status.FAILED), distinct=True,
                ),
            )
        )

    def list(self, request, *args, **kwargs):
        if not KnowledgeBase.objects.filter(tenant=request.user.tenant).exists():
            services.default_knowledge_base(request.user.tenant_id)
        return super().list(request, *args, **kwargs)

    def perform_create(self, serializer):
        tenant = self.request.user.tenant
        first = not KnowledgeBase.objects.filter(tenant=tenant).exists()
        serializer.save(tenant=tenant, created_by=self.request.user, is_default=first)

    def perform_destroy(self, instance):
        # Documents cascade with the base, and so do their chunks -- which is
        # the point: deleting a knowledge base has to leave nothing behind
        # that a customer could still be answered from.
        instance.delete()

    @action(detail=True, methods=["post"])
    def ask(self, request, pk=None):
        """
        Test the knowledge base without messaging anybody.

        Runs the exact retrieval and prompt the customer-facing path uses --
        a test on a different code path would build confidence in behaviour
        that is not the behaviour.
        """
        base = self.get_object()
        serializer = KnowledgeAskSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        result = services.ask(
            request.user.tenant_id, serializer.validated_data["question"], knowledge_base_id=base.pk,
        )
        return Response(result)

    @action(detail=True, methods=["get"])
    def recommendations(self, request, pk=None):
        services.require_agent(request.user.tenant_id)
        base = self.get_object()
        return Response({"results": ai_recommendations.build(request.user.tenant, knowledge_base=base)})


class KnowledgeDocumentViewSet(viewsets.ModelViewSet):
    """
    The material the agent may answer from.

    Typed documents are chunked on save rather than on a schedule: a tenant
    who has just corrected a price expects the next customer to be told the
    new one, not the one from before lunch. Uploaded documents go to the
    worker, because parsing a 300-page PDF is not something a browser should
    be made to wait for.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = KnowledgeDocumentSerializer
    filterset_fields = {"is_active": ["exact"], "knowledge_base": ["exact"], "status": ["exact"]}
    search_fields = ["title", "content", "original_filename"]

    def get_queryset(self):
        return KnowledgeDocument.objects.filter(tenant=self.request.user.tenant).select_related("knowledge_base")

    def perform_create(self, serializer):
        tenant = self.request.user.tenant
        base = serializer.validated_data.get("knowledge_base") or services.default_knowledge_base(tenant.pk)
        document = serializer.save(
            tenant=tenant, created_by=self.request.user, knowledge_base=base,
            source_type=KnowledgeDocument.Source.TEXT, status=KnowledgeDocument.Status.READY,
        )
        count = knowledge.rebuild(document)
        KnowledgeDocument.objects.filter(pk=document.pk).update(
            chunk_count=count, indexed_at=timezone.now(), error="",
        )

    def perform_update(self, serializer):
        document = serializer.save()
        if document.source_type == KnowledgeDocument.Source.FILE:
            # The text of an uploaded document comes from the file, so editing
            # its title or base must re-point the chunks, not re-chunk pasted
            # text that is not authoritative here.
            #
            # The vectors have to move with them. `knowledge_base` is
            # denormalised onto both tables, and semantic retrieval filters on
            # the embedding's copy -- so a document moved out of a base whose
            # vectors stayed behind goes on answering questions asked of the
            # base it just left, which is the isolation bug this denormalisation
            # is otherwise worth having.
            KnowledgeChunk.objects.filter(document=document).update(knowledge_base_id=document.knowledge_base_id)
            KnowledgeEmbedding.objects.filter(chunk__document=document).update(
                knowledge_base_id=document.knowledge_base_id,
            )
            return
        count = knowledge.rebuild(document)
        KnowledgeDocument.objects.filter(pk=document.pk).update(
            chunk_count=count, indexed_at=timezone.now(), status=KnowledgeDocument.Status.READY, error="",
        )

    @action(detail=False, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def upload(self, request):
        """
        Take a PDF/DOCX/TXT/CSV/XLSX and index it in the background.

        Returns immediately with the document in PENDING. The file is
        validated before it is stored -- extension, size and a sanity check on
        the declared content type -- so an unparseable upload is refused at
        the door rather than filling the media volume and failing on a worker.
        """
        serializer = KnowledgeUploadSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        upload = serializer.validated_data["file"]
        tenant = request.user.tenant
        base = serializer.validated_data.get("knowledge_base") or services.default_knowledge_base(tenant.pk)
        title = (serializer.validated_data.get("title") or "").strip() or upload.name.rsplit(".", 1)[0][:200]
        replaces = serializer.validated_data.get("replaces")

        if replaces is not None:
            # A new version of a document already here. Its chunks (and, by
            # cascade, its vectors) go now rather than when the new file
            # finishes parsing: between those two moments the document is
            # PENDING and therefore already excluded from retrieval, so
            # dropping them early costs nothing and guarantees the old text
            # cannot be quoted at a customer while the new one is being read.
            KnowledgeChunk.objects.filter(document=replaces).delete()
            document = replaces
            document.knowledge_base = base
            document.title = title
            document.content = ""
            document.source_type = KnowledgeDocument.Source.FILE
            document.file = upload
            document.original_filename = upload.name[:255]
            document.content_type = (getattr(upload, "content_type", "") or "")[:150]
            document.size_bytes = upload.size or 0
            document.page_count = 0
            document.chunk_count = 0
            document.status = KnowledgeDocument.Status.PENDING
            document.error = ""
            document.indexed_at = None
            document.save()
        else:
            document = KnowledgeDocument.objects.create(
                tenant=tenant, knowledge_base=base, created_by=request.user,
                title=title, content="", source_type=KnowledgeDocument.Source.FILE,
                file=upload, original_filename=upload.name[:255],
                content_type=(getattr(upload, "content_type", "") or "")[:150],
                size_bytes=upload.size or 0,
                status=KnowledgeDocument.Status.PENDING,
            )
        process_document_task.delay(document.pk)
        log_event(
            request=request, action="knowledge.document_uploaded", tenant=tenant,
            object_type="knowledge_document", object_id=document.pk,
            metadata={"filename": document.original_filename, "bytes": document.size_bytes},
        )
        return Response(KnowledgeDocumentSerializer(document).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def reindex(self, request, pk=None):
        """Read the document again — for a file that failed, or a parser that improved."""
        document = self.get_object()
        KnowledgeDocument.objects.filter(pk=document.pk).update(
            status=KnowledgeDocument.Status.PENDING, error="",
        )
        process_document_task.delay(document.pk)
        document.refresh_from_db()
        return Response(KnowledgeDocumentSerializer(document).data)

    @action(detail=False, methods=["post"])
    def search(self, request):
        """
        What the agent would retrieve for a question -- the way to find out
        why it answered as it did without sending anybody a message.
        """
        serializer = KnowledgeSearchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        base_id = request.data.get("knowledge_base") or None
        if base_id and not KnowledgeBase.objects.filter(pk=base_id, tenant=request.user.tenant).exists():
            return Response({"detail": "Choose one of your own knowledge bases."}, status=status.HTTP_400_BAD_REQUEST)

        retrieval = knowledge.retrieve(
            request.user.tenant_id, serializer.validated_data["query"],
            knowledge_base_id=base_id, limit=serializer.validated_data["limit"],
        )
        return Response({
            "confidence": retrieval.confidence,
            "relevance": round(retrieval.relevance, 3),
            "results": [
                {
                    "document_id": hit.chunk.document_id,
                    "document_title": hit.chunk.document.title,
                    "source_url": hit.chunk.document.source_url,
                    "citation": hit.chunk.citation,
                    "page": hit.chunk.page,
                    "score": round(hit.score, 3),
                    "content": hit.chunk.content,
                }
                for hit in retrieval.hits
            ],
        })


class KnowledgeAnswerViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    Every question the knowledge base has been asked, and what it answered.

    Read-only and append-only: this is the record you consult when a customer
    says "your bot told me X", and a record that can be edited is not one.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = KnowledgeAnswerSerializer
    filterset_fields = {"confidence": ["exact"], "knowledge_base": ["exact"], "answered": ["exact"], "is_test": ["exact"]}
    search_fields = ["question", "answer"]

    def get_queryset(self):
        return KnowledgeAnswer.objects.filter(tenant=self.request.user.tenant).select_related("contact")

    @action(detail=False, methods=["get"])
    def gaps(self, request):
        """
        The questions the knowledge base could not answer.

        The single most useful screen in the feature: it is a list of the
        documents this business is missing, written by their own customers.
        """
        queryset = (
            self.get_queryset()
            .filter(confidence=KnowledgeAnswer.Confidence.NONE, is_test=False)
            .order_by("-created_at")
        )
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page if page is not None else queryset, many=True)
        return self.get_paginated_response(serializer.data) if page is not None else Response(serializer.data)


class KnowledgeAskView(APIView):
    """Test the agent's knowledge without sending a message to anybody."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = KnowledgeAskSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        base = serializer.validated_data.get("knowledge_base")
        return Response(
            services.ask(
                request.user.tenant_id, serializer.validated_data["question"],
                knowledge_base_id=base.pk if base else None,
            )
        )


class KnowledgeRecommendationView(APIView):
    """What the uploaded documents suggest is worth switching on. Never acts."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        services.require_agent(request.user.tenant_id)
        return Response({"results": ai_recommendations.build(request.user.tenant)})


class AIStatusView(APIView):
    """
    Whether the assistant is actually working, in one request.

    The agent's configuration, the knowledge base's processing state, every
    connected WhatsApp number, what the automation ladder above the agent
    would answer first, and what the last call did. Read-only: it never calls
    a model, so it is safe for a dashboard to poll.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(ai_status.build(request.user.tenant_id))


class SuggestReplyView(APIView):
    """A draft for the agent to read and edit. Never sent by this endpoint."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        conversation = _conversation_for(request)
        if conversation is None:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            return Response({"suggestion": services.suggest_reply(conversation)})
        except AIProviderError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class SummariseView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        conversation = _conversation_for(request)
        if conversation is None:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            return Response({"summary": services.summarise(conversation)})
        except AIProviderError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


def _conversation_for(request):
    serializer = ConversationActionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    return Conversation.objects.filter(
        pk=serializer.validated_data["conversation"], tenant=request.user.tenant,
    ).select_related("contact", "whatsapp_account").first()


class AIUsageViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = AIUsageLogSerializer
    filterset_fields = {"outcome": ["exact"], "conversation": ["exact"]}

    def get_queryset(self):
        return AIUsageLog.objects.filter(tenant=self.request.user.tenant)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        queryset = self.get_queryset()
        totals = queryset.aggregate(input_tokens=Sum("input_tokens"), output_tokens=Sum("output_tokens"))
        by_outcome = dict(
            queryset.values_list("outcome").annotate(total=Count("id")).values_list("outcome", "total")
        )
        return Response({
            "calls": queryset.count(),
            "input_tokens": totals["input_tokens"] or 0,
            "output_tokens": totals["output_tokens"] or 0,
            "by_outcome": by_outcome,
        })
