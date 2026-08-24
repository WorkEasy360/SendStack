from django.contrib import admin

from .models import Campaign


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "status", "audience_type", "total_recipients", "sent_count", "failed_count", "created_at")
    list_filter = ("status", "audience_type")
    search_fields = ("name",)
    readonly_fields = ("total_recipients", "sent_count", "delivered_count", "read_count", "failed_count", "started_at", "completed_at")
