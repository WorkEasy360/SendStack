from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ["kind", "title", "user", "read_at", "created_at"]
    list_filter = ["kind", "created_at"]
    search_fields = ["title", "body"]
