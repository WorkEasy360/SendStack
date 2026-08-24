from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.contacts.models import Contact
from core.bulk import BulkActionMixin
from core.media import resolve_media_items, spec_from_object

from .models import MessageTemplate
from .serializers import MessageTemplateSerializer, TemplatePreviewSerializer
from .services import render_template, render_text


class MessageTemplateViewSet(BulkActionMixin, viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = MessageTemplateSerializer
    filterset_fields = ["message_type", "is_active"]
    search_fields = ["name", "body"]

    bulk_object_label = "template"
    bulk_actions = {"delete": "_bulk_delete", "activate": "_bulk_activate", "deactivate": "_bulk_deactivate"}

    def get_queryset(self):
        return MessageTemplate.objects.filter(tenant=self.request.user.tenant)

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.user.tenant, created_by=self.request.user)

    def _bulk_delete(self, request, instance):
        self.perform_destroy(instance)

    def _set_active(self, instance, is_active):
        if instance.is_active != is_active:
            instance.is_active = is_active
            instance.save(update_fields=["is_active", "updated_at"])

    def _bulk_activate(self, request, instance):
        self._set_active(instance, True)

    def _bulk_deactivate(self, request, instance):
        self._set_active(instance, False)

    @action(detail=True, methods=["post"])
    def preview(self, request, pk=None):
        template = self.get_object()
        payload = TemplatePreviewSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        if payload.validated_data.get("contact_id"):
            try:
                contact = Contact.objects.get(pk=payload.validated_data["contact_id"], tenant=request.user.tenant)
            except Contact.DoesNotExist:
                return Response({"detail": "Contact not found."}, status=status.HTTP_404_NOT_FOUND)
            rendered = render_template(template, contact)
        else:
            sample = {k.lower(): v for k, v in payload.validated_data.get("sample_data", {}).items()}
            rendered = {
                "body": render_text(template.body, sample, template.variables),
                "caption": render_text(template.caption, sample, template.variables),
                "media_url": template.media_url,
                "media_items": resolve_media_items(spec_from_object(template)),
                "filename": template.filename,
                "message_type": template.message_type,
            }

        return Response(rendered)
