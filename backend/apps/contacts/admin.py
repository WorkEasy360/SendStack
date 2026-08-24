from django.contrib import admin

from .models import Contact, ContactGroup, ContactImport, ContactSegment, ContactTag


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "tenant", "subscribed", "created_at")
    list_filter = ("subscribed", "tenant")
    search_fields = ("name", "phone", "email")


@admin.register(ContactGroup)
class ContactGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "created_at")


@admin.register(ContactTag)
class ContactTagAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "color", "created_at")


@admin.register(ContactImport)
class ContactImportAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "status", "created_count", "updated_count", "skipped_count", "error_count", "created_at")
    list_filter = ("status",)
    readonly_fields = ("total_rows", "created_count", "updated_count", "skipped_count", "error_count", "errors", "created_at", "completed_at")


@admin.register(ContactSegment)
class ContactSegmentAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "created_at")
