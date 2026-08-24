from django.contrib import admin

from .models import Tenant


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "parent", "is_active", "created_at")
    list_filter = ("kind", "is_active")
    search_fields = ("name", "slug")
