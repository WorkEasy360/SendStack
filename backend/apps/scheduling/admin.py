from django.contrib import admin

from .models import ScheduledJob


@admin.register(ScheduledJob)
class ScheduledJobAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "object_id", "tenant", "scheduled_for", "status", "attempts", "executed_at")
    list_filter = ("status", "kind")
    search_fields = ("object_id",)
    readonly_fields = ("created_at", "updated_at", "executed_at", "locked_at", "locked_by")
    date_hierarchy = "scheduled_for"
