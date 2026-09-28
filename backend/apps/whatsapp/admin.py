from django.contrib import admin

from .models import WhatsAppAccount, WhatsAppConnectionSession, WhatsAppWebhookEvent


@admin.register(WhatsAppAccount)
class WhatsAppAccountAdmin(admin.ModelAdmin):

    list_display = (
        "name",
        "phone_number",
        "account_type",
        "connection_method",
        "provider",
        "status",
        "tenant",
        "user",
        "last_connected_at",
        "created_at",
        "archived_at",
    )

    list_filter = (
        "status",
        "account_type",
        "connection_method",
        "provider",
    )

    def get_queryset(self, request):
        # `all_objects`, so support can still see a device the user deleted
        # that had send history behind it -- the default manager hides those
        # from the product on purpose. See WhatsAppAccount.archived_at.
        return WhatsAppAccount.all_objects.all()

    search_fields = (
        "name",
        "phone_number",
        "phone_number_id",
        "provider_account_id",
        "user__username",
    )

    readonly_fields = (
        "provider",
        "provider_account_id",
        "provider_session_id",
        "access_token_encrypted",
        "app_secret_encrypted",
        "last_connected_at",
        "last_seen_at",
        "created_at",
        "updated_at",
    )


@admin.register(WhatsAppConnectionSession)
class WhatsAppConnectionSessionAdmin(admin.ModelAdmin):

    list_display = (
        "whatsapp_account",
        "provider",
        "status",
        "session_id",
        "qr_expires_at",
        "connected_at",
        "created_at",
    )

    list_filter = ("status", "provider")
    search_fields = ("session_id", "whatsapp_account__name")
    readonly_fields = (
        "whatsapp_account",
        "provider",
        "session_id",
        "created_at",
        "updated_at",
    )


@admin.register(WhatsAppWebhookEvent)
class WhatsAppWebhookEventAdmin(admin.ModelAdmin):

    list_display = (
        "account",
        "event_type",
        "provider_event_id",
        "processed_at",
        "received_at",
    )

    list_filter = ("event_type",)
    search_fields = ("provider_event_id",)
    readonly_fields = ("account", "event_type", "provider_event_id", "payload", "received_at")
