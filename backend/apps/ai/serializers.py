from rest_framework import serializers

from .models import AIAgentConfig, AIUsageLog, KnowledgeAnswer, KnowledgeBase, KnowledgeChunk, KnowledgeDocument
from .providers.registry import base_url_problem, model_name_problem


class AIAgentConfigSerializer(serializers.ModelSerializer):
    # Write-only, and never echoed back: a key that can be read out of the API
    # is a key that ends up in a browser history or a screenshot.
    api_key = serializers.CharField(write_only=True, required=False, allow_blank=True)
    has_api_key = serializers.SerializerMethodField()
    is_ready = serializers.SerializerMethodField()
    # What the agent can currently do, resolved from the three switches. Read
    # back rather than inferred in the browser, so the page and the send path
    # cannot disagree about what is switched on.
    available_tools = serializers.SerializerMethodField()
    # Whether the deployment has taken the knowledge-only decision out of this
    # tenant's hands. Read back so the settings page can show the switch as
    # locked on instead of offering a control that silently does nothing.
    knowledge_only_enforced = serializers.SerializerMethodField()

    class Meta:
        model = AIAgentConfig
        fields = (
            "is_enabled", "display_name", "provider", "model", "base_url", "business_context",
            "temperature", "max_output_tokens", "history_messages", "knowledge_chunks",
            "answer_from_knowledge_only", "knowledge_only_message", "knowledge_only_enforced",
            "reply_when_assigned", "max_replies_per_conversation",
            "handoff_enabled", "handoff_message",
            "knowledge_base", "reply_scope", "fallback_action", "min_relevance",
            "crm_logging_enabled", "crm_tagging_enabled",
            "tools_enabled", "allow_write_tools", "allow_send_tools", "max_tool_rounds", "available_tools",
            "api_key", "has_api_key", "is_ready", "updated_at",
        )
        read_only_fields = (
            "has_api_key", "is_ready", "available_tools", "knowledge_only_enforced", "updated_at",
        )

    def get_knowledge_only_enforced(self, obj):
        from django.conf import settings

        return bool(getattr(settings, "AI_KNOWLEDGE_ONLY", False))

    def get_available_tools(self, obj):
        from . import tools

        return tools.available(obj)

    def validate_max_tool_rounds(self, value):
        from .agent import MAX_TOOL_ROUNDS_CEILING

        if value and value > MAX_TOOL_ROUNDS_CEILING:
            raise serializers.ValidationError(
                f"At most {MAX_TOOL_ROUNDS_CEILING}. Each round is a full request billed to your account."
            )
        return value

    def validate_knowledge_base(self, value):
        if value is not None and value.tenant_id != self.context["request"].user.tenant_id:
            raise serializers.ValidationError("Choose one of your own knowledge bases.")
        return value

    def validate_min_relevance(self, value):
        if not 0 <= value <= 1:
            raise serializers.ValidationError("This must be between 0 and 1.")
        return value

    def get_has_api_key(self, obj):
        return bool(obj.api_key_encrypted)

    def get_is_ready(self, obj):
        from .services import is_configured

        return is_configured(obj)

    def validate_temperature(self, value):
        if not 0 <= value <= 1:
            raise serializers.ValidationError("Temperature must be between 0 and 1.")
        return value

    def validate(self, attrs):
        merged = {**{field: getattr(self.instance, field, None) for field in self.fields}, **attrs}

        # A model name from the wrong vendor's vocabulary is only discovered
        # when a customer is already waiting for the reply, so it is caught on
        # the way in instead.
        problem = model_name_problem(merged.get("provider"), merged.get("model"))
        if problem:
            raise serializers.ValidationError({"model": problem})

        problem = base_url_problem(merged.get("provider"), merged.get("base_url"))
        if problem:
            raise serializers.ValidationError({"base_url": problem})

        if merged.get("handoff_enabled") and not (merged.get("handoff_message") or "").strip():
            raise serializers.ValidationError(
                {"handoff_message": "Write what the customer should be told when the agent hands over."}
            )
        return attrs

    def update(self, instance, validated_data):
        # A blank key means "leave the stored one alone", not "delete it" --
        # otherwise every save from a form that cannot show the key would
        # silently disable the agent. Clearing is a separate action.
        raw_key = validated_data.pop("api_key", None)
        if raw_key:
            instance.set_api_key(raw_key)
        instance = super().update(instance, validated_data)
        if raw_key:
            instance.save(update_fields=["api_key_encrypted"])
        return instance


