from rest_framework import serializers

from .keywords import START_KEYWORDS, STOP_KEYWORDS
from .models import CompliancePolicy, ConsentEvent
from .timezones import COUNTRY_TIMEZONES, load_zone


class KeywordListField(serializers.ListField):
    """A tenant's own stop/start phrases: trimmed, de-duplicated, bounded."""

    child = serializers.CharField(max_length=60, allow_blank=False, trim_whitespace=True)

    def to_internal_value(self, data):
        phrases = super().to_internal_value(data)
        if len(phrases) > 50:
            raise serializers.ValidationError("Add at most 50 keywords.")

        seen, unique = set(), []
        for phrase in phrases:
            folded = phrase.casefold()
            if folded not in seen:
                seen.add(folded)
                unique.append(phrase)
        return unique


class CompliancePolicySerializer(serializers.ModelSerializer):
    # What the tenant gets for free, so the settings page can show the
    # built-in list next to their own additions instead of leaving people
    # guessing whether "stop" is already covered.
    built_in_stop_keywords = serializers.SerializerMethodField()
    built_in_start_keywords = serializers.SerializerMethodField()
    available_timezones = serializers.SerializerMethodField()

    extra_stop_keywords = KeywordListField(required=False)
    extra_start_keywords = KeywordListField(required=False)

    class Meta:
        model = CompliancePolicy
        fields = [
            "stop_keywords_enabled", "extra_stop_keywords", "extra_start_keywords",
            "send_opt_out_confirmation", "opt_out_confirmation",
            "send_opt_in_confirmation", "opt_in_confirmation",
            "opt_out_style", "opt_out_button_title", "opt_out_footer",
            "quiet_hours_enabled", "quiet_hours_start", "quiet_hours_end",
            "timezone", "use_contact_timezone",
            "built_in_stop_keywords", "built_in_start_keywords", "available_timezones",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]

    def get_built_in_stop_keywords(self, _obj):
        return sorted(STOP_KEYWORDS)

    def get_built_in_start_keywords(self, _obj):
        return sorted(START_KEYWORDS)

    def get_available_timezones(self, _obj):
        return sorted({"UTC", *COUNTRY_TIMEZONES.values()})

    def validate_timezone(self, value):
        if load_zone(value) is None:
            raise serializers.ValidationError("Not a timezone this server recognises.")
        return value

    def validate(self, attrs):
        merged = {**{field: getattr(self.instance, field, None) for field in self.Meta.fields}, **attrs}

        if merged.get("quiet_hours_enabled") and merged.get("quiet_hours_start") == merged.get("quiet_hours_end"):
            raise serializers.ValidationError({
                "quiet_hours_end": "Quiet hours need a start and an end that differ.",
            })

        # A confirmation switched on with nothing to say would silently send
        # nothing, which reads as a bug rather than as a setting.
        if merged.get("send_opt_out_confirmation") and not (merged.get("opt_out_confirmation") or "").strip():
            raise serializers.ValidationError({
                "opt_out_confirmation": "Write the message to send, or turn the confirmation off.",
            })
        if merged.get("send_opt_in_confirmation") and not (merged.get("opt_in_confirmation") or "").strip():
            raise serializers.ValidationError({
                "opt_in_confirmation": "Write the message to send, or turn the confirmation off.",
            })

        return attrs


class ConsentEventSerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source="contact.name", read_only=True)
    contact_phone = serializers.CharField(source="contact.phone", read_only=True)
    actor_name = serializers.CharField(source="actor.username", read_only=True, default=None)

    class Meta:
        model = ConsentEvent
        fields = [
            "id", "contact", "contact_name", "contact_phone", "event", "source",
            "actor", "actor_name", "message", "detail", "created_at",
        ]
        read_only_fields = fields
