import re

from rest_framework import serializers

from .models import CallSession, CallTurn, VoiceAgentConfig

# E.164: a plus, a non-zero country digit, then up to fourteen more. Carriers
# reject anything else, and rejecting it here means the tenant finds out on the
# form rather than from a failed call and a bill.
E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")

EFFORTS = ("low", "medium", "high", "xhigh", "max")


def validate_e164(value):
    value = (value or "").strip().replace(" ", "")
    if value and not E164_RE.match(value):
        raise serializers.ValidationError(
            "Use the international format, starting with + and the country code (for example +919876543210)."
        )
    return value


class VoiceAgentConfigSerializer(serializers.ModelSerializer):
    # Secrets go in but never come out. The booleans are what the settings page
    # renders instead -- enough to show "configured", useless to an attacker
    # who has read the response.
    account_sid = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=True)
    auth_token = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=True)
    api_key = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=True)

    has_carrier_credentials = serializers.BooleanField(read_only=True)
    inbound_webhook_url = serializers.SerializerMethodField()
    has_api_key = serializers.SerializerMethodField()
    account_sid_hint = serializers.SerializerMethodField()

    class Meta:
        model = VoiceAgentConfig
        fields = [
            "id", "is_enabled", "provider",
            "account_sid", "auth_token", "api_key",
            "has_carrier_credentials", "has_api_key", "account_sid_hint",
            "from_number", "display_name", "voice", "language", "speech_hints",
            "answer_inbound", "inbound_greeting", "inbound_webhook_url",
            "model", "effort", "temperature", "max_output_tokens",
            "business_context", "default_goal", "greeting", "voicemail_message",
            "knowledge_chunks", "answer_from_knowledge_only",
            "max_turns", "max_call_seconds", "silence_timeout_seconds", "max_silent_turns",
            "daily_call_limit",
            "allow_transfer", "transfer_number", "allow_whatsapp_followup",
            "allow_callback_scheduling", "record_calls",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_has_api_key(self, obj) -> bool:
        return bool(obj.get_api_key())

    def get_inbound_webhook_url(self, obj) -> str:
        """
        What to paste into the Twilio number's "A call comes in" setting.

        Built here rather than in the UI because the origin is a deployment
        setting the browser has no way to know.
        """
        from .services import public_base_url

        base = public_base_url()
        return f"{base}/voice/incoming/{obj.inbound_token}/" if base and obj.pk else ""

    def get_account_sid_hint(self, obj) -> str:
        """The last four characters, so a person can tell which account this is."""
        sid = obj.get_account_sid() or ""
        return f"...{sid[-4:]}" if len(sid) > 4 else ""

    def validate_from_number(self, value):
        return validate_e164(value)

    def validate_transfer_number(self, value):
        return validate_e164(value)

    def validate_effort(self, value):
        value = (value or "").strip().lower()
        if value and value not in EFFORTS:
            raise serializers.ValidationError(f"Choose one of: {', '.join(EFFORTS)}.")
        return value

    def validate_temperature(self, value):
        if not 0 <= value <= 1:
            raise serializers.ValidationError("Temperature must be between 0 and 1.")
        return value

    def validate(self, attrs):
        instance = self.instance
        enabling = attrs.get("is_enabled", getattr(instance, "is_enabled", False))
        if not enabling:
            return attrs

        # Everything below is a reason the first call would fail. Refusing to
        # switch the agent on is a better failure than a green toggle and a
        # phone that never rings.
        sid = attrs.get("account_sid") or (instance.get_account_sid() if instance else "")
        token = attrs.get("auth_token") or (instance.get_auth_token() if instance else "")
        from_number = attrs.get("from_number", getattr(instance, "from_number", ""))

        missing = []
        if not sid:
            missing.append("a Twilio Account SID")
        if not token:
            missing.append("a Twilio Auth Token")
        if not from_number:
            missing.append("a number to call from")
        if missing:
            raise serializers.ValidationError(
                {"is_enabled": f"The agent needs {', '.join(missing)} before it can be switched on."}
            )

        allow_transfer = attrs.get("allow_transfer", getattr(instance, "allow_transfer", False))
        transfer_number = attrs.get("transfer_number", getattr(instance, "transfer_number", ""))
        if allow_transfer and not transfer_number:
            raise serializers.ValidationError(
                {"transfer_number": "Transfers are switched on, so there has to be a number to transfer to."}
            )
        return attrs

    def update(self, instance, validated_data):
        # A blank secret means "leave it alone", which is what lets the form be
        # saved without retyping credentials it was never shown.
        for field, setter in (
            ("account_sid", instance.set_account_sid),
            ("auth_token", instance.set_auth_token),
            ("api_key", instance.set_api_key),
        ):
            value = validated_data.pop(field, None)
            if value:
                setter(value)
        return super().update(instance, validated_data)


class CallTurnSerializer(serializers.ModelSerializer):
    class Meta:
        model = CallTurn
        fields = ["id", "ordinal", "role", "text", "confidence", "latency_ms", "created_at"]


class CallSessionSerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source="contact.name", read_only=True, default="")
    created_by_name = serializers.CharField(source="created_by.username", read_only=True, default="")

    class Meta:
        model = CallSession
        fields = [
            "id", "direction", "provider", "provider_call_id", "status", "outcome",
            "contact", "contact_name", "created_by_name",
            "from_number", "to_number", "goal", "context", "collected",
            "turns_used", "recording_url", "summary", "error",
            "input_tokens", "output_tokens",
            "created_at", "started_at", "answered_at", "ended_at", "duration_seconds",
        ]
        read_only_fields = fields


class CallSessionDetailSerializer(CallSessionSerializer):
    turns = CallTurnSerializer(many=True, read_only=True)

    class Meta(CallSessionSerializer.Meta):
        fields = CallSessionSerializer.Meta.fields + ["turns"]
        read_only_fields = fields


class StartCallSerializer(serializers.Serializer):
    to_number = serializers.CharField()
    contact = serializers.IntegerField(required=False, allow_null=True)
    goal = serializers.CharField(required=False, allow_blank=True, default="")
    context = serializers.DictField(required=False, default=dict)

    def validate_to_number(self, value):
        value = validate_e164(value)
        if not value:
            raise serializers.ValidationError("A number to call is required.")
        return value


class BulkCallSerializer(serializers.Serializer):
    """Dial a list of contacts with one goal."""

    contacts = serializers.ListField(child=serializers.IntegerField(), allow_empty=False, max_length=500)
    goal = serializers.CharField(required=False, allow_blank=True, default="")
    context = serializers.DictField(required=False, default=dict)
