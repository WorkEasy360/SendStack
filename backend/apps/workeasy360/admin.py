from django.contrib import admin

from .models import OrganizationLink


@admin.register(OrganizationLink)
class OrganizationLinkAdmin(admin.ModelAdmin):
    """Read-only: links are made by a first sign-in or by ``w360_link_organization`` (audited)."""

    list_display = ("organization_id", "tenant", "origin", "linked_by", "created_at")
    search_fields = ("organization_id", "tenant__name")
    readonly_fields = ("organization_id", "tenant", "origin", "linked_by", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
