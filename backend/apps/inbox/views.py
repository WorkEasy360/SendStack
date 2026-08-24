import logging

from django.contrib.auth import get_user_model
from django.db.models import OuterRef, Subquery
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.audit.services import log_event
from apps.messaging.models import Message
from apps.messaging.serializers import MessageSerializer
from core.bulk import BulkActionMixin

from . import services
from .models import Conversation, ConversationLabel
from .serializers import ConversationLabelSerializer, ConversationNoteSerializer, ConversationSerializer, ReplySerializer
from .services import InboxError

logger = logging.getLogger("multisender")


def _notify_mentions(request, conversation, note):
    """
    Tell anyone named with @username that they were named.

    Notes are the one place in the inbox where agents talk to each other, so
    a mention that nobody is told about is a message into the void. Failures
    are swallowed: the note is already saved, and losing the notification is
    better than losing the note.
    """
    from apps.notifications.models import Notification
    from apps.notifications.services import notify
    from apps.teams.services import mentioned_users

    try:
        for user in mentioned_users(conversation.tenant_id, note.body):
            notify(
                user, kind=Notification.Kind.MENTION, actor=request.user,
                title=f"{request.user.username} mentioned you",
                body=note.body[:500],
                link=f"/inbox?conversation={conversation.pk}",
            )
    except Exception:
        logger.exception("Failed to deliver mentions for note %s.", note.pk)


class ConversationLabelViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ConversationLabelSerializer

    def get_queryset(self):
        return ConversationLabel.objects.filter(tenant=self.request.user.tenant)

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.user.tenant)


class ConversationViewSet(
    BulkActionMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Conversations are created implicitly by inbound messages, never via the API directly.

    Deleting one drops the whole thread (its messages and internal notes cascade);
    a later inbound message from the same contact simply starts a fresh conversation.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = ConversationSerializer
    filterset_fields = {
        "status": ["exact"],
        "assigned_agent": ["exact", "isnull"],
        "whatsapp_account": ["exact"],
        # Lets the inbox ask for "unread only" via ?unread_count__gt=0.
        "unread_count": ["gt"],
    }
    search_fields = ["contact__name", "contact__phone"]

    bulk_object_label = "conversation"
    bulk_actions = {
        "delete": "_bulk_delete",
        "close": "_bulk_close",
        "reopen": "_bulk_reopen",
        "mark_read": "_bulk_mark_read",
    }

    def get_queryset(self):
        # The list needs a one-line preview of the newest message; a subquery
        # keeps that at one extra join instead of a query per conversation.
        latest = Message.objects.filter(conversation=OuterRef("pk")).order_by("-queued_at", "-id")
        return Conversation.objects.filter(tenant=self.request.user.tenant).select_related(
            "contact", "assigned_agent", "whatsapp_account"
        ).prefetch_related("labels").annotate(
            last_message_preview=Subquery(latest.values("body")[:1]),
            last_message_direction=Subquery(latest.values("direction")[:1]),
        )

    def perform_destroy(self, instance):
        tenant, pk = instance.tenant, instance.pk
        message_count = instance.messages.count()
        instance.delete()
        log_event(
            request=self.request, action="inbox.conversation_deleted", tenant=tenant,
            object_type="conversation", object_id=pk, metadata={"messages_deleted": message_count},
        )

    def _bulk_delete(self, request, instance):
        self.perform_destroy(instance)

    def _bulk_close(self, request, instance):
        services.close(instance)

    def _bulk_reopen(self, request, instance):
        services.reopen(instance)

    def _bulk_mark_read(self, request, instance):
        services.mark_read(instance)

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        conversation = self.get_object()
        agent = None
        agent_id = request.data.get("agent_id")
        if agent_id:
            agent = get_user_model().objects.filter(pk=agent_id, tenant=request.user.tenant).first()
            if not agent:
                return Response({"detail": "Agent not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            services.assign_agent(conversation, agent)
        except InboxError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        log_event(request=request, action="inbox.assigned", tenant=conversation.tenant, object_type="conversation", object_id=conversation.pk)
        return Response(ConversationSerializer(conversation, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        conversation = services.close(self.get_object())
        return Response(ConversationSerializer(conversation, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        conversation = services.reopen(self.get_object())
        return Response(ConversationSerializer(conversation, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="mark-read")
    def mark_read(self, request, pk=None):
        conversation = self.get_object()
        services.mark_read(conversation)
        conversation.refresh_from_db()
        return Response(ConversationSerializer(conversation, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def reply(self, request, pk=None):
        conversation = self.get_object()
        serializer = ReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            message = services.send_reply(conversation, request.user, **serializer.validated_data)
        except InboxError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(MessageSerializer(message).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get", "post"])
    def notes(self, request, pk=None):
        conversation = self.get_object()

        if request.method == "POST":
            serializer = ConversationNoteSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            note = serializer.save(conversation=conversation, author=request.user)
            _notify_mentions(request, conversation, note)
            return Response(ConversationNoteSerializer(note).data, status=status.HTTP_201_CREATED)

        notes = conversation.notes.select_related("author")
        return Response(ConversationNoteSerializer(notes, many=True).data)

    @action(detail=True, methods=["get"])
    def messages(self, request, pk=None):
        """Newest first, so page 1 is always the live end of the thread and
        `next` walks backwards through the history. Clients render in reverse."""
        conversation = self.get_object()
        queryset = conversation.messages.order_by("-queued_at", "-id")
        page = self.paginate_queryset(queryset)
        serializer = MessageSerializer(page if page is not None else queryset, many=True)
        return self.get_paginated_response(serializer.data) if page is not None else Response(serializer.data)
