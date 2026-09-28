from rest_framework import serializers

from core.uploads import UploadRejected, validate_upload

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
            "marketing_consent", "consent_source", "consent_at", "consent_reference",
            "whatsapp_status", "whatsapp_checked_at",
        )
        # `subscribed` and every consent field are read here and written only
        # through the dedicated actions (unsubscribe / resubscribe /
        # set-consent), never through a plain PATCH.
        #
        # This is the hole the audit was looking for. While `subscribed` was
        # writable, `PATCH /contacts/{id}/ {"subscribed": true}` silently
        # re-enabled a contact who had replied STOP: no ConsentEvent, no
        # `unsubscribed_at` cleared, nothing in the log to say it happened.
        # A CRM sync, a bulk edit or an integration doing a read-modify-write
        # of the whole object would do it by accident, because a GET returns
        # `subscribed` and a PUT sends it straight back.
        #
        # Consent state now changes only where a consent record is written
        # alongside it, so the flag and its evidence cannot come apart.
        read_only_fields = (
            "id", "unsubscribed_at", "last_message_at", "created_at", "updated_at",
            "lead_score", "score_band", "last_inbound_at", "last_outbound_at", "owner_username",
            "subscribed", "marketing_consent", "consent_source", "consent_at", "consent_reference",
            # Written only by the number filter (apps.leadtools), which is the
            # one thing that has actually asked WhatsApp.
            "whatsapp_status", "whatsapp_checked_at",
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
            "consent_source", "consent_reference",
            "total_rows", "created_count", "updated_count", "skipped_count", "error_count",
            "errors", "created_at", "completed_at",
        )
        read_only_fields = (
            "id", "status", "column_mapping", "total_rows", "created_count", "updated_count",
            "skipped_count", "error_count", "errors", "created_at", "completed_at",
        )

    def validate_file(self, value):
        max_size = 10 * 1024 * 1024
        if value.size > max_size:
            raise serializers.ValidationError("File must be smaller than 10MB.")
        # The extension alone is not evidence: this file is written under
        # MEDIA_ROOT, which nginx serves back on this origin, so an HTML
        # payload renamed to ".csv" would be a stored script if the bytes were
        # never looked at. validate_upload checks that it really is text.
        try:
            validate_upload(value, allowed={".csv", ".xlsx"})
        except UploadRejected as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value

    def validate_consent_source(self, value):
        """
        Blank, or one of the recorded sources. Never a free-text claim.

        A source somebody can type is a source nobody can audit -- the point of
        the list is that "where did this consent come from" has a small set of
        answers a reviewer recognises.
        """
        from .models import Contact

        if not value:
            return ""
        value = value.upper()
        if value not in Contact.ConsentSource.values:
            raise serializers.ValidationError(
                f"Choose one of: {', '.join(Contact.ConsentSource.values)}."
            )
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
    "marketing_consent": {"eq"},
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


class PasteParseSerializer(serializers.Serializer):
    """
    A block of pasted text to classify. Nothing is created by this request.

    `default_country_code` is required in practice rather than in the schema:
    a number pasted without one is reported as NO_COUNTRY_CODE instead of
    being guessed at, and the screen asks for the code once rather than per
    row. See apps/contacts/paste.py for why guessing would be worse than
    refusing.
    """

    text = serializers.CharField(allow_blank=True, trim_whitespace=False)
    default_country_code = serializers.CharField(required=False, allow_blank=True, default="", max_length=6)


class PasteRowSerializer(serializers.Serializer):
    """One row the reviewer has decided to keep."""

    name = serializers.CharField(required=False, allow_blank=True, default="", max_length=150)
    phone = serializers.CharField(max_length=32)


class PasteImportSerializer(serializers.Serializer):
    """
    The reviewed rows, committed.

    Takes the rows rather than the original text on purpose: what gets
    imported is what the person saw and approved in the review table, not a
    re-parse of the paste that might classify a line differently a minute
    later. Every phone is re-validated server-side regardless -- the browser's
    parse is a convenience, never the authority.
    """

    rows = PasteRowSerializer(many=True)
    group_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    tag_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    consent_source = serializers.CharField(required=False, allow_blank=True, default="", max_length=20)
    consent_reference = serializers.CharField(required=False, allow_blank=True, default="", max_length=120)

    def validate_rows(self, value):
        from .paste import MAX_LINES

        if not value:
            raise serializers.ValidationError("There is nothing to import.")
        if len(value) > MAX_LINES:
            raise serializers.ValidationError(
                f"At most {MAX_LINES:,} contacts can be imported from a paste at once "
                f"({len(value):,} given). Upload the rest as a CSV."
            )
        return value
