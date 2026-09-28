"""
The API's shapes, and where the validation actually happens.

Two rules run through everything here:

* **Every id a client sends is confirmed against the caller's own tenant.**
  Not checked after the fetch -- filtered in the query, so another workspace's
  knowledge base is "not found" rather than "forbidden". A 403 would confirm it
  exists.
* **Actions and flows are validated by the modules that execute them.**
  ``actions.validate`` and ``flow.validate`` are called from here rather than
  reimplemented, so what the API accepts and what the engine can run are the
  same thing by construction rather than by two lists somebody keeps in step.
"""

from rest_framework import serializers

from . import actions as actions_module
from . import flow as flow_module
from . import matching
from .models import (
    Chatbot,
    ChatbotEvent,
    ChatbotHandoff,
    ChatbotMediaAsset,
    ChatbotRule,
    ChatbotSession,
    ChatbotVersion,
    ChatbotWorkingHours,
)


def _tenant(context):
    request = context.get("request")
    return getattr(getattr(request, "user", None), "tenant", None)


class ActionsField(serializers.JSONField):
    """A list of actions, validated by the module that runs them."""

    def to_internal_value(self, data):
        data = super().to_internal_value(data)
        tenant = _tenant(self.context)
        chatbot = self.context.get("chatbot")
        try:
            return actions_module.validate(data, tenant_id=getattr(tenant, "id", None), chatbot=chatbot)
        except actions_module.ActionError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class ChatbotWorkingHoursSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatbotWorkingHours
        fields = ("id", "weekday", "is_open", "opens_at", "closes_at")
        read_only_fields = ("id",)


class ChatbotRuleSerializer(serializers.ModelSerializer):
    actions = ActionsField(required=False)

    class Meta:
        model = ChatbotRule
        fields = (
            "id", "chatbot", "name", "match_type", "pattern", "case_sensitive", "priority",
            "actions", "cooldown_seconds", "is_active", "match_count", "last_matched_at",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "match_count", "last_matched_at", "created_at", "updated_at")

    def validate_chatbot(self, value):
        tenant = _tenant(self.context)
        if tenant is None or value.tenant_id != tenant.id:
            raise serializers.ValidationError("That chatbot is not in your workspace.")
        return value

    def validate(self, attrs):
        match_type = attrs.get("match_type", getattr(self.instance, "match_type", ChatbotRule.MatchType.CONTAINS))
        pattern = attrs.get("pattern", getattr(self.instance, "pattern", ""))

        if match_type != ChatbotRule.MatchType.ANY and not (pattern or "").strip():
            raise serializers.ValidationError({"pattern": "Say what this rule should match on."})

        if match_type == ChatbotRule.MatchType.REGEX:
            # Compiled here, at save time, so a broken expression is a form
            # error the author can fix rather than a rule that silently never
            # matches anything for the rest of its life.
            try:
                matching.compile_pattern(pattern, case_sensitive=attrs.get("case_sensitive", False))
            except matching.PatternError as exc:
                raise serializers.ValidationError({"pattern": str(exc)}) from exc

        if not attrs.get("actions", getattr(self.instance, "actions", None)):
            raise serializers.ValidationError({"actions": "A rule needs at least one thing to do."})
        return attrs


class ChatbotVersionSerializer(serializers.ModelSerializer):
    published_by_username = serializers.CharField(source="published_by.username", read_only=True, default=None)
    is_live = serializers.SerializerMethodField()

    class Meta:
        model = ChatbotVersion
        fields = (
            "id", "version", "flow", "rules", "settings", "working_hours",
            "notes", "published_at", "published_by", "published_by_username",
            "is_live", "created_at",
        )
        read_only_fields = fields

    def get_is_live(self, obj):
        return obj.chatbot.published_version_id == obj.pk


