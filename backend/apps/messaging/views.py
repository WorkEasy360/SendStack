from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.audit.services import log_event

from . import recovery
from .models import Message
from .serializers import MessageSerializer

_RESOLVE_ACTIONS = {"confirm_sent", "mark_failed", "retry_safe"}


class MessageViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Read-only: messages are created by campaign/automation services, never directly via the API."""

    permission_classes = [IsAuthenticated]
    serializer_class = MessageSerializer
    filterset_fields = ["campaign", "conversation", "status", "direction", "whatsapp_account"]

    def get_queryset(self):
        # select_related covers the account/contact names the serializer
        # exposes -- without it a 20-row page costs 40 extra queries.
        return Message.objects.filter(tenant=self.request.user.tenant).select_related("whatsapp_account", "contact")

    @action(detail=True, methods=["get"])
    def recovery(self, request, pk=None):
        """The send-attempt history and current recovery picture for one message."""
        return Response(recovery.message_recovery_detail(self.get_object()))

    @action(detail=True, methods=["post"])
    def reconcile(self, request, pk=None):
        """
        Re-run the automated classification for a message now, instead of
        waiting for the next sweep. Safe: it never blindly resends -- it only
        moves a message forward when it can prove what happened.
        """
        message = self.get_object()
        if message.status != Message.Status.SENDING:
            # Only a still-SENDING row is reconcilable by the automated path;
            # a RECOVERY_REQUIRED one needs an operator decision (resolve).
            return Response(
                {"detail": f"This message is {message.get_status_display().lower()}; nothing to reconcile."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        outcome = recovery.classify_and_recover(message, force=True)
        log_event(
            request=request, action="message.reconciled", tenant=message.tenant,
            object_type="message", object_id=message.pk, metadata={"outcome": outcome},
        )
        message.refresh_from_db()
        return Response(recovery.message_recovery_detail(message))

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        """
        Apply an operator's decision to a RECOVERY_REQUIRED message:
        ``confirm_sent`` / ``mark_failed`` / ``retry_safe``. ``retry_safe`` is
        refused unless the system can establish the send is safe to repeat.
        """
        message = self.get_object()
        chosen = (request.data or {}).get("action")
        if chosen not in _RESOLVE_ACTIONS:
            return Response(
                {"detail": f"action must be one of {sorted(_RESOLVE_ACTIONS)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            new_status = recovery.resolve_message(message, chosen, actor=request.user)
        except recovery.RecoveryActionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        log_event(
            request=request, action="message.recovery_resolved", tenant=message.tenant,
            object_type="message", object_id=message.pk,
            metadata={"action": chosen, "new_status": new_status},
        )
        message.refresh_from_db()
        return Response(recovery.message_recovery_detail(message))
