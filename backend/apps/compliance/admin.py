from django.contrib import admin

from .models import CompliancePolicy, ConsentEvent


@admin.register(CompliancePolicy)
class CompliancePolicyAdmin(admin.ModelAdmin):
    list_display = ("tenant", "stop_keywords_enabled", "quiet_hours_enabled", "timezone", "updated_at")
    list_filter = ("stop_keywords_enabled", "quiet_hours_enabled")


@admin.register(ConsentEvent)
class ConsentEventAdmin(admin.ModelAdmin):
    list_display = ("contact", "event", "source", "actor", "created_at")
    list_filter = ("event", "source")
    search_fields = ("contact__phone", "contact__name", "detail")
    readonly_fields = ("tenant", "contact", "event", "source", "actor", "message", "detail", "created_at")
