"""
Django admin for the chatbot tables.

Read-mostly on purpose. Configuration belongs in the product's own screens,
where the actions and the flow are validated by the modules that run them; the
admin exists so an operator can *see* what a workspace has and answer "why did
this conversation do that" without a database client.
"""

from django.contrib import admin

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


class ChatbotRuleInline(admin.TabularInline):
    model = ChatbotRule
    extra = 0
    fields = ("name", "match_type", "pattern", "priority", "is_active", "match_count")
    readonly_fields = ("match_count",)


class ChatbotWorkingHoursInline(admin.TabularInline):
    model = ChatbotWorkingHours
    extra = 0


@admin.register(Chatbot)
class ChatbotAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "whatsapp_account", "bot_type", "status", "ai_enabled", "updated_at")
    list_filter = ("status", "bot_type", "ai_enabled", "tenant")
    search_fields = ("name", "description")
    raw_id_fields = ("tenant", "whatsapp_account", "created_by", "knowledge_base", "published_version")
    inlines = (ChatbotRuleInline, ChatbotWorkingHoursInline)


@admin.register(ChatbotVersion)
class ChatbotVersionAdmin(admin.ModelAdmin):
    list_display = ("chatbot", "version", "published_at", "published_by")
    list_filter = ("published_at",)
    raw_id_fields = ("chatbot", "published_by")
    readonly_fields = ("flow", "rules", "settings", "working_hours", "created_at")


@admin.register(ChatbotSession)
class ChatbotSessionAdmin(admin.ModelAdmin):
    list_display = ("conversation", "chatbot", "state", "resolution", "bot_replies", "ai_replies", "updated_at")
    list_filter = ("state", "resolution", "tenant")
    raw_id_fields = ("tenant", "conversation", "chatbot", "version")
    readonly_fields = ("started_at", "updated_at", "ended_at", "last_processed_message_id")


@admin.register(ChatbotEvent)
class ChatbotEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "step", "outcome", "chatbot", "conversation", "duration_ms", "detail")
    list_filter = ("step", "outcome")
    search_fields = ("correlation_id", "detail")
    raw_id_fields = ("tenant", "conversation", "chatbot", "message")
    # Append-only by construction: nothing writes to a row after it is created,
    # so making it editable here would only be a way to falsify the trail.
    readonly_fields = [field.name for field in ChatbotEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ChatbotHandoff)
class ChatbotHandoffAdmin(admin.ModelAdmin):
    list_display = ("created_at", "conversation", "chatbot", "trigger", "assigned_to", "returned_at")
    list_filter = ("trigger",)
    raw_id_fields = ("tenant", "conversation", "chatbot", "session", "assigned_to")


@admin.register(ChatbotMediaAsset)
class ChatbotMediaAssetAdmin(admin.ModelAdmin):
    list_display = ("key", "label", "media_type", "chatbot", "tenant")
    list_filter = ("media_type",)
    search_fields = ("key", "label")
    raw_id_fields = ("tenant", "chatbot")


@admin.register(ChatbotRule)
class ChatbotRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "chatbot", "match_type", "pattern", "priority", "is_active", "match_count")
    list_filter = ("match_type", "is_active")
    search_fields = ("name", "pattern")
    raw_id_fields = ("chatbot",)
