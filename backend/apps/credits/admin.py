from django.contrib import admin

from .models import CreditTransaction


@admin.register(CreditTransaction)
class CreditTransactionAdmin(admin.ModelAdmin):
    list_display = ("id", "account", "type", "amount", "balance_before", "balance_after", "created_at")
    list_filter = ("type",)
    search_fields = ("account__username", "reference")
    readonly_fields = [f.name for f in CreditTransaction._meta.fields]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