class KnowledgeBaseSerializer(serializers.ModelSerializer):
    document_count = serializers.IntegerField(read_only=True, required=False)
    chunk_total = serializers.IntegerField(read_only=True, required=False)
    processing_count = serializers.IntegerField(read_only=True, required=False)
    failed_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = KnowledgeBase
        fields = (
            "id", "name", "description", "is_active", "is_default",
            "document_count", "chunk_total", "processing_count", "failed_count",
            "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "document_count", "chunk_total", "processing_count", "failed_count",
            "created_at", "updated_at",
        )


class KnowledgeDocumentSerializer(serializers.ModelSerializer):
    knowledge_base_name = serializers.CharField(source="knowledge_base.name", read_only=True, default=None)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = KnowledgeDocument
        fields = (
            "id", "knowledge_base", "knowledge_base_name", "title", "content", "source_url", "is_active",
            "source_type", "original_filename", "content_type", "size_bytes", "page_count",
            "status", "status_display", "error", "chunk_count", "indexed_at",
            "created_by", "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "knowledge_base_name", "source_type", "original_filename", "content_type", "size_bytes",
            "page_count", "status", "status_display", "error", "chunk_count", "indexed_at",
            "created_by", "created_at", "updated_at",
        )

    def validate_knowledge_base(self, value):
        if value is not None and value.tenant_id != self.context["request"].user.tenant_id:
            raise serializers.ValidationError("Choose one of your own knowledge bases.")
        return value

    def validate(self, attrs):
        # A typed document with no text is a document that answers nothing;
        # an uploaded one legitimately has no text until the worker has read it.
        instance_is_file = getattr(self.instance, "source_type", None) == KnowledgeDocument.Source.FILE
        if not instance_is_file and not (attrs.get("content") or getattr(self.instance, "content", "")).strip():
            raise serializers.ValidationError({"content": "Write what the assistant should know."})
        return attrs


class KnowledgeUploadSerializer(serializers.Serializer):
    """The upload form: a file, and where to put it."""

    file = serializers.FileField()
    knowledge_base = serializers.PrimaryKeyRelatedField(
        queryset=KnowledgeBase.objects.all(), required=False, allow_null=True,
    )
    title = serializers.CharField(required=False, allow_blank=True, max_length=200)
    # An updated version of a document already here, rather than a new one.
    # Without this, re-uploading a corrected price list leaves the old one
    # active alongside it and the agent answers from whichever the retrieval
    # happens to prefer -- which is the stale-answer failure the whole
    # delete-then-rechunk design exists to avoid.
    replaces = serializers.PrimaryKeyRelatedField(
        queryset=KnowledgeDocument.objects.all(), required=False, allow_null=True,
    )

    def validate_knowledge_base(self, value):
        if value is not None and value.tenant_id != self.context["request"].user.tenant_id:
            raise serializers.ValidationError("Choose one of your own knowledge bases.")
        return value

    def validate_replaces(self, value):
        if value is not None and value.tenant_id != self.context["request"].user.tenant_id:
            raise serializers.ValidationError("Choose one of your own documents.")
        return value

    def validate_file(self, value):
        from .extraction import ExtractionError, validate_upload

        try:
            validate_upload(value.name, value.size, getattr(value, "content_type", ""))
        except ExtractionError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value


class KnowledgeAnswerSerializer(serializers.ModelSerializer):
    confidence_display = serializers.CharField(source="get_confidence_display", read_only=True)
    contact_name = serializers.SerializerMethodField()

    class Meta:
        model = KnowledgeAnswer
        fields = (
            "id", "knowledge_base", "conversation", "contact", "contact_name", "question", "answer",
            "confidence", "confidence_display", "relevance", "sources", "answered", "fallback_used",
            "is_test", "created_at",
        )
        read_only_fields = fields

    def get_contact_name(self, obj):
        if not obj.contact_id:
            return None
        return obj.contact.name or obj.contact.phone


class KnowledgeAskSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=2000)
    knowledge_base = serializers.PrimaryKeyRelatedField(
        queryset=KnowledgeBase.objects.all(), required=False, allow_null=True,
    )

    def validate_knowledge_base(self, value):
        if value is not None and value.tenant_id != self.context["request"].user.tenant_id:
            raise serializers.ValidationError("Choose one of your own knowledge bases.")
        return value


class AIUsageLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIUsageLog
        fields = (
            "id", "conversation", "model", "outcome", "input_tokens", "output_tokens",
            "latency_ms", "error", "created_at",
        )
        read_only_fields = fields


class ConversationActionSerializer(serializers.Serializer):
    conversation = serializers.IntegerField()


class KnowledgeSearchSerializer(serializers.Serializer):
    query = serializers.CharField()
    limit = serializers.IntegerField(required=False, min_value=1, max_value=20, default=5)
