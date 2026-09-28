"""
What a support request looks like on the wire, to each of the two audiences.

Two serializers, because "the customer who raised it" and "the person
answering it" are genuinely different readers with different rights. A single
serializer with conditional fields is how an internal note ends up in a
customer's response the first time somebody adds a field and forgets the
condition.
"""

from rest_framework import serializers

from .models import SupportRequest


class SupportRequestCreateSerializer(serializers.ModelSerializer):
    """
    What a customer may send.

    The field list is the allowlist. Everything that decides how the request is
    handled -- the tenant, the author, the reference, the status, the priority,
    the captured context -- is set by the view from the request, never read
    from the body. A customer who posts ``{"priority": "URGENT", "tenant": 7}``
    changes nothing: those names are not in `fields`, so DRF discards them
    before validation rather than after.
    """

    class Meta:
        model = SupportRequest
        fields = ("category", "subject", "message", "campaign_id")

    def validate_subject(self, value):
        value = (value or "").strip()
        if len(value) < 3:
            raise serializers.ValidationError("Give your request a short subject.")
        return value

    def validate_message(self, value):
        value = (value or "").strip()
        if len(value) < 10:
            # Not pedantry: a three-word request produces a round trip asking
            # what happened, and the customer waits a day for a question.
            raise serializers.ValidationError(
                "Tell us a little more about what happened, so we can help without having to ask."
            )
        return value


class SupportRequestSerializer(serializers.ModelSerializer):
    """
    A request as the customer who raised it sees it.

    `internal_note` and `context` are both absent. The note is written for
    whoever is handling it and may say things about a workspace that are true
    and unhelpful to send back; the context is a snapshot of their own account
    which they can already see live, and returning it would only create a
    second, staler copy of it in the UI.
    """

    category_label = serializers.CharField(source="get_category_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = SupportRequest
        fields = (
            "id", "reference", "category", "category_label", "subject", "message",
            "status", "status_label", "campaign_id", "created_at", "resolved_at",
        )
        read_only_fields = fields


class SupportRequestAdminSerializer(serializers.ModelSerializer):
    """
    A request as whoever answers it sees it: everything, including the snapshot.

    `context` is included here and only here. What it may contain is decided in
    services.capture_context, not by this serializer -- counts, statuses and
    identifiers, never a phone number, a contact name or a message body.
    """

    tenant_name = serializers.CharField(source="tenant.name", read_only=True)
    created_by_username = serializers.CharField(source="created_by.username", read_only=True, default="")
    category_label = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = SupportRequest
        fields = (
            "id", "reference", "tenant", "tenant_name", "created_by_username",
            "category", "category_label", "subject", "message",
            "status", "priority", "correlation_id", "campaign_id",
            "context", "internal_note", "created_at", "updated_at", "resolved_at",
        )
        read_only_fields = (
            "id", "reference", "tenant", "tenant_name", "created_by_username",
            "category", "category_label", "subject", "message",
            "correlation_id", "campaign_id", "context", "created_at", "updated_at",
        )
