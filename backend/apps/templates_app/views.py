from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.contacts.models import Contact
from core.bulk import BulkActionMixin
from core.interactive import clean_list_content, clean_poll_content
from core.interactive import content_kind as resolve_content_kind
from core.media import resolve_media_items, spec_from_object

from .delivery import delivery_preview
from .models import MessageTemplate
from .serializers import DraftPreviewSerializer, MessageTemplateSerializer, TemplatePreviewSerializer
from .services import (
    available_variables,
    build_render_context,
    render_buttons,
    render_interactive,
    render_template,
    render_text,
)


def _normalised(cleaner, value):
    """
    Run a draft through its validator, or hand it back untouched.

    The live composer sends content mid-edit -- a list with an empty section,
    a poll with one option -- which is invalid and still has to be drawn.
    Cleaning where possible means a finished draft previews with exactly the
    ids and defaults it will be saved with, and an unfinished one previews as
    typed rather than not at all.
    """
    try:
        return cleaner(value)
    except DjangoValidationError:
        return value or {}


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

    def _render_context(self, validated):
        """
        The lookup a preview renders against: a real contact's data, or the
        sample the composer supplied.

        Returns ``(context, error_response)``. A named contact that does not
        belong to this workspace is a 404 and not an empty render -- the
        difference between "this contact has no company" and "this contact is
        not yours" matters, and only one of them is a mistake worth showing.
        """
        if validated.get("contact_id"):
            contact = Contact.objects.filter(
                pk=validated["contact_id"], tenant=self.request.user.tenant,
            ).first()
            if contact is None:
                return None, Response({"detail": "Contact not found."}, status=status.HTTP_404_NOT_FOUND)
            return build_render_context(contact), None

        return {key.lower(): value for key, value in (validated.get("sample_data") or {}).items()}, None

    @action(detail=True, methods=["post"])
    def preview(self, request, pk=None):
        template = self.get_object()
        payload = TemplatePreviewSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        if payload.validated_data.get("contact_id"):
            contact = Contact.objects.filter(
                pk=payload.validated_data["contact_id"], tenant=request.user.tenant,
            ).first()
            if contact is None:
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
                "buttons": render_buttons(template.buttons, sample, template.variables),
                "list_content": render_interactive(
                    template.list_content, sample, template.variables, kind="list",
                ),
                "poll_content": render_interactive(
                    template.poll_content, sample, template.variables, kind="poll",
                ),
            }

        rendered["content_kind"] = template.content_kind
        return Response(rendered)

    @action(detail=False, methods=["post"], url_path="draft-preview")
    def draft_preview(self, request):
        """
        Render unsaved content, and say how it will actually be delivered.

        The browser renders the *visual* preview itself, on every keystroke --
        a round trip per character would be neither instant nor kind to the
        server. What it cannot do faithfully is the second half of this
        response: the exact text a connection that has no native support will
        put on the wire. That is produced by the same functions the send path
        calls, so what the composer shows and what the recipient receives
        cannot drift apart.
        """
        payload = DraftPreviewSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        validated = payload.validated_data

        context, error = self._render_context(validated)
        if error is not None:
            return error

        fallbacks = validated.get("variables") or {}
        rendered = {
            "body": render_text(validated.get("body", ""), context, fallbacks),
            "caption": render_text(validated.get("caption", ""), context, fallbacks),
            "buttons": render_buttons(validated.get("buttons") or [], context, fallbacks),
            # Normalised first *where it can be*, so a finished draft previews
            # with the row ids it will actually be saved with -- and a draft
            # still being typed falls back to the raw content rather than
            # refusing to render. See DraftPreviewSerializer for why this
            # endpoint renders rather than validates.
            "list_content": render_interactive(
                _normalised(clean_list_content, validated.get("list_content")),
                context, fallbacks, kind="list",
            ),
            "poll_content": render_interactive(
                _normalised(clean_poll_content, validated.get("poll_content")),
                context, fallbacks, kind="poll",
            ),
        }
        rendered["content_kind"] = resolve_content_kind(rendered)
        rendered["delivery"] = delivery_preview(
            rendered,
            tenant=request.user.tenant,
            account_id=validated.get("whatsapp_account_id"),
        )
        return Response(rendered)

    @action(detail=False, methods=["get"])
    def variables(self, request):
        """
        Which variables this workspace's authors can use.

        Answered from their own contacts rather than from a fixed list -- see
        `services.available_variables`.
        """
        return Response(available_variables(request.user.tenant_id))
