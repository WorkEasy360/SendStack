from django.contrib import admin

from .models import Conversation, ConversationLabel, ConversationNote


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "contact", "status", "assigned_agent", "unread_count", "last_message_at")
    list_filter = ("status",)
    search_fields = ("contact__name", "contact__phone")


@admin.register(ConversationLabel)
class ConversationLabelAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "color")


@admin.register(ConversationNote)
class ConversationNoteAdmin(admin.ModelAdmin):
    list_display = ("conversation", "author", "created_at")
