from rest_framework import serializers

from core.button_fields import ButtonsField, validate_button_body
from core.media_fields import MediaItemsField, apply_media_items

from .models import MessageTemplate


class MessageTemplateSerializer(serializers.ModelSerializer):
    detected_variables = serializers.SerializerMethodField()
    media_items = MediaItemsField(required=False)
    buttons = ButtonsField(required=False)

    class Meta:
        model = MessageTemplate
        fields = (
            "id", "name", "message_type", "body", "media_url", "media_items", "caption", "filename",
            "buttons", "variables", "detected_variables", "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

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

        # Buttons are shown under the body, so a template that has them needs
        # one -- including an image template, whose body would otherwise go
        # unsent. See core.button_fields.validate_button_body.
        validate_button_body(
            attrs.get("buttons", getattr(self.instance, "buttons", [])),
            attrs.get("body", getattr(self.instance, "body", "")),
        )
        return attrs


class TemplatePreviewSerializer(serializers.Serializer):
    contact_id = serializers.IntegerField(required=False)
    sample_data = serializers.DictField(required=False)