class FlowSerializer(serializers.Serializer):
    """The draft graph, validated by the module that walks it."""

    flow = serializers.JSONField()

    def validate_flow(self, value):
        tenant = _tenant(self.context)
        try:
            return flow_module.validate(
                value, tenant_id=getattr(tenant, "id", None), chatbot=self.context.get("chatbot"),
            )
        # ``flow.validate`` delegates action validation to ``actions.validate``.
        # Both validators use the same user-correctable ``ValueError`` family
        # (``FlowError`` and ``ActionError`` respectively).  Catch the common
        # base here so an incomplete node (for example a question whose body
        # is still being typed) is a normal 400 validation response instead of
        # escaping DRF as a 500 with the generic error toast.
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class ChatbotSerializer(serializers.ModelSerializer):
    account_name = serializers.CharField(source="whatsapp_account.name", read_only=True)
    account_status = serializers.CharField(source="whatsapp_account.status", read_only=True)
    account_phone = serializers.CharField(source="whatsapp_account.phone_number", read_only=True, default=None)
    account_provider = serializers.CharField(source="whatsapp_account.provider", read_only=True)
    knowledge_base_name = serializers.CharField(source="knowledge_base.name", read_only=True, default=None)
    rule_count = serializers.SerializerMethodField()
    published_version_number = serializers.IntegerField(source="published_version.version", read_only=True, default=None)
    working_hours = ChatbotWorkingHoursSerializer(many=True, read_only=True)
    #: What this bot can actually deliver on the number it is attached to.
    #: Read at list time so the composer can say "this number cannot send
    #: buttons" before somebody builds a flow made of them.
    capabilities = serializers.SerializerMethodField()
    has_legacy_automation = serializers.SerializerMethodField()

    class Meta:
        model = Chatbot
        fields = (
            "id", "name", "description", "bot_type", "status",
            "whatsapp_account", "account_name", "account_status", "account_phone", "account_provider",
            "welcome_message", "fallback_message", "unsupported_message", "outside_hours_message",
            "ai_enabled", "knowledge_base", "knowledge_base_name", "system_prompt", "persona", "language",
            "max_ai_response_chars", "history_messages", "min_relevance",
            "response_delay_min_seconds", "response_delay_max_seconds",
            "human_handoff_enabled", "handoff_message", "handoff_conditions",
            "working_hours_enabled", "working_hours", "conversation_timeout_minutes",
            "defer_to_automation",
            "published_version", "published_version_number", "rule_count", "capabilities",
            "has_legacy_automation", "created_at", "updated_at",
        )
        read_only_fields = ("id", "status", "published_version", "created_at", "updated_at")

    def get_rule_count(self, obj):
        return getattr(obj, "rule_total", None) if hasattr(obj, "rule_total") else obj.rules.count()

    def get_capabilities(self, obj):
        from apps.whatsapp.connections.manager import ConnectionManager

        try:
            return ConnectionManager.capabilities(obj.whatsapp_account)
        except Exception:  # noqa: BLE001 - a capability lookup must not break the list
            return {}

    def get_has_legacy_automation(self, obj):
        """
        Whether the old automation rules are also configured on this number.

        Surfaced rather than resolved silently: a workspace that publishes a
        chatbot onto a number that already has keyword rules has two things
        that both want to answer, and the platform picks the bot. Telling them
        is far better than letting them discover it when a rule they wrote
        last year stops firing.
        """
        from apps.automation.models import AutomationRule, Workflow

        return (
            AutomationRule.objects.filter(whatsapp_account_id=obj.whatsapp_account_id, is_active=True).exists()
            or Workflow.objects.filter(whatsapp_account_id=obj.whatsapp_account_id, is_active=True).exists()
        )

    def validate_whatsapp_account(self, value):
        tenant = _tenant(self.context)
        if tenant is None or value.tenant_id != tenant.id:
            raise serializers.ValidationError("That WhatsApp number is not in your workspace.")
        return value

    def validate_knowledge_base(self, value):
        if value is None:
            return value
        tenant = _tenant(self.context)
        if tenant is None or value.tenant_id != tenant.id:
            raise serializers.ValidationError("That knowledge base is not in your workspace.")
        return value

    def validate_handoff_conditions(self, value):
        from .services import DEFAULT_HANDOFF_CONDITIONS

        if not isinstance(value, dict):
            raise serializers.ValidationError("Handoff conditions must be an object.")
        unknown = set(value) - set(DEFAULT_HANDOFF_CONDITIONS)
        if unknown:
            raise serializers.ValidationError(
                f"Not a handoff condition: {', '.join(sorted(unknown))}."
            )
        return value

    def validate(self, attrs):
        low = attrs.get("response_delay_min_seconds", getattr(self.instance, "response_delay_min_seconds", 0))
        high = attrs.get("response_delay_max_seconds", getattr(self.instance, "response_delay_max_seconds", 0))
        if low and high and low > high:
            raise serializers.ValidationError(
                {"response_delay_min_seconds": "The shortest delay cannot be longer than the longest."}
            )

        relevance = attrs.get("min_relevance")
        if relevance is not None and not 0 <= relevance <= 1:
            raise serializers.ValidationError({"min_relevance": "Must be between 0 and 1."})
        return attrs


class ChatbotSessionSerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source="conversation.contact.name", read_only=True, default=None)
    contact_phone = serializers.CharField(source="conversation.contact.phone", read_only=True, default=None)
    chatbot_name = serializers.CharField(source="chatbot.name", read_only=True, default=None)
    unread_count = serializers.IntegerField(source="conversation.unread_count", read_only=True, default=0)
    conversation_status = serializers.CharField(source="conversation.status", read_only=True, default=None)
    assigned_agent = serializers.IntegerField(source="conversation.assigned_agent_id", read_only=True, default=None)
    assigned_agent_username = serializers.CharField(
        source="conversation.assigned_agent.username", read_only=True, default=None,
    )
    last_message_preview = serializers.SerializerMethodField()

    class Meta:
        model = ChatbotSession
        fields = (
            "id", "conversation", "chatbot", "chatbot_name", "state", "resolution",
            "contact_name", "contact_phone", "unread_count", "conversation_status",
            "assigned_agent", "assigned_agent_username",
            "current_node_id", "awaiting_node_id", "variables", "summary",
            "detected_language", "consecutive_fallbacks", "ai_replies", "bot_replies",
            "messages_received", "handed_off_at", "handoff_reason", "returned_to_bot_at",
            "last_message_preview", "started_at", "last_customer_message_at",
            "last_bot_message_at", "updated_at", "ended_at",
        )
        read_only_fields = fields

    def get_last_message_preview(self, obj):
        return getattr(obj, "last_message_preview", None)


