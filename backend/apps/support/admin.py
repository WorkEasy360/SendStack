from django.contrib import admin

from .models import SupportRequest


@admin.register(SupportRequest)
class SupportRequestAdmin(admin.ModelAdmin):
    """
    The Django admin view of a request, for whoever has admin at all.

    The customer's words, the reference and the captured context are read-only.
    A support record whose question can be edited after the fact is not
    evidence of anything, and the snapshot beside it would stop describing the
    question it was taken for.
    """

    list_display = ("reference", "tenant", "category", "subject", "status", "priority", "created_at")
    list_filter = ("status", "priority", "category")
    search_fields = ("reference", "subject", "tenant__name")
    readonly_fields = (
        "reference", "tenant", "created_by", "category", "subject", "message",
        "correlation_id", "campaign_id", "context", "created_at", "updated_at",
    )
