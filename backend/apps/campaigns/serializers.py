from django.conf import settings
from rest_framework import serializers

from apps.whatsapp.models import WhatsAppAccount
from core.button_fields import ButtonsField, validate_button_body
from core.interactive_fields import ListContentField, PollContentField, validate_interactive_content
from core.media_fields import MediaItemsField, apply_media_items

from .assignment import resolve_delay_bounds
from .models import Campaign


class CampaignSerializer(serializers.ModelSerializer):
    # Declared explicitly so it can be omitted in AUTO mode, where the user
    # picks no specific device. The model FK stays non-null (see
    # Campaign.whatsapp_account) -- validate() fills it in from the connected
    # pool instead, so nothing downstream has to cope with a null account.
    whatsapp_account = serializers.PrimaryKeyRelatedField(
        queryset=WhatsAppAccount.objects.all(), required=False, allow_null=True,
    )
    # The bounds actually in force, campaign overrides resolved against the
    # deployment defaults -- so the UI can show what a campaign that has
    # never set them will really do.
    media_items = MediaItemsField(required=False)
    buttons = ButtonsField(required=False)
    list_content = ListContentField(required=False)
    poll_content = PollContentField(required=False)
    effective_min_delay_seconds = serializers.SerializerMethodField()
    effective_max_delay_seconds = serializers.SerializerMethodField()

    class Meta:
        model = Campaign
        fields = (
            "id", "name", "whatsapp_account", "template",
            "account_assignment_mode", "min_delay_seconds", "max_delay_seconds",
            "effective_min_delay_seconds", "effective_max_delay_seconds",
            "message_type", "message_body", "media_url", "media_items", "caption", "filename", "buttons",
            "list_content", "poll_content",
            "append_opt_out",
            "track_links",
            "audience_type", "audience_group", "audience_tag", "audience_segment", "audience_contact_ids",
            "scheduled_at", "status",
            "total_recipients", "sent_count", "delivered_count", "read_count", "failed_count", "skipped_count",
            "recovery_required_count",
            "created_at", "updated_at", "started_at", "completed_at",
        )
        read_only_fields = (
            "id", "status", "total_recipients", "sent_count", "delivered_count", "read_count",
            "failed_count", "skipped_count", "recovery_required_count",
            "effective_min_delay_seconds", "effective_max_delay_seconds",
            "created_at", "updated_at", "started_at", "completed_at",
        )

    def get_effective_min_delay_seconds(self, obj):
        return resolve_delay_bounds(obj)[0]

    def get_effective_max_delay_seconds(self, obj):
        return resolve_delay_bounds(obj)[1]

    def _resolve_assignment_mode(self, attrs):
        if "account_assignment_mode" in attrs:
            return attrs["account_assignment_mode"]
        if self.instance is not None:
            return self.instance.account_assignment_mode
        # New campaign, mode not stated: fall back to the deployment default
        # rather than hard-coding one here (see settings.CAMPAIGN_*).
        configured = settings.CAMPAIGN_ACCOUNT_ASSIGNMENT_MODE
        if configured in Campaign.AccountAssignmentMode.values:
            return configured
        return Campaign.AccountAssignmentMode.MANUAL

    def _validate_delays(self, attrs):
        def current(field):
            if field in attrs:
                return attrs[field]
            return getattr(self.instance, field, None)

        minimum = current("min_delay_seconds")
        maximum = current("max_delay_seconds")

        for field, value in (("min_delay_seconds", minimum), ("max_delay_seconds", maximum)):
            if value is not None and value < 0:
                raise serializers.ValidationError({field: "Delay cannot be negative."})

        # Compare against the resolved counterpart, not the raw one: setting
        # only a maximum of 2s on a deployment whose default minimum is 5s
        # would otherwise pass validation and then silently be corrected.
        resolved_min = minimum if minimum is not None else settings.CAMPAIGN_MIN_DELAY_SECONDS
        resolved_max = maximum if maximum is not None else settings.CAMPAIGN_MAX_DELAY_SECONDS
        if resolved_max < resolved_min:
            raise serializers.ValidationError(
                {"max_delay_seconds": "Maximum delay must be greater than or equal to the minimum delay."}
            )

    def validate(self, attrs):
        attrs = apply_media_items(attrs)
        tenant = self.context["request"].user.tenant

        for field in ("whatsapp_account", "template", "audience_group", "audience_tag", "audience_segment"):
            value = attrs.get(field)
            if value is not None and value.tenant_id != tenant.id:
                raise serializers.ValidationError({field: "Must belong to your tenant."})

        audience_type = attrs.get("audience_type", getattr(self.instance, "audience_type", Campaign.AudienceType.ALL))
        required_field = {
            Campaign.AudienceType.GROUP: "audience_group",
            Campaign.AudienceType.TAG: "audience_tag",
            Campaign.AudienceType.SEGMENT: "audience_segment",
        }.get(audience_type)
        if required_field and not attrs.get(required_field, getattr(self.instance, required_field, None)):
            raise serializers.ValidationError({required_field: f"Required when audience_type is {audience_type}."})

        template = attrs.get("template", getattr(self.instance, "template", None))
        message_body = attrs.get("message_body", getattr(self.instance, "message_body", ""))
        media_url = attrs.get("media_url", getattr(self.instance, "media_url", ""))
        poll_content = attrs.get("poll_content", getattr(self.instance, "poll_content", {}))
        # A poll is content in its own right: its question is the message,
        # so a poll campaign legitimately has no body and no attachment.
        if not template and not message_body and not media_url and not poll_content:
            raise serializers.ValidationError(
                "Select a template, or provide message_body, media_url or a poll."
            )

        # Only the campaign's own buttons are checked here: a template brings
        # its own, already validated when the template was saved.
        validate_button_body(attrs.get("buttons", getattr(self.instance, "buttons", [])), message_body,
                             field="message_body")
        validate_interactive_content(attrs, self.instance, body_field="message_body")

        self._validate_delays(attrs)

        mode = self._resolve_assignment_mode(attrs)
        attrs["account_assignment_mode"] = mode

        account = attrs.get("whatsapp_account") or getattr(self.instance, "whatsapp_account", None)
        if account is None:
            if mode != Campaign.AccountAssignmentMode.AUTO:
                raise serializers.ValidationError({"whatsapp_account": "This field is required."})
            # AUTO: the campaign still needs a primary account on record (the
            # FK is non-null, and it is what MANUAL falls back to if the mode
            # is switched later). Any connected account will do -- which one
            # a given recipient actually gets is decided per message at
            # dispatch time, not here.
            account = (
                WhatsAppAccount.objects.filter(tenant=tenant, status=WhatsAppAccount.Status.CONNECTED)
                .order_by("id")
                .first()
            )
            if account is None:
                raise serializers.ValidationError(
                    {"whatsapp_account": "Connect at least one WhatsApp device before creating a campaign."}
                )
        attrs["whatsapp_account"] = account

        return attrs


class CampaignEstimateSerializer(serializers.Serializer):
    estimated_recipients = serializers.IntegerField()
