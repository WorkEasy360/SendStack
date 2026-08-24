from django.contrib import admin

from .models import LinkClick, TrackedLink


@admin.register(TrackedLink)
class TrackedLinkAdmin(admin.ModelAdmin):
    list_display = ["code", "original_url", "campaign", "contact", "click_count", "last_clicked_at"]
    list_filter = ["tenant", "campaign"]
    search_fields = ["code", "original_url"]
    readonly_fields = ["code", "created_at"]


@admin.register(LinkClick)
class LinkClickAdmin(admin.ModelAdmin):
    list_display = ["link", "clicked_at", "ip_address"]
    list_filter = ["clicked_at"]
