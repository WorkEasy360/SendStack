from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated

from .models import Message
from .serializers import MessageSerializer


class MessageViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Read-only: messages are created by campaign/automation services, never directly via the API."""

    permission_classes = [IsAuthenticated]
    serializer_class = MessageSerializer
    filterset_fields = ["campaign", "conversation", "status", "direction", "whatsapp_account"]

    def get_queryset(self):
        # select_related covers the account/contact names the serializer
        # exposes -- without it a 20-row page costs 40 extra queries.
        return Message.objects.filter(tenant=self.request.user.tenant).select_related("whatsapp_account", "contact")
