from django.contrib import admin

from .models import Activity, ContactNote, Deal, Pipeline, PipelineStage, Task


class PipelineStageInline(admin.TabularInline):
    model = PipelineStage
    extra = 0


@admin.register(Pipeline)
class PipelineAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "is_default", "created_at")
    list_filter = ("is_default",)
    search_fields = ("name",)
    inlines = [PipelineStageInline]


@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "contact", "stage", "value", "currency", "status", "owner", "expected_close_date")
    list_filter = ("status", "currency")
    search_fields = ("name", "contact__name", "contact__phone")
    autocomplete_fields = ()
    raw_id_fields = ("contact", "pipeline", "stage", "owner", "created_by")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("tenant", "contact", "stage", "owner")


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "tenant", "contact", "due_at", "status", "assigned_to")
    list_filter = ("status", "kind")
    search_fields = ("title", "contact__name", "contact__phone")
    raw_id_fields = ("contact", "deal", "assigned_to", "created_by")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("tenant", "contact", "assigned_to")


@admin.register(ContactNote)
class ContactNoteAdmin(admin.ModelAdmin):
    list_display = ("contact", "tenant", "author", "created_at")
    search_fields = ("body", "contact__phone")
    raw_id_fields = ("contact", "author")


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ("contact", "kind", "summary", "actor", "occurred_at")
    list_filter = ("kind",)
    search_fields = ("summary", "contact__phone")
    raw_id_fields = ("contact", "actor")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("contact", "actor")
