from django.contrib import admin

from .models import LegalAcceptance


@admin.register(LegalAcceptance)
class LegalAcceptanceAdmin(admin.ModelAdmin):
    """
    Read-only on purpose: this is the evidence trail. An acceptance an operator
    can edit or delete from the admin is not evidence of anything.
    """

    list_display = ("user", "tenant", "document", "version", "accepted_at")
    list_filter = ("document", "version")
    search_fields = ("user__username", "user__email", "tenant__name")
    readonly_fields = ("user", "tenant", "document", "version", "accepted_at", "ip_address", "user_agent")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
