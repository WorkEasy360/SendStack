from rest_framework import serializers

from core.button_fields import ButtonsField, validate_button_body
from core.interactive_fields import ListContentField, PollContentField, validate_interactive_content
from core.media_fields import MediaItemsField, apply_media_items

from .models import MessageTemplate


class MessageTemplateSerializer(serializers.ModelSerializer):
    detected_variables = serializers.SerializerMethodField()
    #: TEXT / BUTTON / LIST / POLL, derived from which content the template
    #: actually carries. Exposed so the composer and the campaign wizard can
    #: label and capability-check a template without re-deriving the rule.
    content_kind = serializers.CharField(read_only=True)
    media_items = MediaItemsField(required=False)
    buttons = ButtonsField(required=False)
    list_content = ListContentField(required=False)
    poll_content = PollContentField(required=False)

    class Meta:
        model = MessageTemplate
        fields = (
            "id", "name", "message_type", "content_kind", "body", "media_url", "media_items", "caption",
            "filename", "buttons", "list_content", "poll_content", "variables", "detected_variables",
            "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("id", "content_kind", "created_at", "updated_at")

    def get_detected_variables(self, obj):
        return obj.detected_variables()

    def validate(self, attrs):
        attrs = apply_media_items(attrs)
        message_type = attrs.get("message_type", getattr(self.instance, "message_type", MessageTemplate.MessageType.TEXT))
        media_url = attrs.get("media_url", getattr(self.instance, "media_url", ""))
        filename = attrs.get("filename", getattr(self.instance, "filename", ""))

        if message_type != MessageTemplate.MessageType.TEXT and not media_url:
            raise serializers.ValidationError({"media_url": "Required for non-text message types."})
        if message_type == MessageTemplate.MessageType.DOCUMENT and not filename:
            raise serializers.ValidationError({"filename": "Required for document templates."})

        # `body` became optional on the model when polls arrived -- a poll's
        # question is the message. Everything else still needs *something* to
        # send, so the requirement moved here rather than disappearing: a
        # template with no body, no attachment and no poll is one that could
        # never go out.
        body = attrs.get("body", getattr(self.instance, "body", ""))
        poll_content = attrs.get("poll_content", getattr(self.instance, "poll_content", {}))
        if not (body or "").strip() and not media_url and not poll_content:
            raise serializers.ValidationError(
                {"body": "A template needs message text, an attachment, or a poll."}
            )

        # Buttons are shown under the body, so a template that has them needs
        # one -- including an image template, whose body would otherwise go
        # unsent. See core.button_fields.validate_button_body.
        validate_button_body(
            attrs.get("buttons", getattr(self.instance, "buttons", [])),
            attrs.get("body", getattr(self.instance, "body", "")),
        )
        # The same question for the other two interactive kinds, plus the rule
        # that a message can only be one of them.
        validate_interactive_content(attrs, self.instance)
        return attrs


class TemplatePreviewSerializer(serializers.Serializer):
    contact_id = serializers.IntegerField(required=False)
    sample_data = serializers.DictField(required=False)


class DraftPreviewSerializer(serializers.Serializer):
    """
    A preview of content that has not been saved yet.

    The live composer renders on every keystroke, against a template that may
    not exist -- so this takes the content itself rather than an id. It is
    deliberately a *rendering* endpoint and not a validating one: an author
    half-way through typing a list has an invalid list, and refusing to show
    them what they have so far would make the preview useless exactly when it
    is most wanted.
    """

    body = serializers.CharField(required=False, allow_blank=True, default="")
    caption = serializers.CharField(required=False, allow_blank=True, default="")
    buttons = serializers.JSONField(required=False, default=list)
    list_content = serializers.JSONField(required=False, default=dict)
    poll_content = serializers.JSONField(required=False, default=dict)
    variables = serializers.JSONField(required=False, default=dict)
    contact_id = serializers.IntegerField(required=False)
    sample_data = serializers.DictField(required=False)
    #: Which connection the preview should be shown for. When given, the
    #: response says how this content will actually arrive on that number --
    #: natively, or degraded to a numbered list.
    whatsapp_account_id = serializers.IntegerField(required=False)
