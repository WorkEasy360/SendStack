from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):

    list_display = (
        "username",
        "email",
        "mobile",
        "tenant",
        "role",
        "credits",
        "is_active",
        "created_at",
    )

    list_filter = (
        "role",
        "is_active",
        "is_staff",
    )

    search_fields = (
        "username",
        "email",
        "mobile",
    )

    fieldsets = UserAdmin.fieldsets + (
        (
            "MultiSender Information",
            {
                "fields": (
                    "role",
                    "tenant",
                    "mobile",
                    "credits",
                )
            },
        ),
    )
