from django.contrib import admin

from .models import CallSession, CallTurn, VoiceAgentConfig


@admin.register(VoiceAgentConfig)
class VoiceAgentConfigAdmin(admin.ModelAdmin):
    list_display = ["tenant", "is_enabled", "provider", "from_number", "model", "updated_at"]
    list_filter = ["is_enabled", "provider"]
    search_fields = ["tenant__name", "from_number"]
    # The encrypted columns are deliberately absent: the admin is the one place
    # somebody could otherwise read a tenant's carrier credentials back out.
    exclude = ["account_sid_encrypted", "auth_token_encrypted", "api_key_encrypted"]


class CallTurnInline(admin.TabularInline):
    model = CallTurn
    extra = 0
    can_delete = False
    readonly_fields = ["ordinal", "role", "text", "confidence", "latency_ms", "created_at"]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(CallSession)
class CallSessionAdmin(admin.ModelAdmin):
    list_display = ["id", "tenant", "to_number", "status", "outcome", "turns_used", "duration_seconds", "created_at"]
    list_filter = ["status", "outcome", "direction", "provider"]
    search_fields = ["to_number", "provider_call_id", "summary"]
    readonly_fields = ["webhook_token", "provider_call_id", "created_at", "started_at", "answered_at", "ended_at"]
    inlines = [CallTurnInline]
