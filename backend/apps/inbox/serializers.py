from rest_framework import serializers

from apps.messaging.models import Message
from core.button_fields import ButtonsField, validate_button_body
from core.media_fields import MediaItemsField, apply_media_items

from .models import Conversation, ConversationLabel, ConversationNote


class ConversationLabelSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConversationLabel
        fields = ("id", "name", "color")
        read_only_fields = ("id",)


class ConversationSerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source="contact.name", read_only=True)
    contact_phone = serializers.CharField(source="contact.phone", read_only=True)
    assigned_agent_username = serializers.CharField(source="assigned_agent.username", read_only=True, default=None)
    labels = serializers.PrimaryKeyRelatedField(many=True, queryset=ConversationLabel.objects.all(), required=False)
    # Annotated by ConversationViewSet.get_queryset(); absent on instances built
    # elsewhere (e.g. tests), hence the getattr fallback rather than a source=.
    last_message_preview = serializers.SerializerMethodField()
    last_message_direction = serializers.SerializerMethodField()
    response_due_at = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = (
            "id", "whatsapp_account", "contact", "contact_name", "contact_phone", "status",
            "assigned_agent", "assigned_agent_username", "labels", "unread_count", "last_message_at",
            "last_message_preview", "last_message_direction",
            "awaiting_reply_since", "first_response_seconds", "response_due_at", "is_overdue",
            "created_at", "updated_at", "closed_at",
        )
        read_only_fields = (
            "id", "whatsapp_account", "contact", "status", "assigned_agent", "unread_count",
            "last_message_at", "awaiting_reply_since", "first_response_seconds",
            "created_at", "updated_at", "closed_at",
        )

    def get_last_message_preview(self, obj):
        return getattr(obj, "last_message_preview", None)

    def get_last_message_direction(self, obj):
        return getattr(obj, "last_message_direction", None)

    def _assignment_policy(self, obj):
        """
        The tenant's SLA target, looked up once per response.

        Cached on the serializer context because the two fields below are
        rendered for every row: without this, listing 50 conversations would
        fetch the same policy 100 times.
        """
        cache = self.context.setdefault("_assignment_policies", {})
        if obj.tenant_id not in cache:
            from apps.teams.models import AssignmentPolicy

            cache[obj.tenant_id] = AssignmentPolicy.for_tenant(obj.tenant_id)
        return cache[obj.tenant_id]

    def get_response_due_at(self, obj):
        from apps.teams.services import response_due_at

        due = response_due_at(obj, self._assignment_policy(obj))
        return due.isoformat() if due else None

    def get_is_overdue(self, obj):
        """Computed rather than stored, so it is never a stale flag left behind by a target that has since changed."""
        from django.utils import timezone

        from apps.teams.services import response_due_at

        due = response_due_at(obj, self._assignment_policy(obj))
        return bool(due and due < timezone.now())

    def validate_labels(self, value):
        tenant = self.context["request"].user.tenant
        for label in value:
            if label.tenant_id != tenant.id:
                raise serializers.ValidationError("Labels must belong to your tenant.")
        return value


class ConversationNoteSerializer(serializers.ModelSerializer):
    author_username = serializers.CharField(source="author.username", read_only=True, default=None)

    class Meta:
        model = ConversationNote
        fields = ("id", "body", "author", "author_username", "created_at")
        read_only_fields = ("id", "author", "author_username", "created_at")


class ReplySerializer(serializers.Serializer):
    message_type = serializers.ChoiceField(choices=Message.MessageType.choices, default=Message.MessageType.TEXT)
    body = serializers.CharField(required=False, allow_blank=True)
    media_url = serializers.URLField(required=False, allow_blank=True)
    media_items = MediaItemsField(required=False)
    caption = serializers.CharField(required=False, allow_blank=True)
    filename = serializers.CharField(required=False, allow_blank=True)
    buttons = ButtonsField(required=False)

    def validate(self, attrs):
        # Mirrors the model serializers: attaching several files sets
        # message_type/media_url from the first one, so send_reply() and the
        # thread view keep behaving exactly as they do for a single attachment.
        attrs = apply_media_items(attrs)
        validate_button_body(attrs.get("buttons"), attrs.get("body"))
        return attrs
