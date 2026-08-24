from rest_framework import serializers

from core.button_fields import ButtonsField, validate_button_body
from core.media_fields import MediaItemsField, apply_media_items

from .models import AutomationRule, FollowUpRule, Workflow, WorkflowRun
from .services import WorkflowError, trigger_config, validate_nodes


class _ContentValidationMixin:
    def validate(self, attrs):
        attrs = apply_media_items(attrs)
        tenant = self.context["request"].user.tenant

        account = attrs.get("whatsapp_account", getattr(self.instance, "whatsapp_account", None))
        if account is not None and account.tenant_id != tenant.id:
            raise serializers.ValidationError({"whatsapp_account": "Must belong to your tenant."})

        template = attrs.get("template", getattr(self.instance, "template", None))
        if template is not None and template.tenant_id != tenant.id:
            raise serializers.ValidationError({"template": "Must belong to your tenant."})

        message_body = attrs.get("message_body", getattr(self.instance, "message_body", ""))
        media_url = attrs.get("media_url", getattr(self.instance, "media_url", ""))
        if not template and not message_body and not media_url:
            raise serializers.ValidationError("Select a template or provide message_body/media_url.")

        # A template carries its own (already validated) buttons; only inline
        # content is checked here.
        validate_button_body(attrs.get("buttons", getattr(self.instance, "buttons", [])), message_body,
                             field="message_body")

        return attrs


class AutomationRuleSerializer(_ContentValidationMixin, serializers.ModelSerializer):
    media_items = MediaItemsField(required=False)
    buttons = ButtonsField(required=False)

    class Meta:
        model = AutomationRule
        fields = (
            "id", "name", "whatsapp_account", "trigger_type", "keyword", "keyword_match_type", "button_id",
            "priority", "cooldown_seconds", "template", "message_type", "message_body", "media_url", "media_items",
            "caption", "filename", "buttons", "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        trigger_type = attrs.get("trigger_type", getattr(self.instance, "trigger_type", None))
        keyword = attrs.get("keyword", getattr(self.instance, "keyword", ""))
        if trigger_type == AutomationRule.Trigger.KEYWORD and not keyword:
            raise serializers.ValidationError({"keyword": "Required for keyword-triggered rules."})

        button_id = attrs.get("button_id", getattr(self.instance, "button_id", ""))
        if trigger_type == AutomationRule.Trigger.BUTTON and not button_id:
            raise serializers.ValidationError(
                {"button_id": "Required for button-triggered rules -- pick the button this rule answers."}
            )
        return attrs


class FollowUpRuleSerializer(_ContentValidationMixin, serializers.ModelSerializer):
    media_items = MediaItemsField(required=False)
    buttons = ButtonsField(required=False)

    class Meta:
        model = FollowUpRule
        fields = (
            "id", "name", "whatsapp_account", "delay_minutes", "trigger_templates",
            "template", "message_type", "message_body", "media_url", "media_items", "caption", "filename",
            "buttons", "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        trigger_templates = attrs.get("trigger_templates")
        if trigger_templates:
            tenant = self.context["request"].user.tenant
            foreign = [t for t in trigger_templates if t.tenant_id != tenant.id]
            if foreign:
                raise serializers.ValidationError({"trigger_templates": "Must belong to your tenant."})
        return attrs


class WorkflowSerializer(serializers.ModelSerializer):
    trigger = serializers.SerializerMethodField()

    class Meta:
        model = Workflow
        fields = ("id", "name", "whatsapp_account", "nodes", "trigger", "is_active", "created_at", "updated_at")
        read_only_fields = ("id", "trigger", "created_at", "updated_at")

    def get_trigger(self, obj):
        """What starts this workflow, lifted out of the TRIGGER node so a list
        view can say "on keyword: price" without parsing the graph itself."""
        return trigger_config(obj.nodes)

    def validate_whatsapp_account(self, value):
        if value.tenant_id != self.context["request"].user.tenant.id:
            raise serializers.ValidationError("Must belong to your tenant.")
        return value

    def validate_nodes(self, value):
        try:
            validate_nodes(value)
        except WorkflowError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value


class WorkflowRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowRun
        fields = (
            "id", "workflow", "contact", "conversation", "current_node_id", "status", "error", "steps_taken",
            "context", "started_at", "updated_at", "completed_at",
        )
        read_only_fields = fields


class StartWorkflowSerializer(serializers.Serializer):
    contact_id = serializers.IntegerField(min_value=1)
    # Start a second run even though this contact is part-way through one.
    # Off by default -- see services.start_workflow for why concurrent runs
    # of the same sequence are almost always a mistake.
    force = serializers.BooleanField(required=False, default=False)
