from django.contrib import admin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("action", "actor", "acting_as", "tenant", "created_at")
    list_filter = ("action", "tenant")
    search_fields = ("object_type", "object_id")
    readonly_fields = ("actor", "acting_as", "tenant", "action", "object_type", "object_id", "metadata", "ip_address", "user_agent", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