class ChatbotEventSerializer(serializers.ModelSerializer):
    step_label = serializers.CharField(source="get_step_display", read_only=True)

    class Meta:
        model = ChatbotEvent
        fields = (
            "id", "correlation_id", "step", "step_label", "outcome", "detail",
            "metadata", "duration_ms", "conversation", "chatbot", "message", "created_at",
        )
        read_only_fields = fields


class ChatbotHandoffSerializer(serializers.ModelSerializer):
    trigger_label = serializers.CharField(source="get_trigger_display", read_only=True)
    assigned_to_username = serializers.CharField(source="assigned_to.username", read_only=True, default=None)

    class Meta:
        model = ChatbotHandoff
        fields = (
            "id", "conversation", "chatbot", "trigger", "trigger_label", "detail",
            "assigned_to", "assigned_to_username", "returned_at", "created_at",
        )
        read_only_fields = fields


class ChatbotMediaAssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatbotMediaAsset
        fields = (
            "id", "chatbot", "key", "label", "media_type", "media_url",
            "filename", "caption", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_chatbot(self, value):
        if value is None:
            return value
        tenant = _tenant(self.context)
        if tenant is None or value.tenant_id != tenant.id:
            raise serializers.ValidationError("That chatbot is not in your workspace.")
        return value

    def validate_media_type(self, value):
        from core.media import MEDIA_TYPES

        upper = (value or "").upper()
        if upper not in MEDIA_TYPES:
            raise serializers.ValidationError(f"Choose one of: {', '.join(MEDIA_TYPES)}.")
        return upper


# ---------------------------------------------------------------------------
# Action-shaped request bodies
# ---------------------------------------------------------------------------

class PublishSerializer(serializers.Serializer):
    notes = serializers.CharField(required=False, allow_blank=True, max_length=300)


class RollbackSerializer(serializers.Serializer):
    version_id = serializers.IntegerField()


class RuleTestSerializer(serializers.Serializer):
    """
    Try a message against this bot's rules without sending anything.

    Runs the *same* matcher the engine runs, deliberately -- a tester that used
    an approximation would build confidence in behaviour that is not the
    behaviour.
    """

    message = serializers.CharField(max_length=4096)
    button_id = serializers.CharField(required=False, allow_blank=True, max_length=256)
    use_published = serializers.BooleanField(default=True)


class ReplySerializer(serializers.Serializer):
    """An agent typing into a chatbot conversation. Mirrors the inbox's own reply shape."""

    from core.button_fields import ButtonsField
    from core.media_fields import MediaItemsField

    body = serializers.CharField(required=False, allow_blank=True)
    media_items = MediaItemsField(required=False)
    buttons = ButtonsField(required=False)

    def validate(self, attrs):
        from core.button_fields import validate_button_body
        from core.media_fields import apply_media_items

        attrs = apply_media_items(attrs)
        validate_button_body(attrs.get("buttons"), attrs.get("body"))
        if not attrs.get("body") and not attrs.get("media_items"):
            raise serializers.ValidationError("Write something or attach a file.")
        return attrs


class SimulationTurnSerializer(serializers.Serializer):
    """One thing the tester said, in the preview panel."""

    text = serializers.CharField(required=False, allow_blank=True, max_length=4096)
    #: The id of an option they tapped. Sent separately from the text because
    #: that is exactly how a real inbound message carries it -- a tap arrives as
    #: `Message.replied_button_id` with the label as the body -- and a preview
    #: that flattened the two would never exercise per-option branching.
    button_id = serializers.CharField(required=False, allow_blank=True, max_length=256)

    def validate(self, attrs):
        if not (attrs.get("text") or "").strip() and not (attrs.get("button_id") or "").strip():
            raise serializers.ValidationError("A turn needs either text or a chosen option.")
        return attrs


class SimulationSerializer(serializers.Serializer):
    """
    The whole transcript so far.

    The whole thing, every time, because the preview stores nothing between
    requests -- see ``services.simulate``. Capped at the same number of turns
    the service will replay, so an over-long transcript is a 400 naming the
    limit rather than a silent truncation the tester cannot see.
    """

    messages = SimulationTurnSerializer(many=True, allow_empty=False, max_length=25)


class ApplyTemplateSerializer(serializers.Serializer):
    template = serializers.CharField(max_length=64)
    #: Guards the one destructive thing this endpoint can do. Defaults to false
    #: so a mis-click cannot discard a draft nobody has a copy of.
    replace = serializers.BooleanField(required=False, default=False)
