from django.contrib import admin

from .models import (
    AIAgentConfig, AIConversationState, AIToolCall, AIUsageLog, KnowledgeChunk, KnowledgeDocument,
)


@admin.register(AIAgentConfig)
class AIAgentConfigAdmin(admin.ModelAdmin):
    list_display = ["tenant", "is_enabled", "model", "answer_from_knowledge_only", "handoff_enabled"]
    list_filter = ["is_enabled", "provider"]
    exclude = ["api_key_encrypted"]


@admin.register(KnowledgeDocument)
class KnowledgeDocumentAdmin(admin.ModelAdmin):
    list_display = ["title", "tenant", "is_active", "updated_at"]
    list_filter = ["tenant", "is_active"]
    search_fields = ["title", "content"]


@admin.register(KnowledgeChunk)
class KnowledgeChunkAdmin(admin.ModelAdmin):
    list_display = ["document", "ordinal"]
    list_filter = ["tenant"]


@admin.register(AIConversationState)
class AIConversationStateAdmin(admin.ModelAdmin):
    list_display = ["conversation", "replies_sent", "handed_off_at", "handoff_reason"]


@admin.register(AIUsageLog)
class AIUsageLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "tenant", "outcome", "model", "input_tokens", "output_tokens", "latency_ms"]
    list_filter = ["outcome", "model", "created_at"]


@admin.register(AIToolCall)
class AIToolCallAdmin(admin.ModelAdmin):
    """
    What the agent did, per call.

    Read-only: this is an audit trail, and an audit trail somebody can edit is
    not one. Filterable by error first, because "what has been refused or gone
    wrong" is the question this table is opened to answer.
    """

    list_display = ["created_at", "tenant", "name", "is_error", "conversation", "whatsapp_account", "duration_ms"]
    list_filter = ["is_error", "name", "created_at"]
    search_fields = ["detail", "result", "device_id"]
    readonly_fields = [field.name for field in AIToolCall._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
