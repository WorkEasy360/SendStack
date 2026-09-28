from django.contrib import admin

from .models import FunnelEvent, OnboardingState


@admin.register(OnboardingState)
class OnboardingStateAdmin(admin.ModelAdmin):
    list_display = ("tenant", "is_guided", "activated_at", "whatsapp_connected_at", "test_message_sent_at")
    list_filter = ("is_guided",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(FunnelEvent)
class FunnelEventAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "occurred_at")
    list_filter = ("name",)
    date_hierarchy = "occurred_at"
