from django.contrib import admin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("action", "actor", "acting_as", "tenant", "created_at")
    list_filter = ("action", "tenant")
    search_fields = ("object_type", "object_id")
    readonly_fields = ("actor", "acting_as", "tenant", "action", "object_type", "object_id", "metadata", "ip_address", "user_agent", "created_at")

    # Read-only in all three directions, which is what "append-only" has to
    # mean to be worth anything. Add and change were already blocked; delete
    # was not, so an operator could remove the record of their own action from
    # the interface that records it. An audit trail that can be erased by the
    # people it audits is not evidence, it is decoration.
    #
    # Rows are still removable by a database administrator, which is the
    # correct remaining boundary: it takes deliberate access outside the
    # application, and the retention policy covers when that is appropriate.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
