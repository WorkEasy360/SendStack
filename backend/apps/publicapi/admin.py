"""
Operator views of the developer platform.

Every one of these is read-mostly. An operator needs to see that a key exists,
what it has been doing and why its webhooks are failing; none of that requires
being able to edit a scope list or a hashed secret by hand, and `hashed_secret`
is not displayed at all -- there is nothing useful an operator can do with it
and it is the one column worth keeping out of a screenshot.
"""

from django.contrib import admin

from .models import ApiKey, ApiRequestLog, IdempotencyRecord, WebhookDelivery, WebhookEndpoint


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("name", "masked_key", "tenant", "environment", "status", "last_used_at", "request_count")
    list_filter = ("environment", "is_active", "tenant")
    search_fields = ("name", "key_prefix", "tenant__name")
    readonly_fields = (
        "key_prefix", "hashed_secret", "request_count", "message_count",
        "last_used_at", "last_used_ip", "last_user_agent", "rotated_from",
        "rotated_at", "created_at", "updated_at",
    )
    # The full object is never creatable here: `add` would leave a row with no
    # usable secret, because minting one is services.create_api_key's job and
    # the plaintext has to be handed to a person at that moment.
    def has_add_permission(self, request):
        return False

    @admin.display(description="Key")
    def masked_key(self, obj):
        return obj.masked_key

    @admin.display(description="Status")
    def status(self, obj):
        return obj.status


@admin.register(ApiRequestLog)
class ApiRequestLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "tenant", "key_prefix", "method", "path", "status_code", "error_code", "duration_ms")
    list_filter = ("status_code", "method", "tenant")
    search_fields = ("path", "key_prefix", "request_id")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # Append-only, like the audit trail: a log somebody can edit is not
        # evidence of anything.
        return False


@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(admin.ModelAdmin):
    list_display = ("__str__", "tenant", "is_active", "consecutive_failures", "last_success_at", "last_failure_at")
    list_filter = ("is_active", "tenant")
    search_fields = ("name", "url", "tenant__name")
    readonly_fields = (
        "secret_encrypted", "last_success_at", "last_failure_at", "last_error",
        "consecutive_failures", "auto_disabled_at", "created_at", "updated_at",
    )


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "tenant", "endpoint", "event_type", "status", "attempt_count", "http_status")
    list_filter = ("status", "event_type", "tenant")
    search_fields = ("event_id",)
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False


@admin.register(IdempotencyRecord)
class IdempotencyRecordAdmin(admin.ModelAdmin):
    list_display = ("key", "tenant", "endpoint", "state", "status_code", "created_at", "expires_at")
    list_filter = ("state", "tenant")
    search_fields = ("key", "endpoint")

    def has_add_permission(self, request):
        return False
