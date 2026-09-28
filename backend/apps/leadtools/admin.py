from django.contrib import admin

from .models import GroupGrab, NumberCheck, NumberCheckEntry


class NumberCheckEntryInline(admin.TabularInline):
    model = NumberCheckEntry
    extra = 0
    can_delete = False
    readonly_fields = ["phone", "contact", "result", "jid", "checked_at"]
    max_num = 200


@admin.register(NumberCheck)
class NumberCheckAdmin(admin.ModelAdmin):
    list_display = [
        "id", "tenant", "whatsapp_account", "source", "status", "total", "on_whatsapp_count",
        "not_on_whatsapp_count", "error_count", "created_at",
    ]
    list_filter = ["status", "source", "created_at"]
    search_fields = ["tenant__name", "whatsapp_account__name"]
    readonly_fields = [
        "tenant", "created_by", "whatsapp_account", "source", "contact_group", "total", "checked",
        "on_whatsapp_count", "not_on_whatsapp_count", "error_count", "error",
        "created_at", "started_at", "completed_at",
    ]
    inlines = [NumberCheckEntryInline]


@admin.register(GroupGrab)
class GroupGrabAdmin(admin.ModelAdmin):
    list_display = ["id", "tenant", "whatsapp_account", "contact_group", "members_found", "members_hidden", "created_at"]
    list_filter = ["created_at"]
    search_fields = ["tenant__name", "whatsapp_account__name", "contact_group__name"]
    readonly_fields = [
        "tenant", "created_by", "whatsapp_account", "groups", "contact_group", "contact_import",
        "members_found", "members_hidden", "created_at",
    ]
