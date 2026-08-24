from django.contrib import admin

from .models import MessageTemplate


@admin.register(MessageTemplate)
class MessageTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "message_type", "is_active", "created_at")
    list_filter = ("message_type", "is_active")
    search_fields = ("name", "body")
