from django.contrib import admin

from .models import Message


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("id", "to_phone", "campaign", "status", "message_type", "queued_at")
    list_filter = ("status", "message_type")
    search_fields = ("to_phone", "provider_message_id")
    readonly_fields = ("queued_at", "sent_at", "delivered_at", "read_at", "failed_at", "updated_at")
