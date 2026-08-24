from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import Activity, ContactNote, Deal, Pipeline, PipelineStage, Task


class PipelineStageSerializer(serializers.ModelSerializer):
    deal_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = PipelineStage
        fields = ("id", "pipeline", "name", "kind", "order", "probability", "color", "deal_count")
        read_only_fields = ("id", "deal_count")
        extra_kwargs = {"pipeline": {"required": False}}


class PipelineSerializer(serializers.ModelSerializer):
    stages = PipelineStageSerializer(many=True, read_only=True)

    class Meta:
        model = Pipeline
        fields = ("id", "name", "is_default", "stages", "created_at", "updated_at")
        read_only_fields = ("id", "stages", "created_at", "updated_at")


class _TenantOwnedMixin:
    """Every relation a client can set has to be checked against their tenant."""

    tenant_checked_fields = ()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        tenant = self.context["request"].user.tenant
        for field in self.tenant_checked_fields:
            value = attrs.get(field)
            if value is not None and getattr(value, "tenant_id", tenant.id) != tenant.id:
                raise serializers.ValidationError({field: "Must belong to your tenant."})
        return attrs

    def validate_assigned_to(self, value):
        return self._same_tenant_user(value, "assigned_to")

    def validate_owner(self, value):
        return self._same_tenant_user(value, "owner")

    def _same_tenant_user(self, value, field):
        if value is not None and value.tenant_id != self.context["request"].user.tenant_id:
            raise serializers.ValidationError("Must be someone on your team.")
        return value


class DealSerializer(_TenantOwnedMixin, serializers.ModelSerializer):
    tenant_checked_fields = ("contact", "pipeline", "stage")

    contact_name = serializers.SerializerMethodField()
    contact_phone = serializers.CharField(source="contact.phone", read_only=True)
    stage_name = serializers.CharField(source="stage.name", read_only=True)
    stage_kind = serializers.CharField(source="stage.kind", read_only=True)
    owner_username = serializers.CharField(source="owner.username", read_only=True, default=None)

    class Meta:
        model = Deal
        fields = (
            "id", "contact", "contact_name", "contact_phone", "pipeline", "stage", "stage_name", "stage_kind",
            "name", "value", "currency", "owner", "owner_username", "expected_close_date", "notes",
            "status", "created_at", "updated_at", "closed_at",
        )
        read_only_fields = ("id", "status", "created_at", "updated_at", "closed_at")
        extra_kwargs = {"pipeline": {"required": False}, "stage": {"required": False}}

    def get_contact_name(self, obj):
        return obj.contact.name or obj.contact.phone

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # A stage always belongs to the deal's pipeline. Without this check a
        # client can put a deal in another pipeline's "Won" column, and the
        # board silently loses it.
        stage = attrs.get("stage") or getattr(self.instance, "stage", None)
        pipeline = attrs.get("pipeline") or getattr(self.instance, "pipeline", None)
        if stage and pipeline and stage.pipeline_id != pipeline.pk:
            raise serializers.ValidationError({"stage": "That stage belongs to a different pipeline."})
        return attrs


class TaskSerializer(_TenantOwnedMixin, serializers.ModelSerializer):
    tenant_checked_fields = ("contact", "deal")

    contact_name = serializers.SerializerMethodField()
    contact_phone = serializers.CharField(source="contact.phone", read_only=True, default=None)
    assigned_to_username = serializers.CharField(source="assigned_to.username", read_only=True, default=None)
    is_overdue = serializers.BooleanField(read_only=True)

    class Meta:
        model = Task
        fields = (
            "id", "contact", "contact_name", "contact_phone", "deal", "title", "notes", "kind",
            "due_at", "assigned_to", "assigned_to_username", "status", "is_overdue",
            "created_at", "updated_at", "completed_at",
        )
        read_only_fields = ("id", "created_at", "updated_at", "completed_at", "is_overdue")

    def get_contact_name(self, obj):
        if not obj.contact_id:
            return None
        return obj.contact.name or obj.contact.phone


class ContactNoteSerializer(_TenantOwnedMixin, serializers.ModelSerializer):
    tenant_checked_fields = ("contact",)
    author_username = serializers.CharField(source="author.username", read_only=True, default=None)

    class Meta:
        model = ContactNote
        fields = ("id", "contact", "body", "author", "author_username", "created_at")
        read_only_fields = ("id", "author", "created_at")


class ActivitySerializer(serializers.ModelSerializer):
    actor_username = serializers.CharField(source="actor.username", read_only=True, default=None)

    class Meta:
        model = Activity
        fields = ("id", "contact", "kind", "summary", "metadata", "actor", "actor_username", "occurred_at")
        read_only_fields = fields


class TimelineEntrySerializer(serializers.Serializer):
    """The merged Activity + Message stream -- see services.contact_timeline."""

    id = serializers.CharField()
    source = serializers.CharField()
    kind = serializers.CharField()
    summary = serializers.CharField()
    metadata = serializers.DictField()
    actor = serializers.CharField(allow_null=True)
    at = serializers.DateTimeField()


class TodayItemSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    count = serializers.IntegerField()
    urgent = serializers.IntegerField()
    detail = serializers.CharField(allow_blank=True)
    link = serializers.CharField()


class TeamOptionSerializer(serializers.ModelSerializer):
    """The owner/assignee picker: just enough to render a dropdown."""

    class Meta:
        model = get_user_model()
        fields = ("id", "username", "first_name", "last_name")
        read_only_fields = fields
