from django.conf import settings
from rest_framework import serializers

from apps.contacts.models import ContactGroup, ContactImport

from .models import GroupGrab, NumberCheck, NumberCheckEntry


class ContactImportSummarySerializer(serializers.ModelSerializer):
    """The importer's own report, in the shape the contacts screen already reads."""

    class Meta:
        model = ContactImport
        fields = (
            "id", "status", "total_rows", "created_count", "updated_count", "skipped_count",
            "error_count", "errors", "created_at", "completed_at",
        )
        read_only_fields = fields


class NumberCheckSerializer(serializers.ModelSerializer):
    whatsapp_account_name = serializers.CharField(source="whatsapp_account.name", read_only=True)
    contact_group_name = serializers.CharField(source="contact_group.name", read_only=True, default=None)
    progress = serializers.IntegerField(read_only=True)

    class Meta:
        model = NumberCheck
        fields = (
            "id", "status", "source", "whatsapp_account", "whatsapp_account_name",
            "contact_group", "contact_group_name",
            "total", "checked", "on_whatsapp_count", "not_on_whatsapp_count", "error_count", "progress",
            "error", "created_at", "started_at", "completed_at",
        )
        read_only_fields = fields


class NumberCheckCreateSerializer(serializers.Serializer):
    whatsapp_account = serializers.IntegerField()
    source = serializers.ChoiceField(choices=NumberCheck.Source.choices)
    text = serializers.CharField(required=False, allow_blank=True, default="")
    # Digits only, applied to lines that carry no country code -- the same
    # rule the paste importer uses, for the same reason: a bare ten-digit
    # number is valid in more than one country and guessing is how a stranger
    # gets messaged.
    default_country_code = serializers.CharField(required=False, allow_blank=True, default="", max_length=4)
    contact_group = serializers.IntegerField(required=False, allow_null=True, default=None)

    def validate_default_country_code(self, value):
        value = (value or "").strip().lstrip("+")
        if value and not value.isdigit():
            raise serializers.ValidationError("Enter the country code as digits, e.g. 91.")
        return value

    def validate(self, attrs):
        if attrs["source"] == NumberCheck.Source.PASTE and not (attrs.get("text") or "").strip():
            raise serializers.ValidationError({"text": "Paste the numbers to check."})
        tenant = self.context["request"].user.tenant
        group_id = attrs.get("contact_group")
        if group_id is not None:
            group = ContactGroup.objects.filter(tenant=tenant, pk=group_id).first()
            if group is None:
                raise serializers.ValidationError({"contact_group": "Pick one of your own groups."})
            attrs["contact_group"] = group
        return attrs


class NumberCheckEntrySerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source="contact.name", read_only=True, default="")

    class Meta:
        model = NumberCheckEntry
        fields = ("id", "phone", "result", "contact", "contact_name", "checked_at")
        read_only_fields = fields


class SaveEntriesSerializer(serializers.Serializer):
    result = serializers.ChoiceField(
        choices=[
            (NumberCheckEntry.Result.ON_WHATSAPP, "On WhatsApp"),
            (NumberCheckEntry.Result.NOT_ON_WHATSAPP, "Not on WhatsApp"),
        ],
        default=NumberCheckEntry.Result.ON_WHATSAPP,
    )
    group_name = serializers.CharField(max_length=100)

    def validate_group_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Name the group these numbers go into.")
        return value


class GroupGrabSerializer(serializers.ModelSerializer):
    whatsapp_account_name = serializers.CharField(source="whatsapp_account.name", read_only=True)
    contact_group_name = serializers.CharField(source="contact_group.name", read_only=True, default=None)
    contact_import = ContactImportSummarySerializer(read_only=True)

    class Meta:
        model = GroupGrab
        fields = (
            "id", "whatsapp_account", "whatsapp_account_name", "groups",
            "contact_group", "contact_group_name", "contact_import",
            "members_found", "members_hidden", "created_at",
        )
        read_only_fields = fields


class GroupGrabCreateSerializer(serializers.Serializer):
    whatsapp_account = serializers.IntegerField()
    group_ids = serializers.ListField(
        child=serializers.CharField(max_length=64), min_length=1, allow_empty=False,
    )
    contact_group_name = serializers.CharField(required=False, allow_blank=True, default="", max_length=100)

    def validate_group_ids(self, value):
        ceiling = settings.LEADTOOLS_GROUP_GRAB_MAX_GROUPS
        unique = list(dict.fromkeys(value))
        if len(unique) > ceiling:
            raise serializers.ValidationError(f"Pick at most {ceiling} groups at a time.")
        return unique
