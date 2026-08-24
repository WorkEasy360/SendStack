from rest_framework import serializers

from .models import Contact, ContactGroup, ContactImport, ContactSegment, ContactTag
from .utils import normalize_phone


class ContactGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactGroup
        fields = ("id", "name", "description", "created_at")
        read_only_fields = ("id", "created_at")


class ContactTagSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactTag
        fields = ("id", "name", "color", "created_at")
        read_only_fields = ("id", "created_at")


class ContactSerializer(serializers.ModelSerializer):
    groups = serializers.PrimaryKeyRelatedField(many=True, queryset=ContactGroup.objects.all(), required=False)
    tags = serializers.PrimaryKeyRelatedField(many=True, queryset=ContactTag.objects.all(), required=False)
    # Derived, never stored: the band is a view of `lead_score`, so returning
    # it saves every caller from re-implementing the two thresholds.
    score_band = serializers.CharField(read_only=True)
    owner_username = serializers.CharField(source="owner.username", read_only=True, default=None)

    class Meta:
        model = Contact
        fields = (
            "id", "name", "phone", "email", "company", "country", "custom_fields", "subscribed",
            "unsubscribed_at", "groups", "tags", "last_message_at", "created_at", "updated_at",
            "owner", "owner_username", "lifecycle_status", "lead_score", "score_band",
            "last_inbound_at", "last_outbound_at",
        )
        read_only_fields = (
            "id", "unsubscribed_at", "last_message_at", "created_at", "updated_at",
            "lead_score", "score_band", "last_inbound_at", "last_outbound_at", "owner_username",
        )

    def validate_phone(self, value):
        try:
            return normalize_phone(value)
        except ValueError as exc:
            raise serializers.ValidationError("Enter a valid E.164 phone number.") from exc

    def validate_owner(self, value):
        if value is not None and value.tenant_id != self.context["request"].user.tenant_id:
            raise serializers.ValidationError("Must be someone on your team.")
        return value

    def validate(self, attrs):
        tenant = self.context["request"].user.tenant
        for relation in ("groups", "tags"):
            for item in attrs.get(relation, []):
                if item.tenant_id != tenant.id:
                    raise serializers.ValidationError({relation: "Items must belong to your tenant."})
        return attrs


class ContactImportSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactImport
        fields = (
            "id", "file", "status", "group_ids", "tag_ids", "column_mapping",
            "total_rows", "created_count", "updated_count", "skipped_count", "error_count",
            "errors", "created_at", "completed_at",
        )
        read_only_fields = (
            "id", "status", "column_mapping", "total_rows", "created_count", "updated_count",
            "skipped_count", "error_count", "errors", "created_at", "completed_at",
        )

    def validate_file(self, value):
        if not value.name.lower().endswith(".csv"):
            raise serializers.ValidationError("Only .csv files are supported.")
        max_size = 10 * 1024 * 1024
        if value.size > max_size:
            raise serializers.ValidationError("File must be smaller than 10MB.")
        return value

    def validate(self, attrs):
        tenant = self.context["request"].user.tenant
        for field in ("group_ids", "tag_ids"):
            ids = attrs.get(field) or []
            model = ContactGroup if field == "group_ids" else ContactTag
            if model.objects.filter(tenant=tenant, id__in=ids).count() != len(set(ids)):
                raise serializers.ValidationError({field: "All ids must belong to your tenant."})
        return attrs


ALLOWED_SEGMENT_FIELDS = {
    "name": {"eq", "contains"},
    "email": {"eq", "contains"},
    "country": {"eq"},
    "subscribed": {"eq"},
    "tags": {"eq"},
    "groups": {"eq"},
}


class ContactSegmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactSegment
        fields = ("id", "name", "filters", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_filters(self, value):
        if not isinstance(value, list) or not value:
            raise serializers.ValidationError("Provide at least one filter.")
        for rule in value:
            if not isinstance(rule, dict):
                raise serializers.ValidationError("Each filter must be an object.")
            field = rule.get("field")
            operator = rule.get("operator")
            if field not in ALLOWED_SEGMENT_FIELDS:
                raise serializers.ValidationError(f"Unsupported field '{field}'.")
            if operator not in ALLOWED_SEGMENT_FIELDS[field]:
                raise serializers.ValidationError(f"Unsupported operator '{operator}' for '{field}'.")
            if "value" not in rule:
                raise serializers.ValidationError("Each filter needs a 'value'.")
        return value
