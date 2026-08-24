from django.contrib import admin

from .models import ClickToChatLink, LeadEvent, LinkVisit


@admin.register(ClickToChatLink)
class ClickToChatLinkAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "tenant", "whatsapp_account", "is_active", "click_count", "lead_count"]
    list_filter = ["tenant", "is_active"]
    search_fields = ["name", "code"]
    readonly_fields = ["code", "click_count", "lead_count"]


@admin.register(LeadEvent)
class LeadEventAdmin(admin.ModelAdmin):
    list_display = ["created_at", "kind", "contact", "link"]
    list_filter = ["kind", "created_at"]


@admin.register(LinkVisit)
class LinkVisitAdmin(admin.ModelAdmin):
    list_display = ["link", "visited_at", "ip_address"]
