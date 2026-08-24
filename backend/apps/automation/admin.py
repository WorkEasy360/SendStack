from django.contrib import admin

from .models import AutomationRule, FollowUpRule, Workflow, WorkflowRun


@admin.register(AutomationRule)
class AutomationRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "whatsapp_account", "trigger_type", "keyword", "cooldown_seconds", "is_active")
    list_filter = ("trigger_type", "is_active")
    search_fields = ("name", "keyword")


@admin.register(FollowUpRule)
class FollowUpRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "whatsapp_account", "delay_minutes", "template_scope", "is_active")
    list_filter = ("is_active",)
    filter_horizontal = ("trigger_templates",)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("trigger_templates")

    @admin.display(description="Armed by")
    def template_scope(self, obj):
        names = [t.name for t in obj.trigger_templates.all()]
        return ", ".join(names) if names else "Every message"


@admin.register(Workflow)
class WorkflowAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "whatsapp_account", "is_active", "created_at")
    list_filter = ("is_active",)


@admin.register(WorkflowRun)
class WorkflowRunAdmin(admin.ModelAdmin):
    list_display = ("id", "workflow", "contact", "status", "current_node_id", "steps_taken", "started_at", "error")
    list_filter = ("status",)
    readonly_fields = ("started_at", "updated_at", "completed_at", "steps_taken", "error")
