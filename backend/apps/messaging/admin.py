from django.contrib import admin

from .models import Message, MessageSendAttempt


class MessageSendAttemptInline(admin.TabularInline):
    model = MessageSendAttempt
    extra = 0
    can_delete = False
    fields = (
        "attempt_number", "status", "provider", "worker_id", "idempotency_key",
        "submission_started_at", "submission_completed_at", "provider_message_id",
        "last_error", "last_heartbeat_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("id", "to_phone", "campaign", "status", "message_type", "lease_owner", "lease_expires_at", "queued_at")
    list_filter = ("status", "message_type", "failure_reason")
    search_fields = ("to_phone", "provider_message_id")
    readonly_fields = (
        "queued_at", "sent_at", "delivered_at", "read_at", "failed_at", "updated_at",
        "lease_owner", "lease_expires_at", "lease_heartbeat_at",
    )
    inlines = [MessageSendAttemptInline]


@admin.register(MessageSendAttempt)
class MessageSendAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "id", "message", "attempt_number", "status", "provider", "account_id",
        "worker_id", "submission_started_at", "provider_message_id", "created_at",
    )
    list_filter = ("status", "provider")
    search_fields = ("idempotency_key", "provider_message_id", "worker_id", "task_id")
    readonly_fields = [f.name for f in MessageSendAttempt._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
