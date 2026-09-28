from django.contrib import admin

from .models import Campaign


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = (
        "name", "tenant", "status", "audience_type", "total_recipients",
        "sent_count", "failed_count", "recovery_required_count", "created_at",
    )
    list_filter = ("status", "audience_type")
    search_fields = ("name",)
    readonly_fields = (
        "total_recipients", "sent_count", "delivered_count", "read_count",
        "failed_count", "skipped_count", "recovery_required_count", "started_at", "completed_at",
    )
